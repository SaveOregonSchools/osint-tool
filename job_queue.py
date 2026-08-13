from __future__ import annotations

import json
import os
import sqlite3
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


BASE_DIR = Path(__file__).resolve().parent
JOB_DB_PATH = Path(os.getenv("OSINT_JOB_DB_PATH", str(BASE_DIR / "data" / "job_queue.sqlite")))
JobHandler = Callable[[dict[str, Any], Callable[[int, int, str], None]], dict[str, Any] | None]

_handlers: dict[str, JobHandler] = {}
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()
_worker_wakeup = threading.Event()
_initialized_paths: set[str] = set()


class JobExecutionError(RuntimeError):
    def __init__(self, message: str, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = result or {}


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc_value: Any, traceback_value: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback_value))
        finally:
            self.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    JOB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(JOB_DB_PATH, timeout=30, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_job_queue() -> None:
    path_key = str(JOB_DB_PATH.resolve())
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                module_key TEXT NOT NULL,
                handler_key TEXT NOT NULL,
                label TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 1,
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)")
        if path_key not in _initialized_paths:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    error = CASE WHEN error = '' THEN 'The application stopped while this job was running.' ELSE error END,
                    completed_at = ?
                WHERE status = 'running'
                """,
                (_now(),),
            )
            _initialized_paths.add(path_key)


def register_job_handler(key: str, handler: JobHandler) -> None:
    if not key or not callable(handler):
        raise ValueError("A queue handler requires a key and callable handler.")
    _handlers[key] = handler


def enqueue_job(
    *,
    module_key: str,
    handler_key: str,
    label: str,
    payload: dict[str, Any],
    group_id: str = "",
    start_worker_now: bool = True,
) -> str:
    init_job_queue()
    if handler_key not in _handlers:
        raise RuntimeError(f"Queue handler is not registered: {handler_key}")
    job_id = uuid.uuid4().hex
    group = group_id or job_id
    payload_json = json.dumps(payload, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, group_id, module_key, handler_key, label, status,
                payload_json, created_at, message
            ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, 'Waiting for a worker')
            """,
            (job_id, group, module_key, handler_key, label, payload_json, _now()),
        )
    if start_worker_now:
        start_worker()
        _worker_wakeup.set()
    return job_id


def _claim_next_job() -> dict[str, Any] | None:
    init_job_queue()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at, rowid LIMIT 1"
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        changed = conn.execute(
            """
            UPDATE jobs
            SET status = 'running', started_at = ?, message = 'Starting job'
            WHERE id = ? AND status = 'queued'
            """,
            (_now(), row["id"]),
        ).rowcount
        conn.commit()
        if not changed:
            return None
        result = dict(row)
        result["status"] = "running"
        return result


def update_job_progress(job_id: str, current: int, total: int, message: str = "") -> None:
    total = max(1, int(total))
    current = max(0, min(int(current), total))
    with _connect() as conn:
        conn.execute(
            """
            UPDATE jobs SET progress_current = ?, progress_total = ?, message = ?
            WHERE id = ? AND status = 'running'
            """,
            (current, total, str(message or ""), job_id),
        )


def _finish_job(job_id: str, status: str, *, result: dict[str, Any] | None = None, error: str = "") -> None:
    message = "Completed" if status == "completed" else "Failed"
    with _connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, result_json = ?, error = ?, message = ?,
                progress_current = progress_total, completed_at = ?
            WHERE id = ?
            """,
            (status, json.dumps(result or {}, ensure_ascii=False), error, message, _now(), job_id),
        )


def run_pending_job_once() -> bool:
    job = _claim_next_job()
    if job is None:
        return False
    job_id = str(job["id"])
    handler = _handlers.get(str(job["handler_key"]))
    if handler is None:
        _finish_job(job_id, "failed", error=f"Queue handler is unavailable: {job['handler_key']}")
        return True
    try:
        payload = json.loads(str(job["payload_json"]))
        result = handler(
            payload,
            lambda current, total, message="": update_job_progress(job_id, current, total, message),
        )
        _finish_job(job_id, "completed", result=result or {})
    except JobExecutionError as exc:
        _finish_job(job_id, "failed", result=exc.result, error=str(exc))
    except Exception:
        _finish_job(job_id, "failed", error=traceback.format_exc())
    return True


def _worker_loop() -> None:
    while True:
        if run_pending_job_once():
            continue
        _worker_wakeup.wait(timeout=1.0)
        _worker_wakeup.clear()


def start_worker() -> None:
    global _worker_thread
    init_job_queue()
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(target=_worker_loop, name="osint-job-worker", daemon=True)
        _worker_thread.start()


def list_jobs(limit: int = 250) -> list[dict[str, Any]]:
    init_job_queue()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            ORDER BY
                CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                CASE WHEN status IN ('running', 'queued') THEN created_at END ASC,
                CASE WHEN status NOT IN ('running', 'queued')
                     THEN COALESCE(NULLIF(completed_at, ''), created_at) END DESC,
                CASE WHEN status IN ('running', 'queued') THEN rowid END ASC,
                CASE WHEN status NOT IN ('running', 'queued') THEN rowid END DESC
            LIMIT ?
            """,
            (max(1, min(limit, 2000)),),
        ).fetchall()
    jobs: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["result"] = json.loads(item.pop("result_json") or "{}")
        except json.JSONDecodeError:
            item["result"] = {}
        item.pop("payload_json", None)
        jobs.append(item)
    return jobs


def get_job(job_id: str) -> dict[str, Any] | None:
    init_job_queue()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["result"] = json.loads(item.pop("result_json") or "{}")
    except json.JSONDecodeError:
        item["result"] = {}
    item.pop("payload_json", None)
    return item


def queue_counts() -> dict[str, int]:
    init_job_queue()
    counts = {"queued": 0, "running": 0, "completed": 0, "failed": 0}
    with _connect() as conn:
        for row in conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"):
            counts[str(row["status"])] = int(row["count"])
    return counts
