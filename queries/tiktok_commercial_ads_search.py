from __future__ import annotations

from typing import Any, Iterator

from common import h, parse_int
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.tiktok_commercial import TikTokCommercialClient
from queries._shared import date_range_fields, export_core, flag_controls, include_settings, keep_row, maybe_flag, parse_lines, run_core


HEADERS = CORE_HEADERS + ["ad_id", "advertiser_name", "first_shown_date", "last_shown_date", "targeting_summary", "audience_range", "landing_url", "media_type", "ad_detail_url"]

META = {
    "key": "tiktok_commercial_ads_search",
    "name": "TikTok - Commercial Content Ads Search",
    "description": "Search TikTok Commercial Content API ad/commercial transparency data after API access approval.",
    "source_type": "approved_research_api",
    "limitations": [
        "This is ad/commercial transparency data, not organic TikTok post/comment scraping.",
        "API access and regional coverage depend on TikTok Commercial Content Library availability.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="grid">
      <div class="row"><label>Advertiser name</label><input type="text" name="advertiser_name" value="{h(form.get("advertiser_name", ""))}"></div>
      <div class="row" style="grid-column: 1 / -1;"><label>Keyword terms</label><textarea name="keyword_terms">{h(form.get("keyword_terms", ""))}</textarea></div>
      <div class="row"><label>Country</label><input type="text" name="country" value="{h(form.get("country", ""))}" placeholder="US"></div>
      {date_range_fields(form)}
      <div class="row"><label>Max results</label><input type="number" name="max_results" min="1" max="1000" value="{h(form.get("max_results", "100"))}"></div>
      {flag_controls(form)}
      <div class="row" style="grid-column: 1 / -1;"><label>Custom flag terms</label><textarea name="custom_terms">{h(form.get("custom_terms", ""))}</textarea></div>
    </div>
    """


def _query(form: dict[str, Any]) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if form.get("advertiser_name"):
        query["advertiser_name"] = str(form.get("advertiser_name")).strip()
    terms = parse_lines(form.get("keyword_terms"))
    if terms:
        query["keywords"] = terms
    if form.get("country"):
        query["country"] = str(form.get("country")).upper()
    if form.get("date_min"):
        query["date_min"] = str(form.get("date_min"))
    if form.get("date_max"):
        query["date_max"] = str(form.get("date_max"))
    if not query:
        raise ValueError("Enter advertiser name, keyword terms, country, or date filters.")
    return query


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = include_settings(form)
    client = TikTokCommercialClient()
    max_results = parse_int(form.get("max_results", 100), 100, 1, 1000)
    for ad in client.iter_ads(_query(form), max_results=max_results):
        text = "\n".join(str(ad.get(key) or "") for key in ("creative_text", "ad_text", "title", "description", "advertiser_name"))
        categories, matched = maybe_flag(text, settings)
        ad_id = str(ad.get("ad_id") or ad.get("id") or "")
        row = core_row(
            source_platform="TikTok",
            source_api="TikTok Commercial Content API",
            source_type=META["source_type"],
            target_input=compact_json(_query(form)),
            query_text=str(form.get("keyword_terms") or form.get("advertiser_name") or ""),
            flag_categories=categories,
            matched_terms=matched,
            created_at=str(ad.get("first_shown_date") or ad.get("publish_date") or ""),
            author_display_name=str(ad.get("advertiser_name") or ""),
            canonical_url=str(ad.get("ad_detail_url") or ""),
            text=text,
            media_summary=str(ad.get("media_type") or ""),
            metrics_json={"audience_range": ad.get("audience_range")},
            raw_json=ad,
            platform_item_id=ad_id,
            ad_id=ad_id,
            advertiser_name=ad.get("advertiser_name") or "",
            first_shown_date=ad.get("first_shown_date") or "",
            last_shown_date=ad.get("last_shown_date") or "",
            targeting_summary=compact_json(ad.get("targeting") or ad.get("targeting_summary")),
            audience_range=compact_json(ad.get("audience_range")),
            landing_url=ad.get("landing_url") or "",
            media_type=ad.get("media_type") or "",
            ad_detail_url=ad.get("ad_detail_url") or "",
        )
        if keep_row(row, settings):
            yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "TikTok Commercial Content API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "TikTok Commercial Content API", form, lambda: iter_row_dicts(form), HEADERS)
