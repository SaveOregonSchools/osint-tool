from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator

from common import classify_text, parse_terms


ROOT_DIR = Path(__file__).resolve().parent
OSINT_DB_PATH = os.getenv("OSINT_DB_PATH", str(ROOT_DIR / "data" / "osint_cache.db"))

CORE_HEADERS = [
    "source_platform",
    "source_api",
    "source_type",
    "query_run_id",
    "target_input",
    "query_text",
    "flag_categories",
    "matched_terms",
    "created_at",
    "captured_at_utc",
    "author_handle",
    "author_id",
    "author_display_name",
    "canonical_url",
    "text",
    "media_summary",
    "metrics_json",
    "raw_json",
    "content_hash",
    "notes",
]

SOURCE_TYPES = {
    "official_api",
    "approved_research_api",
    "public_archive",
    "third_party_api",
    "unofficial_local_tool",
    "manual_entry",
}

DATA_ACCESS_MODES = {
    "official": {"official_api", "public_archive", "manual_entry"},
    "approved": {"official_api", "approved_research_api", "public_archive", "manual_entry"},
    "unofficial": set(SOURCE_TYPES),
}

CANDIDATE_INTERVENTION_TERMS = [
    "vote for",
    "vote against",
    "elect",
    "re-elect",
    "reelect",
    "defeat",
    "endorse",
    "endorsement",
    "support candidate",
    "donate to",
    "for congress",
    "for senate",
    "for governor",
    "for mayor",
    "campaign kickoff",
]

LOBBYING_TERMS = [
    "call your senator",
    "call your representative",
    "contact lawmakers",
    "tell congress",
    "tell your legislator",
    "support hb",
    "oppose hb",
    "support sb",
    "oppose sb",
    "vote yes",
    "vote no",
    "ballot measure",
    "referendum",
    "initiative",
    "proposition",
    "sign the petition",
    "pass the bill",
    "stop the bill",
]

REVIEW_LABELS = [
    "candidate_intervention_review",
    "lobbying_review",
    "issue_advocacy_review",
    "ad_transparency_review",
    "coordination_review",
    "needs_manual_review",
]

SECRET_KEY_PARTS = ("token", "secret", "password", "credential", "api_key", "apikey", "session")


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8", errors="replace")).hexdigest()


def getenv_required(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def connect_cache() -> sqlite3.Connection:
    db_path = Path(OSINT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_cache(conn)
    return conn


def init_cache(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS query_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          plugin_key TEXT NOT NULL,
          params_json TEXT NOT NULL,
          started_at_utc TEXT NOT NULL,
          finished_at_utc TEXT,
          status TEXT NOT NULL,
          error TEXT,
          result_count INTEGER DEFAULT 0,
          quota_json TEXT,
          notes TEXT
        );

        CREATE TABLE IF NOT EXISTS osint_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          platform TEXT NOT NULL,
          source_api TEXT NOT NULL,
          platform_item_id TEXT,
          canonical_url TEXT,
          author_id TEXT,
          author_handle TEXT,
          author_display_name TEXT,
          created_at TEXT,
          captured_at_utc TEXT NOT NULL,
          text TEXT,
          media_json TEXT,
          metrics_json TEXT,
          raw_json TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          UNIQUE(platform, platform_item_id, source_api)
        );

        CREATE TABLE IF NOT EXISTS item_matches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          query_run_id INTEGER NOT NULL,
          item_id INTEGER NOT NULL,
          matched_terms_json TEXT,
          flag_categories_json TEXT,
          confidence TEXT,
          notes TEXT,
          FOREIGN KEY(query_run_id) REFERENCES query_runs(id),
          FOREIGN KEY(item_id) REFERENCES osint_items(id)
        );

        CREATE TABLE IF NOT EXISTS archive_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          item_id INTEGER NOT NULL,
          requested_at_utc TEXT NOT NULL,
          archive_provider TEXT,
          archive_url TEXT,
          screenshot_path TEXT,
          status TEXT NOT NULL,
          error TEXT,
          FOREIGN KEY(item_id) REFERENCES osint_items(id)
        );
        """
    )
    conn.commit()


def stable_json(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return str(value)


def compact_json(value: Any) -> str:
    return stable_json(value)


def list_from_text(raw: Any) -> list[str]:
    return parse_terms(raw)


def split_multi_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value or "")
    parts: list[str] = []
    for chunk in raw.replace(",", ";").split(";"):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def redact_params(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        key_str = str(key)
        if any(part in key_str.casefold() for part in SECRET_KEY_PARTS):
            out[key_str] = "REDACTED"
        elif isinstance(value, dict):
            out[key_str] = redact_params(value)
        elif isinstance(value, list):
            out[key_str] = ["REDACTED" if isinstance(item, str) and len(item) > 60 else item for item in value]
        else:
            out[key_str] = value
    return out


def start_query_run(conn: sqlite3.Connection, provider: str, plugin_key: str, params: dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO query_runs (provider, plugin_key, params_json, started_at_utc, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (provider, plugin_key, stable_json(redact_params(params)), now_utc_iso(), "running"),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_query_run(
    conn: sqlite3.Connection,
    query_run_id: int,
    *,
    status: str,
    result_count: int = 0,
    error: str | None = None,
    quota: dict[str, Any] | None = None,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE query_runs
        SET finished_at_utc = ?, status = ?, error = ?, result_count = ?, quota_json = ?, notes = ?
        WHERE id = ?
        """,
        (now_utc_iso(), status, error, result_count, stable_json(quota), notes, query_run_id),
    )
    conn.commit()


