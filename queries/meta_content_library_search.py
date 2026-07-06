from __future__ import annotations

from typing import Any, Iterator

from common import get_form_bool, h, parse_int
from osint_common import CORE_HEADERS
from providers.meta_content_library import MetaContentLibraryClient
from queries._shared import date_range_fields, export_core, run_core, status_row


HEADERS = CORE_HEADERS + ["platform", "content_type", "async_search"]

META = {
    "key": "meta_content_library_search",
    "name": "Meta Content Library - Search",
    "description": "Stub adapter for Meta Content Library/API searches after approved CASD access.",
    "source_type": "approved_research_api",
    "limitations": [
        "Requires approved Meta Content Library API access.",
        "Graph API Page endpoints are not a general-purpose public Facebook/Instagram scraping API for arbitrary pages and comments.",
        "Programmatic access is designed for approved secure computing environments.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    async_checked = "checked" if get_form_bool(form, "async_search", True) else ""
    return f"""
    <div class="notice">Requires approved Meta Content Library API access. Set META_CONTENT_LIBRARY_ENABLED=true only after access is approved and configured.</div>
    <div class="grid">
      <div class="row">
        <label>Platform</label>
        <select name="platform">
          <option value="facebook">Facebook</option>
          <option value="instagram">Instagram</option>
          <option value="threads">Threads</option>
        </select>
      </div>
      <div class="row" style="grid-column: 1 / -1;"><label>Producer list / account IDs</label><textarea name="producers">{h(form.get("producers", ""))}</textarea></div>
      <div class="row" style="grid-column: 1 / -1;"><label>Keyword query</label><textarea name="keyword_query">{h(form.get("keyword_query", ""))}</textarea></div>
      {date_range_fields(form)}
      <div class="row"><label>Content type</label><select name="content_type"><option>posts</option><option>comments</option><option>reels</option><option>all</option></select></div>
      <div class="row"><label>Max results</label><input type="number" name="max_results" min="1" max="100000" value="{h(form.get("max_results", "1000"))}"></div>
      <div class="row"><label><input type="checkbox" name="async_search" {async_checked}> Async search</label></div>
      <div class="row" style="grid-column: 1 / -1;"><label>Application/access status notes</label><textarea name="access_notes">{h(form.get("access_notes", ""))}</textarea></div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    MetaContentLibraryClient()
    row = status_row(META, str(form.get("platform") or ""), "Project-specific Meta Content Library adapter is not configured.", source_platform="Meta Content Library")
    row["platform"] = form.get("platform") or ""
    row["content_type"] = form.get("content_type") or ""
    row["async_search"] = "yes" if get_form_bool(form, "async_search", True) else "no"
    yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "Meta Content Library API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "Meta Content Library API", form, lambda: iter_row_dicts(form), HEADERS)
