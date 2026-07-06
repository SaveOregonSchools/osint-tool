from __future__ import annotations

from typing import Any, Iterator

from common import get_form_bool, h, parse_int
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.wayback import WaybackClient, normalize_archive_url
from queries._shared import export_core, parse_lines, run_core


HEADERS = CORE_HEADERS + ["lookup_type", "original_url", "archive_url", "timestamp", "status_code", "mime_type", "digest"]

META = {
    "key": "osint_wayback_lookup",
    "name": "OSINT - Wayback Lookup",
    "description": "Check Wayback Machine availability and CDX captures for public URLs.",
    "source_type": "public_archive",
    "limitations": [
        "Wayback captures are public archive records and may be incomplete.",
        "Save Page Now availability and success depend on Internet Archive policy and target site behavior.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    cdx_checked = "checked" if get_form_bool(form, "include_cdx", True) else ""
    save_checked = "checked" if get_form_bool(form, "save_page_now", False) else ""
    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;"><label>URLs</label><textarea name="urls">{h(form.get("urls", ""))}</textarea></div>
      <div class="row"><label>Timestamp</label><input type="text" name="timestamp" value="{h(form.get("timestamp", ""))}" placeholder="YYYYMMDDhhmmss optional"></div>
      <div class="row"><label>CDX from</label><input type="text" name="from_ts" value="{h(form.get("from_ts", ""))}" placeholder="YYYYMMDD"></div>
      <div class="row"><label>CDX to</label><input type="text" name="to_ts" value="{h(form.get("to_ts", ""))}" placeholder="YYYYMMDD"></div>
      <div class="row"><label>CDX limit</label><input type="number" name="limit" min="1" max="1000" value="{h(form.get("limit", "50"))}"></div>
      <div class="row"><label><input type="checkbox" name="include_cdx" {cdx_checked}> Include CDX captures</label><label><input type="checkbox" name="save_page_now" {save_checked}> Request Save Page Now</label></div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    urls = parse_lines(form.get("urls"))
    if not urls:
        raise ValueError("Enter at least one URL.")
    client = WaybackClient()
    limit = parse_int(form.get("limit", 50), 50, 1, 1000)
    for url in urls:
        availability = client.availability(url, str(form.get("timestamp") or ""))
        archived = ((availability.get("archived_snapshots") or {}).get("closest") or {})
        archive_url = archived.get("url") or ""
        yield core_row(
            source_platform="Wayback Machine",
            source_api="Availability API",
            source_type=META["source_type"],
            target_input=url,
            query_text=url,
            created_at=archived.get("timestamp") or "",
            canonical_url=archive_url or url,
            text=compact_json(archived),
            media_summary="availability",
            raw_json=availability,
            platform_item_id=f"availability:{url}",
            lookup_type="availability",
            original_url=url,
            archive_url=archive_url,
            timestamp=archived.get("timestamp") or "",
            status_code=archived.get("status") or "",
            mime_type="",
            digest="",
        )
        if get_form_bool(form, "include_cdx", True):
            for capture in client.iter_cdx(url, from_ts=str(form.get("from_ts") or ""), to_ts=str(form.get("to_ts") or ""), limit=limit):
                timestamp = capture.get("timestamp") or ""
                original = capture.get("original") or url
                yield core_row(
                    source_platform="Wayback Machine",
                    source_api="CDX API",
                    source_type=META["source_type"],
                    target_input=url,
                    query_text=url,
                    created_at=timestamp,
                    canonical_url=normalize_archive_url(timestamp, original),
                    text=compact_json(capture),
                    media_summary="cdx capture",
                    raw_json=capture,
                    platform_item_id=f"{timestamp}:{original}",
                    lookup_type="cdx",
                    original_url=original,
                    archive_url=normalize_archive_url(timestamp, original),
                    timestamp=timestamp,
                    status_code=capture.get("statuscode") or "",
                    mime_type=capture.get("mimetype") or "",
                    digest=capture.get("digest") or "",
                )
        if get_form_bool(form, "save_page_now", False):
            saved = client.save_page_now(url)
            yield core_row(
                source_platform="Wayback Machine",
                source_api="Save Page Now",
                source_type=META["source_type"],
                target_input=url,
                query_text=url,
                canonical_url=saved.get("archive_url") or "",
                text=compact_json(saved),
                media_summary="save-page-now",
                raw_json=saved,
                platform_item_id=f"save:{url}",
                lookup_type="save",
                original_url=url,
                archive_url=saved.get("archive_url") or "",
                timestamp="",
                status_code=saved.get("status_code") or "",
                mime_type="",
                digest="",
            )


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "Wayback Machine", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "Wayback Machine", form, lambda: iter_row_dicts(form), HEADERS)
