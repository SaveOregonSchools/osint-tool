from __future__ import annotations

from typing import Any, Iterator

from common import h, parse_int
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.common_crawl import CommonCrawlClient
from queries._shared import export_core, parse_lines, run_core


HEADERS = CORE_HEADERS + ["collection", "url", "timestamp", "mime", "status", "digest", "warc_filename", "offset", "length"]

META = {
    "key": "osint_common_crawl_lookup",
    "name": "OSINT - Common Crawl Lookup",
    "description": "Query Common Crawl CDX indexes for historical public web captures.",
    "source_type": "public_archive",
    "limitations": ["Common Crawl indexes public web captures and is not a social platform API.", "Use WARC records carefully and preserve source context."],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;"><label>URL patterns / domains</label><textarea name="urls" placeholder="example.org/*">{h(form.get("urls", ""))}</textarea></div>
      <div class="row"><label>Collection ID</label><input type="text" name="collection" value="{h(form.get("collection", ""))}" placeholder="Optional, latest if blank"></div>
      <div class="row"><label>Limit per URL</label><input type="number" name="limit" min="1" max="1000" value="{h(form.get("limit", "100"))}"></div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    urls = parse_lines(form.get("urls"))
    if not urls:
        raise ValueError("Enter at least one URL pattern or domain.")
    client = CommonCrawlClient()
    limit = parse_int(form.get("limit", 100), 100, 1, 1000)
    for url in urls:
        for item in client.iter_cdx(url, collection=str(form.get("collection") or ""), limit=limit):
            original = item.get("url") or item.get("urlkey") or url
            timestamp = item.get("timestamp") or ""
            yield core_row(
                source_platform="Common Crawl",
                source_api="CDXJ Index",
                source_type=META["source_type"],
                target_input=url,
                query_text=url,
                created_at=timestamp,
                canonical_url=str(original),
                text=compact_json(item),
                media_summary="warc index record",
                raw_json=item,
                platform_item_id=f"{timestamp}:{original}",
                collection=item.get("_collection") or "",
                url=original,
                timestamp=timestamp,
                mime=item.get("mime") or "",
                status=item.get("status") or "",
                digest=item.get("digest") or "",
                warc_filename=item.get("filename") or "",
                offset=item.get("offset") or "",
                length=item.get("length") or "",
            )


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "Common Crawl", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "Common Crawl", form, lambda: iter_row_dicts(form), HEADERS)
