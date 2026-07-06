from __future__ import annotations

from typing import Any, Iterator

from common import h, parse_int
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.gdelt import GdeltClient
from queries._shared import date_range_fields, export_core, run_core


HEADERS = CORE_HEADERS + ["url", "title", "domain", "language", "seendate", "sourcecountry"]

META = {
    "key": "osint_gdelt_search",
    "name": "OSINT - GDELT News Search",
    "description": "Search GDELT news/media coverage for corroborating narratives, timing, and co-mentions.",
    "source_type": "public_archive",
    "limitations": ["GDELT is news/media coverage, not social-media post scraping.", "Article metadata and availability depend on GDELT indexing."],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;"><label>Query</label><textarea name="query">{h(form.get("query", ""))}</textarea></div>
      {date_range_fields(form)}
      <div class="row"><label>Max results</label><input type="number" name="max_results" min="1" max="250" value="{h(form.get("max_results", "100"))}"></div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    query = str(form.get("query") or "").strip()
    if not query:
        raise ValueError("Enter a GDELT query.")
    max_results = parse_int(form.get("max_results", 100), 100, 1, 250)
    client = GdeltClient()
    for article in client.iter_articles(query=query, date_min=str(form.get("date_min") or ""), date_max=str(form.get("date_max") or ""), max_results=max_results):
        url = article.get("url") or ""
        yield core_row(
            source_platform="GDELT",
            source_api="GDELT DOC 2.0",
            source_type=META["source_type"],
            target_input=query,
            query_text=query,
            created_at=article.get("seendate") or "",
            author_display_name=article.get("sourceCommonName") or article.get("domain") or "",
            canonical_url=url,
            text="\n".join(str(article.get(key) or "") for key in ("title", "snippet")),
            media_summary="news article",
            metrics_json={"socialimage": article.get("socialimage")},
            raw_json=article,
            platform_item_id=url,
            url=url,
            title=article.get("title") or "",
            domain=article.get("domain") or "",
            language=article.get("language") or "",
            seendate=article.get("seendate") or "",
            sourcecountry=article.get("sourcecountry") or "",
        )


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "GDELT", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "GDELT", form, lambda: iter_row_dicts(form), HEADERS)
