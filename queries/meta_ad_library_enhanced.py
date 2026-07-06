from __future__ import annotations

import json
from typing import Any, Iterator

from common import get_form_bool, h, parse_int, parse_terms
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.meta_ad_library import (
    AD_FIELDS,
    MetaAdLibraryClient,
    build_ad_snapshot_url,
    chunk_page_ids,
    normalize_page_ids,
)
from queries._shared import date_range_fields, delay_field, env_password_field, export_core, flag_controls, include_settings, keep_row, maybe_flag, run_core


HEADERS = CORE_HEADERS + [
    "ad_library_id",
    "ad_library_public_url",
    "page_id",
    "page_name",
    "ad_creation_time",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "publisher_platforms",
    "funding_entity",
    "currency",
    "spend",
    "impressions",
    "ad_creative_bodies",
    "ad_creative_link_titles",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "demographic_distribution",
    "delivery_by_region",
    "ad_snapshot_url",
]

META = {
    "key": "meta_ad_library_enhanced",
    "name": "Meta - Ad Library Enhanced",
    "description": "Search Meta Ad Library API ads by keyword, Page ID watchlists, or combined Page ID + keyword searches.",
    "source_type": "official_api",
    "coverage": "Data source: Meta Ad Library API for ads.",
    "limitations": [
        "Meta Ad Library API is for ads. It does not retrieve ordinary Facebook/Instagram posts, comments, replies, stories, or reels comments.",
        "Political/issue ads and all-ads coverage vary by country, date, and Meta API access rules.",
        "Page-ID-only queries intentionally omit blank search_terms and keyword search_type.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    mode = form.get("query_mode", "keywords_only")
    ad_type = form.get("ad_type", "POLITICAL_AND_ISSUE_ADS")
    active_status = form.get("active_status", "ALL")

    def opt(current: str, value: str, label: str) -> str:
        return f'<option value="{h(value)}" {"selected" if current == value else ""}>{h(label)}</option>'

    return f"""
    <div class="grid">
      {env_password_field("META_ACCESS_TOKEN", "access_token", "Meta access token", "Falls back to META_AD_LIBRARY_ACCESS_TOKEN.")}
      <div class="row">
        <label>Query mode</label>
        <select name="query_mode">
          {opt(mode, "keywords_only", "Keyword sweep")}
          {opt(mode, "page_ids_only", "Page ID watch")}
          {opt(mode, "page_ids_plus_keywords", "Page IDs + keywords")}
          {opt(mode, "advertiser_discovery", "Advertiser discovery")}
        </select>
      </div>
      <div class="row">
        <label>Reached country</label>
        <input type="text" name="country" maxlength="2" value="{h(form.get("country", "US"))}">
      </div>
      <div class="row">
        <label>Ad type</label>
        <select name="ad_type">
          {opt(ad_type, "POLITICAL_AND_ISSUE_ADS", "Political and issue ads")}
          {opt(ad_type, "ALL", "All available ad types")}
        </select>
      </div>
      <div class="row">
        <label>Active status</label>
        <select name="active_status">
          {opt(active_status, "ALL", "All")}
          {opt(active_status, "ACTIVE", "Active")}
          {opt(active_status, "INACTIVE", "Inactive")}
        </select>
      </div>
      <div class="row" style="grid-column: 1 / -1;">
        <label>Search terms</label>
        <textarea name="search_terms" placeholder="candidate name&#10;slogan&#10;ballot measure">{h(form.get("search_terms", ""))}</textarea>
      </div>
      <div class="row" style="grid-column: 1 / -1;">
        <label>Page IDs / search_page_ids</label>
        <textarea name="page_ids" placeholder="123456789&#10;987654321">{h(form.get("page_ids", form.get("search_page_ids", "")))}</textarea>
      </div>
      <div class="row">
        <label>Publisher platforms</label>
        <input type="text" name="publisher_platforms" value="{h(form.get("publisher_platforms", "FACEBOOK,INSTAGRAM"))}" placeholder="FACEBOOK,INSTAGRAM,THREADS">
      </div>
      <div class="row">
        <label>Languages</label>
        <input type="text" name="languages" value="{h(form.get("languages", ""))}" placeholder="Optional comma list">
      </div>
      {date_range_fields(form, "date_min", "date_max")}
      {max_field(form, "max_results", "Max results per API query", 250, 5000)}
      {delay_field(form)}
      {flag_controls(form)}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Custom flag terms</label>
        <textarea name="custom_terms">{h(form.get("custom_terms", ""))}</textarea>
      </div>
    </div>
    """


def max_field(form: dict[str, Any], name: str, label: str, default: int, max_value: int) -> str:
    return f"""
    <div class="row">
      <label>{h(label)}</label>
      <input type="number" name="{h(name)}" min="1" max="{max_value}" value="{h(form.get(name, str(default)))}">
    </div>
    """


def _platforms(raw: Any) -> list[str]:
    platforms = []
    for item in str(raw or "FACEBOOK,INSTAGRAM").replace(";", ",").split(","):
        item = item.strip().upper()
        if item:
            platforms.append(item)
    return platforms or ["FACEBOOK", "INSTAGRAM"]


def _settings(form: dict[str, Any]) -> dict[str, Any]:
    settings = include_settings(form)
    terms = parse_terms(form.get("search_terms", ""))
    page_ids = normalize_page_ids(form.get("page_ids") or form.get("search_page_ids"))
    mode = str(form.get("query_mode") or "keywords_only")
    if mode == "page_ids_only" and not page_ids:
        raise ValueError("Page ID watch mode requires at least one Page ID.")
    if mode in {"keywords_only", "advertiser_discovery"} and not terms:
        raise ValueError("Keyword mode requires at least one search term.")
    if mode == "page_ids_plus_keywords" and (not page_ids or not terms):
        raise ValueError("Combination search requires Page IDs and search terms.")
    country = str(form.get("country") or "US").strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise ValueError("Reached country must be a two-letter country code such as US.")
    settings.update(
        {
            "access_token": str(form.get("access_token") or "").strip() or None,
            "terms": terms,
            "page_ids": page_ids,
            "query_mode": mode,
            "country": country,
            "ad_type": form.get("ad_type") or "POLITICAL_AND_ISSUE_ADS",
            "active_status": form.get("active_status") or "ALL",
            "publisher_platforms": _platforms(form.get("publisher_platforms")),
            "languages": [lang.strip() for lang in str(form.get("languages") or "").replace(";", ",").split(",") if lang.strip()],
            "date_min": str(form.get("date_min") or "").strip(),
            "date_max": str(form.get("date_max") or "").strip(),
            "max_results": parse_int(form.get("max_results", 250), 250, 1, 5000),
            "delay": 0.0,
        }
    )
    try:
        settings["delay"] = max(0.0, min(float(form.get("delay") or 0.0), 10.0))
    except Exception:
        settings["delay"] = 0.0
    return settings


def _join(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        return " || ".join(_join(item) for item in value if item not in (None, ""))
    if isinstance(value, dict):
        return compact_json(value)
    return str(value)


def _ad_text(ad: dict[str, Any]) -> str:
    return "\n".join(
        piece
        for piece in [
            _join(ad.get("ad_creative_bodies")),
            _join(ad.get("ad_creative_link_titles")),
            _join(ad.get("ad_creative_link_captions")),
            _join(ad.get("ad_creative_link_descriptions")),
            _join(ad.get("page_name")),
            _join(ad.get("funding_entity")),
        ]
        if piece
    )


def _query_plan(settings: dict[str, Any]) -> Iterator[tuple[str, list[str]]]:
    mode = settings["query_mode"]
    terms = settings["terms"]
    page_ids = settings["page_ids"]
    page_chunks = list(chunk_page_ids(page_ids)) if page_ids else [[]]
    if mode == "page_ids_only":
        for chunk in page_chunks:
            yield "", chunk
    elif mode == "page_ids_plus_keywords":
        for chunk in page_chunks:
            for term in terms:
                yield term, chunk
    else:
        for term in terms:
            yield term, []


def _params(settings: dict[str, Any], term: str, page_ids: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "fields": ",".join(AD_FIELDS),
        "ad_reached_countries": json.dumps([settings["country"]]),
        "ad_type": settings["ad_type"],
        "ad_active_status": settings["active_status"],
        "publisher_platforms": json.dumps(settings["publisher_platforms"]),
    }
    if term:
        params["search_terms"] = term
        params["search_type"] = "KEYWORD_UNORDERED"
    if page_ids:
        params["search_page_ids"] = json.dumps([int(pid) for pid in page_ids])
    if settings["date_min"]:
        params["ad_delivery_date_min"] = settings["date_min"]
    if settings["date_max"]:
        params["ad_delivery_date_max"] = settings["date_max"]
    if settings["languages"]:
        params["languages"] = json.dumps(settings["languages"])
    return params


def _row(ad: dict[str, Any], settings: dict[str, Any], source_query: str, term: str) -> dict[str, Any]:
    text = _ad_text(ad)
    flag_settings = dict(settings)
    flag_settings["custom_terms"] = list(settings.get("custom_terms") or []) + ([term] if term else [])
    categories, matched = maybe_flag(text, flag_settings)
    ad_id = str(ad.get("id") or "")
    return core_row(
        source_platform="Meta Ad Library",
        source_api="graph.ads_archive",
        source_type=META["source_type"],
        target_input=source_query,
        query_text=source_query,
        flag_categories=categories,
        matched_terms=matched,
        created_at=ad.get("ad_creation_time") or ad.get("ad_delivery_start_time") or "",
        author_id=str(ad.get("page_id") or ""),
        author_display_name=str(ad.get("page_name") or ""),
        canonical_url=build_ad_snapshot_url(ad_id),
        text=text,
        media_summary="ad creative",
        metrics_json={"spend": ad.get("spend"), "impressions": ad.get("impressions")},
        raw_json=ad,
        platform_item_id=ad_id,
        ad_library_id=ad_id,
        ad_library_public_url=build_ad_snapshot_url(ad_id),
        page_id=ad.get("page_id") or "",
        page_name=ad.get("page_name") or "",
        ad_creation_time=ad.get("ad_creation_time") or "",
        ad_delivery_start_time=ad.get("ad_delivery_start_time") or "",
        ad_delivery_stop_time=ad.get("ad_delivery_stop_time") or "",
        publisher_platforms=_join(ad.get("publisher_platforms")),
        funding_entity=_join(ad.get("funding_entity")),
        currency=ad.get("currency") or "",
        spend=compact_json(ad.get("spend")),
        impressions=compact_json(ad.get("impressions")),
        ad_creative_bodies=_join(ad.get("ad_creative_bodies")),
        ad_creative_link_titles=_join(ad.get("ad_creative_link_titles")),
        ad_creative_link_captions=_join(ad.get("ad_creative_link_captions")),
        ad_creative_link_descriptions=_join(ad.get("ad_creative_link_descriptions")),
        demographic_distribution=compact_json(ad.get("demographic_distribution")),
        delivery_by_region=compact_json(ad.get("delivery_by_region")),
        ad_snapshot_url=ad.get("ad_snapshot_url") or "",
    )


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = _settings(form)
    client = MetaAdLibraryClient(settings["access_token"])
    seen_ids: set[str] = set()
    for term, page_ids in _query_plan(settings):
        source_query = compact_json(
            {
                "mode": settings["query_mode"],
                "term": term,
                "country": settings["country"],
                "page_ids": page_ids,
                "platforms": settings["publisher_platforms"],
            }
        )
        for ad in client.iter_ads(_params(settings, term, page_ids), max_results=settings["max_results"], delay=settings["delay"]):
            ad_id = str(ad.get("id") or "")
            if ad_id and ad_id in seen_ids:
                continue
            if ad_id:
                seen_ids.add(ad_id)
            row = _row(ad, settings, source_query, term)
            if keep_row(row, settings):
                yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "Meta Ad Library API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "Meta Ad Library API", form, lambda: iter_row_dicts(form), HEADERS)
