from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import urlencode

from common import get_form_bool, h
from osint_common import CORE_HEADERS, core_row
from queries._shared import export_core, run_core


HEADERS = CORE_HEADERS + ["provider_name", "is_official_api", "manual_search_url", "verified_domain", "region", "political_only"]

META = {
    "key": "google_ads_transparency_link_builder",
    "name": "Google - Ads Transparency Link Builder",
    "description": "Generate manual Google Ads Transparency Center investigation links and capture pasted notes without using unofficial scraping.",
    "source_type": "manual_entry",
    "coverage": "Manual investigation helper for Google Ads Transparency Center.",
    "limitations": [
        "There is no broadly documented official API for arbitrary active Google Ads Transparency Center scraping.",
        "Manual links are leads; preserve screenshots and raw page context separately.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    political = "checked" if get_form_bool(form, "political_only", True) else ""
    return f"""
    <div class="grid">
      <div class="row">
        <label>Advertiser name</label>
        <input type="text" name="advertiser_name" value="{h(form.get("advertiser_name", ""))}">
      </div>
      <div class="row">
        <label>Verified domain</label>
        <input type="text" name="verified_domain" value="{h(form.get("verified_domain", ""))}" placeholder="example.org">
      </div>
      <div class="row">
        <label>Region</label>
        <input type="text" name="region" value="{h(form.get("region", "US"))}" placeholder="US">
      </div>
      <div class="row">
        <label><input type="checkbox" name="political_only" {political}> Political ads focus</label>
      </div>
      <div class="row" style="grid-column: 1 / -1;">
        <label>Optional manually captured notes/data</label>
        <textarea name="manual_notes" placeholder="Paste investigator notes, visible advertiser ID, or copied creative details.">{h(form.get("manual_notes", ""))}</textarea>
      </div>
    </div>
    """


def _link(form: dict[str, Any]) -> str:
    params = {}
    query = str(form.get("advertiser_name") or form.get("verified_domain") or "").strip()
    region = str(form.get("region") or "US").strip().upper()
    if region:
        params["region"] = region
    if query:
        params["q"] = query
    return "https://adstransparency.google.com/" + (f"?{urlencode(params)}" if params else "")


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    query = str(form.get("advertiser_name") or form.get("verified_domain") or "").strip()
    if not query:
        raise ValueError("Enter an advertiser name or verified domain.")
    url = _link(form)
    notes = str(form.get("manual_notes") or "")
    yield core_row(
        source_platform="Google Ads",
        source_api="manual Ads Transparency Center link",
        source_type=META["source_type"],
        target_input=query,
        query_text=query,
        flag_categories="ad_transparency_review",
        matched_terms=query,
        canonical_url=url,
        text=notes,
        media_summary="manual investigation link",
        raw_json={"manual_search_url": url, "notes": notes},
        provider_name="Google Ads Transparency Center",
        is_official_api="false",
        manual_search_url=url,
        verified_domain=form.get("verified_domain") or "",
        region=form.get("region") or "",
        political_only="yes" if get_form_bool(form, "political_only", True) else "no",
        notes="Manual link builder; no scraping performed.",
    )


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "Google Ads Transparency Center manual", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "Google Ads Transparency Center manual", form, lambda: iter_row_dicts(form), HEADERS)
