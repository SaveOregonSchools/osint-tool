from __future__ import annotations

from typing import Any, Iterator

from common import h, parse_int
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.open_measures import OpenMeasuresClient
from queries._shared import date_range_fields, env_password_field, export_core, flag_controls, include_settings, keep_row, maybe_flag, parse_lines, run_core


HEADERS = CORE_HEADERS + ["provider_name", "platform", "item_id", "url", "posted_at"]

META = {
    "key": "osint_open_measures_search",
    "name": "OSINT - Open Measures Search",
    "description": "Search Open Measures content API for cross-platform historical checks and enrichment.",
    "source_type": "third_party_api",
    "limitations": [
        "Open Measures is a third-party OSINT enrichment source, not a substitute for official platform APIs.",
        "Public API availability, request limits, supported platforms, and data age limits are controlled by Open Measures.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="grid">
      {env_password_field("OPENMEASURES_API_KEY", "api_key", "Open Measures API key")}
      <div class="row" style="grid-column: 1 / -1;"><label>Query</label><textarea name="query">{h(form.get("query", ""))}</textarea></div>
      <div class="row" style="grid-column: 1 / -1;"><label>Platforms</label><textarea name="platforms" placeholder="telegram&#10;rumble&#10;tiktok">{h(form.get("platforms", ""))}</textarea></div>
      {date_range_fields(form)}
      <div class="row"><label>Max results</label><input type="number" name="max_results" min="1" max="1000" value="{h(form.get("max_results", "100"))}"></div>
      {flag_controls(form)}
      <div class="row" style="grid-column: 1 / -1;"><label>Custom flag terms</label><textarea name="custom_terms">{h(form.get("custom_terms", ""))}</textarea></div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = include_settings(form)
    query = str(form.get("query") or "").strip()
    if not query:
        raise ValueError("Enter an Open Measures query.")
    client = OpenMeasuresClient(str(form.get("api_key") or "").strip() or None)
    max_results = parse_int(form.get("max_results", 100), 100, 1, 1000)
    for item in client.iter_content(query=query, platforms=parse_lines(form.get("platforms")), date_min=str(form.get("date_min") or ""), date_max=str(form.get("date_max") or ""), max_results=max_results):
        text = "\n".join(str(item.get(key) or "") for key in ("text", "content", "body", "title"))
        categories, matched = maybe_flag(text, settings)
        item_id = str(item.get("id") or item.get("_id") or item.get("url") or "")
        row = core_row(
            source_platform=str(item.get("platform") or "Open Measures"),
            source_api="Open Measures Content API",
            source_type=META["source_type"],
            target_input=query,
            query_text=query,
            flag_categories=categories,
            matched_terms=matched,
            created_at=str(item.get("posted_at") or item.get("created_at") or item.get("date") or ""),
            author_handle=str(item.get("author") or item.get("username") or ""),
            canonical_url=str(item.get("url") or ""),
            text=text,
            media_summary=str(item.get("media_type") or ""),
            metrics_json=item.get("metrics") or {},
            raw_json=item,
            platform_item_id=item_id,
            provider_name="Open Measures",
            platform=item.get("platform") or "",
            item_id=item_id,
            url=item.get("url") or "",
            posted_at=item.get("posted_at") or item.get("created_at") or "",
        )
        if keep_row(row, settings):
            yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "Open Measures", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "Open Measures", form, lambda: iter_row_dicts(form), HEADERS)
