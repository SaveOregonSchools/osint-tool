from __future__ import annotations

from typing import Any, Iterator

from common import h, parse_int
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.google_political_ads import GooglePoliticalAdsClient
from queries._shared import date_range_fields, export_core, flag_controls, include_settings, keep_row, maybe_flag, max_field, run_core


HEADERS = CORE_HEADERS + [
    "ad_id",
    "advertiser_id",
    "advertiser_name",
    "date_range_start",
    "date_range_end",
    "impressions",
    "spend_range_min_usd",
    "spend_range_max_usd",
    "regions",
    "creative_page_url",
]

META = {
    "key": "google_political_ads_search",
    "name": "Google - Political Ads BigQuery Search",
    "description": "Search Google's Political Ads Transparency Report public BigQuery dataset for election ads across Google Ads, YouTube, and Display & Video 360.",
    "source_type": "official_api",
    "coverage": "Data source: Google Political Ads Transparency Report / BigQuery.",
    "limitations": [
        "Coverage is election ads, not all YouTube videos and not all organic posts/comments.",
        "Requires a Google Cloud project and BigQuery access to public datasets.",
        "Public dataset schema can change; review raw JSON for defensible evidence.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="grid">
      <div class="row">
        <label>Google Cloud project</label>
        <input type="text" name="google_cloud_project" value="{h(form.get("google_cloud_project", ""))}" placeholder="Optional if GOOGLE_CLOUD_PROJECT is set">
      </div>
      <div class="row">
        <label>Advertiser name contains</label>
        <input type="text" name="advertiser_name_contains" value="{h(form.get("advertiser_name_contains", ""))}">
      </div>
      <div class="row">
        <label>Advertiser ID</label>
        <input type="text" name="advertiser_id" value="{h(form.get("advertiser_id", ""))}">
      </div>
      <div class="row">
        <label>Region / geo targeting text</label>
        <input type="text" name="region" value="{h(form.get("region", ""))}" placeholder="US, Oregon, etc.">
      </div>
      {date_range_fields(form)}
      {max_field(form, "limit", "Limit", 100, 10000)}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Keyword terms</label>
        <textarea name="keyword_terms" placeholder="candidate name&#10;ballot measure">{h(form.get("keyword_terms", ""))}</textarea>
        <div class="subtle">Keyword matching searches the row JSON because creative text availability varies by dataset field.</div>
      </div>
      {flag_controls(form)}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Custom flag terms</label>
        <textarea name="custom_terms">{h(form.get("custom_terms", ""))}</textarea>
      </div>
    </div>
    """


def _filters(form: dict[str, Any]) -> dict[str, Any]:
    return {
        "advertiser_name_contains": form.get("advertiser_name_contains", ""),
        "advertiser_id": form.get("advertiser_id", ""),
        "region": form.get("region", ""),
        "date_min": form.get("date_min", ""),
        "date_max": form.get("date_max", ""),
        "keyword_terms": form.get("keyword_terms", ""),
        "limit": parse_int(form.get("limit", 100), 100, 1, 10000),
    }


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = include_settings(form)
    client = GooglePoliticalAdsClient(str(form.get("google_cloud_project") or "").strip() or None)
    filters = _filters(form)
    for item in client.iter_ads(filters):
        text = compact_json({key: item.get(key) for key in ("advertiser_name", "regions", "gender_targeting", "age_targeting", "geo_targeting_included")})
        categories, matched = maybe_flag(text + "\n" + str(form.get("keyword_terms") or ""), settings)
        ad_id = str(item.get("ad_id") or "")
        row = core_row(
            source_platform="Google Ads",
            source_api="bigquery-public-data.google_political_ads.creative_stats",
            source_type=META["source_type"],
            target_input=str(filters.get("advertiser_name_contains") or filters.get("advertiser_id") or filters.get("keyword_terms") or ""),
            query_text=compact_json(filters),
            flag_categories=categories,
            matched_terms=matched,
            created_at=str(item.get("date_range_start") or ""),
            author_id=str(item.get("advertiser_id") or ""),
            author_display_name=str(item.get("advertiser_name") or ""),
            canonical_url=str(item.get("creative_page_url") or item.get("ad_url") or ""),
            text=text,
            media_summary="ad creative/transparency row",
            metrics_json={"impressions": item.get("impressions"), "spend_range_min_usd": item.get("spend_range_min_usd"), "spend_range_max_usd": item.get("spend_range_max_usd")},
            raw_json=item.get("raw_json") or item,
            platform_item_id=ad_id,
            ad_id=ad_id,
            advertiser_id=item.get("advertiser_id") or "",
            advertiser_name=item.get("advertiser_name") or "",
            date_range_start=item.get("date_range_start") or "",
            date_range_end=item.get("date_range_end") or "",
            impressions=item.get("impressions") or "",
            spend_range_min_usd=item.get("spend_range_min_usd") or "",
            spend_range_max_usd=item.get("spend_range_max_usd") or "",
            regions=compact_json(item.get("regions")),
            creative_page_url=item.get("creative_page_url") or "",
        )
        if keep_row(row, settings):
            yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "Google Political Ads BigQuery", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "Google Political Ads BigQuery", form, lambda: iter_row_dicts(form), HEADERS)
