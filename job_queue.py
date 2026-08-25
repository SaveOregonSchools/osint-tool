from __future__ import annotations

import json
import os
import sqlite3
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone
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
_worker_instance_id = uuid.uuid4().hex
_HEARTBEAT_INTERVAL_SECONDS = 5.0
_HEARTBEAT_STALE_SECONDS = 90


def _canonical_utc_timestamp(value: Any, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Queue timestamps must be ISO-8601 values with a timezone.") from exc
    if parsed.tzinfo is None:
        raise ValueError("Queue timestamps must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


class JobExecutionError(RuntimeError):
    def __init__(self, message: str, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = result or {}


class JobRetry(RuntimeError):
    """Return a running job to the persistent queue for a later attempt."""

    def __init__(
        self,
        message: str,
        *,
        retry_at: str,
        payload: dict[str, Any],
        result: dict[str, Any] | None = None,
        throttle_key: str = "",
    ) -> None:
        super().__init__(message)
        self.retry_at = _canonical_utc_timestamp(retry_at)
        self.payload = payload
        self.result = result or {}
        self.throttle_key = str(throttle_key or "").strip()


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


def _recover_stale_jobs(conn: sqlite3.Connection) -> None:
    current_text = _now()
    current = datetime.fromisoformat(current_text.replace("Z", "+00:00")).astimezone(timezone.utc)
    stale_before = (current - timedelta(seconds=_HEARTBEAT_STALE_SECONDS)).isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE jobs
        SET status = 'failed',
            error = CASE WHEN error = '' THEN 'The queue worker stopped while this job was running.' ELSE error END,
            completed_at = ?, worker_id = '', heartbeat_at = ''
        WHERE status = 'running'
          AND (heartbeat_at = '' OR heartbeat_at < ?)
        """,
        (current_text, stale_before),
    )


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
                available_at TEXT NOT NULL DEFAULT '',
                throttle_key TEXT NOT NULL DEFAULT '',
                resource_key TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                worker_id TEXT NOT NULL DEFAULT '',
                heartbeat_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)")}
        if "available_at" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN available_at TEXT NOT NULL DEFAULT ''")
        if "throttle_key" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN throttle_key TEXT NOT NULL DEFAULT ''")
        if "resource_key" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN resource_key TEXT NOT NULL DEFAULT ''")
        if "attempt_count" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
        if "worker_id" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN worker_id TEXT NOT NULL DEFAULT ''")
        if "heartbeat_at" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_throttles (
                throttle_key TEXT PRIMARY KEY,
                blocked_until TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_available ON jobs(status, available_at, created_at)"
        )
        if path_key not in _initialized_paths:
            _recover_stale_jobs(conn)
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
    throttle_key: str = "",
    resource_key: str = "",
    available_at: str = "",
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
                payload_json, created_at, message, throttle_key, resource_key, available_at
            ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, 'Waiting for a worker', ?, ?, ?)
            """,
            (
                job_id,
                group,
                module_key,
                handler_key,
                label,
                payload_json,
                _now(),
                str(throttle_key or "").strip(),
                str(resource_key or "").strip(),
                _canonical_utc_timestamp(available_at, allow_empty=True),
            ),
        )
    if start_worker_now:
        start_worker()
        _worker_wakeup.set()
    return job_id


def _claim_next_job() -> dict[str, Any] | None:
    init_job_queue()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _recover_stale_jobs(conn)
        now = _now()
        row = conn.execute(
            """
            SELECT jobs.*
            FROM jobs
            LEFT JOIN job_throttles
              ON job_throttles.throttle_key = jobs.throttle_key
            WHERE jobs.status = 'queued'
              AND (jobs.available_at = '' OR jobs.available_at <= ?)
              AND (
                    jobs.throttle_key = ''
                    OR job_throttles.blocked_until IS NULL
                    OR job_throttles.blocked_until <= ?
                  )
              AND (
                    jobs.resource_key = ''
                    OR NOT EXISTS (
                        SELECT 1
                        FROM jobs AS active_resource
                        WHERE active_resource.status = 'running'
                          AND active_resource.resource_key = jobs.resource_key
                    )
                  )
              AND (
                    jobs.throttle_key = ''
                    OR NOT EXISTS (
                        SELECT 1
                        FROM jobs AS active
                        WHERE active.status = 'running'
                          AND active.throttle_key = jobs.throttle_key
                    )
                  )
            ORDER BY jobs.created_at, jobs.rowid
            LIMIT 1
            """,
            (now, now),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        changed = conn.execute(
            """
            UPDATE jobs
            SET status = 'running', started_at = ?, message = 'Starting job',
                attempt_count = attempt_count + 1, worker_id = ?, heartbeat_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now, _worker_instance_id, now, row["id"]),
        ).rowcount
        conn.commit()
        if not changed:
            return None
        result = dict(row)
        result["status"] = "running"
        result["attempt_count"] = int(result.get("attempt_count") or 0) + 1
        return result


def _set_job_throttle_in_connection(
    conn: sqlite3.Connection, throttle_key: str, blocked_until: str, message: str, now: str
) -> None:
    conn.execute(
        """
        INSERT INTO job_throttles (throttle_key, blocked_until, message, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(throttle_key) DO UPDATE SET
            blocked_until = CASE
                WHEN excluded.blocked_until > job_throttles.blocked_until
                THEN excluded.blocked_until
                ELSE job_throttles.blocked_until
            END,
            message = CASE
                WHEN excluded.blocked_until >= job_throttles.blocked_until
                THEN excluded.message
                ELSE job_throttles.message
            END,
            updated_at = CASE
                WHEN excluded.blocked_until >= job_throttles.blocked_until
                THEN excluded.updated_at
                ELSE job_throttles.updated_at
            END
        """,
        (throttle_key, blocked_until, message, now),
    )
    effective = conn.execute(
        "SELECT message FROM job_throttles WHERE throttle_key = ?", (throttle_key,)
    ).fetchone()
    effective_message = str(effective[0]) if effective is not None else message
    conn.execute(
        "UPDATE jobs SET message = ? WHERE status = 'queued' AND throttle_key = ?",
        (effective_message, throttle_key),
    )


def set_job_throttle(throttle_key: str, blocked_until: str, message: str) -> None:
    """Persist a shared gate that prevents matching queued jobs from running early."""
    key = str(throttle_key or "").strip()
    if not key:
        return
    until = _canonical_utc_timestamp(blocked_until)
    init_job_queue()
    now = _now()
    detail = str(message or "Waiting for a shared throttle").strip()
    with _connect() as conn:
        _set_job_throttle_in_connection(conn, key, until, detail, now)


def update_job_progress(job_id: str, current: int, total: int, message: str = "") -> None:
    total = max(1, int(total))
    current = max(0, min(int(current), total))
    with _connect() as conn:
        conn.execute(
            """
            UPDATE jobs SET progress_current = ?, progress_total = ?, message = ?, heartbeat_at = ?
            WHERE id = ? AND status = 'running' AND worker_id = ?
            """,
            (current, total, str(message or ""), _now(), job_id, _worker_instance_id),
        )


def _finish_job(job_id: str, status: str, *, result: dict[str, Any] | None = None, error: str = "") -> None:
    message = "Completed" if status == "completed" else "Failed"
    with _connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, result_json = ?, error = ?, message = ?,
                progress_current = progress_total, completed_at = ?,
                worker_id = '', heartbeat_at = ''
            WHERE id = ? AND status = 'running' AND worker_id = ?
            """,
            (
                status,
                json.dumps(result or {}, ensure_ascii=False),
                error,
                message,
                _now(),
                job_id,
                _worker_instance_id,
            ),
        )


def _defer_job(job_id: str, retry: JobRetry) -> None:
    message = str(retry)
    with _connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'queued', payload_json = ?, result_json = ?, error = '',
                message = ?, available_at = ?, progress_current = 0, started_at = '',
                worker_id = '', heartbeat_at = '',
                throttle_key = CASE WHEN ? <> '' THEN ? ELSE throttle_key END
            WHERE id = ? AND status = 'running' AND worker_id = ?
            """,
            (
                json.dumps(retry.payload, ensure_ascii=False),
                json.dumps(retry.result, ensure_ascii=False),
                message,
                retry.retry_at,
                retry.throttle_key,
                retry.throttle_key,
                job_id,
                _worker_instance_id,
            ),
        )
        if retry.throttle_key:
            _set_job_throttle_in_connection(conn, retry.throttle_key, retry.retry_at, message, _now())


def run_pending_job_once() -> bool:
    job = _claim_next_job()
    if job is None:
        return False
    job_id = str(job["id"])
    handler = _handlers.get(str(job["handler_key"]))
    if handler is None:
        _finish_job(job_id, "failed", error=f"Queue handler is unavailable: {job['handler_key']}")
        return True
    heartbeat_stop = threading.Event()

    def heartbeat() -> None:
        while not heartbeat_stop.wait(_HEARTBEAT_INTERVAL_SECONDS):
            with _connect() as conn:
                conn.execute(
                    "UPDATE jobs SET heartbeat_at = ? WHERE id = ? AND status = 'running' AND worker_id = ?",
                    (_now(), job_id, _worker_instance_id),
                )

    heartbeat_thread = threading.Thread(target=heartbeat, name=f"job-heartbeat-{job_id[:8]}", daemon=True)
    heartbeat_thread.start()
    try:
        payload = json.loads(str(job["payload_json"]))
        result = handler(
            payload,
            lambda current, total, message="": update_job_progress(job_id, current, total, message),
        )
        _finish_job(job_id, "completed", result=result or {})
    except JobRetry as exc:
        _defer_job(job_id, exc)
    except JobExecutionError as exc:
        _finish_job(job_id, "failed", result=exc.result, error=str(exc))
    except Exception:
        _finish_job(job_id, "failed", error=traceback.format_exc())
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)
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