def flag_text(
    text: str,
    *,
    custom_terms: Iterable[str] = (),
    include_candidate: bool = True,
    include_lobbying: bool = True,
) -> tuple[str, str]:
    return classify_text(
        text,
        custom_terms=custom_terms,
        include_candidate=include_candidate,
        include_lobbying=include_lobbying,
    )


def core_row(
    *,
    source_platform: str,
    source_api: str,
    source_type: str,
    target_input: str = "",
    query_text: str = "",
    flag_categories: str = "",
    matched_terms: str = "",
    created_at: str = "",
    captured_at_utc: str | None = None,
    author_handle: str = "",
    author_id: str = "",
    author_display_name: str = "",
    canonical_url: str = "",
    text: str = "",
    media_summary: str = "",
    metrics_json: Any = "",
    raw_json: Any = "",
    content_hash: str = "",
    notes: str = "",
    query_run_id: int | str = "",
    **extra: Any,
) -> dict[str, Any]:
    raw = stable_json(raw_json if raw_json not in (None, "") else extra or text or canonical_url)
    hash_source = "\n".join([source_platform, source_api, str(canonical_url), str(text), raw])
    row = {
        "source_platform": source_platform,
        "source_api": source_api,
        "source_type": source_type,
        "query_run_id": query_run_id,
        "target_input": target_input,
        "query_text": query_text,
        "flag_categories": flag_categories,
        "matched_terms": matched_terms,
        "created_at": created_at,
        "captured_at_utc": captured_at_utc or now_utc_iso(),
        "author_handle": author_handle,
        "author_id": author_id,
        "author_display_name": author_display_name,
        "canonical_url": canonical_url,
        "text": text,
        "media_summary": media_summary,
        "metrics_json": stable_json(metrics_json),
        "raw_json": raw,
        "content_hash": content_hash or sha256_text(hash_source),
        "notes": notes,
    }
    row.update(extra)
    return row


def row_values(row: dict[str, Any], headers: list[str]) -> list[Any]:
    return [row.get(header, "") for header in headers]


def _platform_item_id(row: dict[str, Any]) -> str:
    for key in ("platform_item_id", "tweet_id", "video_id", "comment_id", "ad_id", "ad_library_id", "id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    url = str(row.get("canonical_url") or "").strip()
    return sha256_text(url) if url else ""


def persist_core_item(conn: sqlite3.Connection, query_run_id: int, row: dict[str, Any]) -> int | None:
    raw_json = str(row.get("raw_json") or "")
    platform = str(row.get("source_platform") or "")
    source_api = str(row.get("source_api") or "")
    if not platform or not source_api or not raw_json:
        return None

    platform_item_id = _platform_item_id(row) or None
    conn.execute(
        """
        INSERT INTO osint_items (
          platform, source_api, platform_item_id, canonical_url, author_id, author_handle,
          author_display_name, created_at, captured_at_utc, text, media_json, metrics_json,
          raw_json, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(platform, platform_item_id, source_api) DO UPDATE SET
          canonical_url = excluded.canonical_url,
          author_id = excluded.author_id,
          author_handle = excluded.author_handle,
          author_display_name = excluded.author_display_name,
          created_at = excluded.created_at,
          captured_at_utc = excluded.captured_at_utc,
          text = excluded.text,
          media_json = excluded.media_json,
          metrics_json = excluded.metrics_json,
          raw_json = excluded.raw_json,
          content_hash = excluded.content_hash
        """,
        (
            platform,
            source_api,
            platform_item_id,
            row.get("canonical_url") or "",
            row.get("author_id") or "",
            row.get("author_handle") or "",
            row.get("author_display_name") or "",
            row.get("created_at") or "",
            row.get("captured_at_utc") or now_utc_iso(),
            row.get("text") or "",
            row.get("media_json") or row.get("media_summary") or "",
            row.get("metrics_json") or "",
            raw_json,
            row.get("content_hash") or sha256_text(raw_json),
        ),
    )
    item_row = conn.execute(
        """
        SELECT id FROM osint_items
        WHERE platform = ? AND source_api = ? AND (
          (platform_item_id IS ?)
          OR platform_item_id = ?
        )
        ORDER BY id DESC
        LIMIT 1
        """,
        (platform, source_api, platform_item_id, platform_item_id),
    ).fetchone()
    if not item_row:
        return None
    item_id = int(item_row["id"])
    conn.execute(
        """
        INSERT INTO item_matches (
          query_run_id, item_id, matched_terms_json, flag_categories_json, confidence, notes
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            query_run_id,
            item_id,
            stable_json(split_multi_value(row.get("matched_terms"))),
            stable_json(split_multi_value(row.get("flag_categories"))),
            row.get("confidence") or "",
            row.get("notes") or "",
        ),
    )
    return item_id


def materialize_rows(
    *,
    provider: str,
    plugin_key: str,
    params: dict[str, Any],
    row_iter: Iterable[dict[str, Any]],
    headers: list[str],
) -> list[list[Any]]:
    conn = connect_cache()
    query_run_id = start_query_run(conn, provider, plugin_key, params)
    rows: list[list[Any]] = []
    count = 0
    try:
        for row in row_iter:
            row.setdefault("query_run_id", query_run_id)
            if "captured_at_utc" not in row or not row["captured_at_utc"]:
                row["captured_at_utc"] = now_utc_iso()
            if "content_hash" not in row or not row["content_hash"]:
                row["content_hash"] = sha256_text(str(row.get("raw_json") or row.get("text") or row))
            persist_core_item(conn, query_run_id, row)
            rows.append(row_values(row, headers))
            count += 1
        finish_query_run(conn, query_run_id, status="ok", result_count=count)
        return rows
    except Exception as exc:
        finish_query_run(conn, query_run_id, status="error", result_count=count, error=str(exc))
        raise
    finally:
        conn.close()


def selected_access_mode(form: dict[str, Any]) -> str:
    mode = str(form.get("data_access_mode") or os.getenv("OSINT_DATA_ACCESS_MODE") or "official").strip().lower()
    return mode if mode in DATA_ACCESS_MODES else "official"


def source_type_allowed(source_type: str, form: dict[str, Any]) -> bool:
    mode = selected_access_mode(form)
    return source_type in DATA_ACCESS_MODES[mode]


def enforce_source_access(meta: dict[str, Any], form: dict[str, Any]) -> None:
    source_type = str(meta.get("source_type") or "official_api")
    if source_type_allowed(source_type, form):
        return
    mode = selected_access_mode(form)
    raise RuntimeError(
        f"This module is marked {source_type}. Change Data access mode from {mode!r} "
        "if you have approved access and want to include this source type."
    )


def limit_iter(items: Iterable[Any], limit: int) -> Iterator[Any]:
    count = 0
    for item in items:
        if count >= limit:
            break
        yield item
        count += 1
