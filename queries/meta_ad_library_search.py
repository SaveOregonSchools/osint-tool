from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from common import (
    DEFAULT_DELAY_SECONDS,
    HTTP_TIMEOUT,
    OsintApiError,
    classify_text,
    get_form_bool,
    h,
    make_session,
    parse_int,
    parse_terms,
)

GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v25.0").strip() or "v25.0"
GRAPH_BASE = os.getenv("META_GRAPH_BASE", "https://graph.facebook.com").rstrip("/")
ENV_TOKEN = "META_AD_LIBRARY_ACCESS_TOKEN"

AD_FIELDS = [
    "id",
    "ad_creation_time",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "ad_creative_link_titles",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_snapshot_url",
    "bylines",
    "currency",
    "demographic_distribution",
    "delivery_by_region",
    "impressions",
    "page_id",
    "page_name",
    "publisher_platforms",
    "spend",
]

HEADERS = [
    "platform",
    "source_query",
    "flag_categories",
    "matched_terms",
    "ad_library_id",
    "ad_library_public_url",
    "page_id",
    "page_name",
    "ad_creation_time",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "publisher_platforms",
    "byline",
    "currency",
    "spend_lower",
    "spend_upper",
    "spend_raw",
    "impressions_lower",
    "impressions_upper",
    "impressions_raw",
    "ad_creative_bodies",
    "ad_creative_link_titles",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "demographic_distribution",
    "delivery_by_region",
    "ad_snapshot_url_redacted",
    "captured_at_utc",
    "source_api",
    "notes",
]

META = {
    "key": "meta_ad_library_search",
    "name": "Meta — Ad Library search (Facebook/Instagram)",
    "description": (
        "Search the official Meta Ad Library API for public ads, including ads delivered on "
        "Facebook and/or Instagram. Best for issue/election/political ad review and page-based "
        "ad monitoring. Requires a free Meta developer app token with Ad Library API access."
    ),
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    token_present = bool(os.getenv(ENV_TOKEN))
    token_note = (
        f"Environment variable {ENV_TOKEN} is set; leave token blank here."
        if token_present
        else f"Set {ENV_TOKEN} in your .env/shell or paste a token for this one request."
    )
    search_terms = h(form.get("search_terms", ""))
    search_page_ids = h(form.get("search_page_ids", ""))
    country = h(form.get("country", "US"))
    date_min = h(form.get("ad_delivery_date_min", ""))
    date_max = h(form.get("ad_delivery_date_max", ""))
    max_results = h(form.get("max_results_per_query", "250"))
    delay = h(form.get("delay", str(DEFAULT_DELAY_SECONDS)))
    token_value = ""  # Do not echo secrets back into rendered HTML.

    ad_type = form.get("ad_type", "POLITICAL_AND_ISSUE_ADS")
    active_status = form.get("ad_active_status", "ALL")
    search_type = form.get("search_type", "KEYWORD_UNORDERED")

    fb_checked = "checked" if get_form_bool(form, "platform_facebook", True) else ""
    ig_checked = "checked" if get_form_bool(form, "platform_instagram", True) else ""
    threads_checked = "checked" if get_form_bool(form, "platform_threads", False) else ""
    candidate_checked = "checked" if get_form_bool(form, "include_candidate", True) else ""
    lobbying_checked = "checked" if get_form_bool(form, "include_lobbying", True) else ""
    only_flagged_checked = "checked" if get_form_bool(form, "only_flagged", False) else ""
    status_checked = "checked" if get_form_bool(form, "include_status_rows", True) else ""

    def opt(current: str, value: str, label: str) -> str:
        return f'<option value="{h(value)}" {"selected" if current == value else ""}>{h(label)}</option>'

    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;">
        <label>Meta Ad Library API access token</label>
        <input type="password" name="access_token" value="{token_value}" autocomplete="off" placeholder="Optional if {ENV_TOKEN} is set">
        <div class="subtle">{h(token_note)} This module never logs in, scrapes private pages, or bypasses platform controls.</div>
      </div>

      <div class="row" style="grid-column: 1 / -1;">
        <label>Search terms</label>
        <textarea name="search_terms" placeholder="school board\nPAC name\norganization name\nballot measure">{search_terms}</textarea>
        <div class="subtle">One term or phrase per line. Meta limits search_terms to 100 characters. Leave blank for page-ID-only searches.</div>
      </div>

      <div class="row" style="grid-column: 1 / -1;">
        <label>Optional Facebook Page IDs</label>
        <textarea name="search_page_ids" placeholder="123456789\n987654321">{search_page_ids}</textarea>
        <div class="subtle">Limits results to specific advertisers/pages. Use Page IDs from Meta Ad Library or previous exports. Meta allows up to 10 IDs per API query.</div>
      </div>

      <div class="row">
        <label>Reached country</label>
        <input type="text" name="country" maxlength="2" value="{country}" placeholder="US">
        <div class="subtle">Two-letter country code, e.g. US. Required by the Ad Library API.</div>
      </div>

      <div class="row">
        <label>Ad type</label>
        <select name="ad_type">
          {opt(ad_type, "POLITICAL_AND_ISSUE_ADS", "Political and issue ads")}
          {opt(ad_type, "ALL", "All available ad types")}
          {opt(ad_type, "HOUSING_ADS", "Housing ads")}
          {opt(ad_type, "EMPLOYMENT_ADS", "Employment ads")}
          {opt(ad_type, "FINANCIAL_PRODUCTS_AND_SERVICES_ADS", "Financial products/services ads")}
        </select>
      </div>

      <div class="row">
        <label>Active status</label>
        <select name="ad_active_status">
          {opt(active_status, "ALL", "All")}
          {opt(active_status, "ACTIVE", "Active")}
          {opt(active_status, "INACTIVE", "Inactive")}
        </select>
      </div>

      <div class="row">
        <label>Search type</label>
        <select name="search_type">
          {opt(search_type, "KEYWORD_UNORDERED", "Keyword unordered")}
          {opt(search_type, "KEYWORD_EXACT_PHRASE", "Exact phrase")}
        </select>
        <div class="subtle">Used only when Search terms is filled. Ignored for page-ID-only searches.</div>
      </div>

      <div class="row">
        <label>Delivery date min</label>
        <input type="date" name="ad_delivery_date_min" value="{date_min}">
      </div>

      <div class="row">
        <label>Delivery date max</label>
        <input type="date" name="ad_delivery_date_max" value="{date_max}">
      </div>

      <div class="row">
        <label>Max results per query</label>
        <input type="number" name="max_results_per_query" min="1" max="5000" value="{max_results}">
      </div>

      <div class="row">
        <label>Delay between API calls, seconds</label>
        <input type="number" step="0.05" min="0" max="10" name="delay" value="{delay}">
      </div>

      <div class="row">
        <label><input type="checkbox" name="platform_facebook" {fb_checked}> Facebook placements</label>
        <label><input type="checkbox" name="platform_instagram" {ig_checked}> Instagram placements</label>
        <label><input type="checkbox" name="platform_threads" {threads_checked}> Threads placements</label>
      </div>

      <div class="row">
        <label><input type="checkbox" name="include_candidate" {candidate_checked}> Candidate-intervention review patterns</label>
        <div class="subtle">Examples: vote for, defeat, endorse, campaign, donate to candidate.</div>
      </div>

      <div class="row">
        <label><input type="checkbox" name="include_lobbying" {lobbying_checked}> Lobbying review patterns</label>
        <div class="subtle">Examples: contact lawmakers, support/oppose bill, ballot measure language.</div>
      </div>

      <div class="row">
        <label><input type="checkbox" name="only_flagged" {only_flagged_checked}> Only return flagged or term-matched ads</label>
        <div class="subtle">Leave unchecked to export all API results for the supplied searches.</div>
      </div>

      <div class="row">
        <label><input type="checkbox" name="include_status_rows" {status_checked}> Include status/error rows</label>
      </div>
    </div>
    """


def _access_token(form: dict[str, Any]) -> str:
    return str(form.get("access_token") or os.getenv(ENV_TOKEN) or "").strip()


def _delay(form: dict[str, Any]) -> float:
    try:
        return max(0.0, min(float(form.get("delay") or DEFAULT_DELAY_SECONDS), 10.0))
    except Exception:
        return DEFAULT_DELAY_SECONDS


def _parse_page_ids(raw: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    normalized = (raw or "").replace(",", " ").replace(";", " ")
    for token in normalized.split():
        token = token.strip()
        if token.isdigit() and token not in seen:
            out.append(token)
            seen.add(token)
    return out[:10]


def _selected_platforms(form: dict[str, Any]) -> list[str]:
    platforms: list[str] = []
    if get_form_bool(form, "platform_facebook", True):
        platforms.append("FACEBOOK")
    if get_form_bool(form, "platform_instagram", True):
        platforms.append("INSTAGRAM")
    if get_form_bool(form, "platform_threads", False):
        platforms.append("THREADS")
    return platforms


def _settings(form: dict[str, Any]) -> dict[str, Any]:
    terms = parse_terms(form.get("search_terms", ""))
    page_ids = _parse_page_ids(form.get("search_page_ids", ""))
    if not terms and not page_ids:
        raise ValueError("Enter at least one search term or at least one Facebook Page ID.")

    too_long = [term for term in terms if len(term) > 100]
    if too_long:
        sample = too_long[0]
        raise ValueError(f"Meta limits search_terms to 100 characters. Shorten this term: {sample[:140]}")

    platforms = _selected_platforms(form)
    if not platforms:
        raise ValueError("Choose at least one publisher platform: Facebook, Instagram, or Threads.")

    country = str(form.get("country") or "US").strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise ValueError("Reached country must be a two-letter country code such as US.")

    return {
        "access_token": _access_token(form),
        "terms": terms,
        "page_ids": page_ids,
        "country": country,
        "ad_type": form.get("ad_type") or "POLITICAL_AND_ISSUE_ADS",
        "ad_active_status": form.get("ad_active_status") or "ALL",
        "search_type": form.get("search_type") or "KEYWORD_UNORDERED",
        "platforms": platforms,
        "date_min": str(form.get("ad_delivery_date_min") or "").strip(),
        "date_max": str(form.get("ad_delivery_date_max") or "").strip(),
        "max_results_per_query": parse_int(form.get("max_results_per_query", 250), 250, 1, 5000),
        "include_candidate": get_form_bool(form, "include_candidate", True),
        "include_lobbying": get_form_bool(form, "include_lobbying", True),
        "only_flagged": get_form_bool(form, "only_flagged", False),
        "include_status_rows": get_form_bool(form, "include_status_rows", True),
        "delay": _delay(form),
    }


def _api_url() -> str:
    return f"{GRAPH_BASE}/{GRAPH_API_VERSION}/ads_archive"


def _compact_json(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return str(value)


def _join(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        return " || ".join(_join(item) for item in value if item not in (None, ""))
    if isinstance(value, dict):
        return _compact_json(value)
    return str(value)


def _range_bounds(value: Any) -> tuple[str, str, str]:
    if not isinstance(value, dict):
        return "", "", _join(value)
    lower = value.get("lower_bound", value.get("lower", ""))
    upper = value.get("upper_bound", value.get("upper", ""))
    return str(lower) if lower != "" else "", str(upper) if upper != "" else "", _compact_json(value)


def _redact_access_token(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() == "access_token":
                query.append((key, "REDACTED"))
            else:
                query.append((key, value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except Exception:
        return url.replace("access_token=", "access_token=REDACTED_")


def _status_row(source_query: str, note: str) -> list[Any]:
    row = [""] * len(HEADERS)
    row[0] = "Meta Ad Library"
    row[1] = source_query
    row[-1] = note
    return row


def _graph_get(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    response = session.get(_api_url(), params=params, timeout=HTTP_TIMEOUT)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            wait = min(float(retry_after), 30.0) if retry_after else 3.0
        except ValueError:
            wait = 3.0
        time.sleep(wait)
        response = session.get(_api_url(), params=params, timeout=HTTP_TIMEOUT)

    if not response.ok:
        body = response.text[:1000] if response.text else ""
        raise OsintApiError(
            f"Meta Ad Library request failed: HTTP {response.status_code}",
            status_code=response.status_code,
            url=response.url,
            body=body,
        )

    try:
        return response.json()
    except Exception as exc:
        raise OsintApiError(
            f"Meta Ad Library response was not valid JSON: {exc}",
            status_code=response.status_code,
            url=response.url,
            body=response.text[:1000],
        ) from exc


def _base_params(settings: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "access_token": settings["access_token"],
        "fields": ",".join(AD_FIELDS),
        "ad_reached_countries": json.dumps([settings["country"]]),
        "ad_type": settings["ad_type"],
        "ad_active_status": settings["ad_active_status"],
        "publisher_platforms": json.dumps(settings["platforms"]),
    }
    # Meta's `search_type` parameter applies only to keyword searches.
    # Sending it with no `search_terms` causes HTTP 400 for page-ID-only queries.
    if settings.get("terms"):
        params["search_type"] = settings["search_type"]
    if settings.get("page_ids"):
        params["search_page_ids"] = json.dumps([int(pid) for pid in settings["page_ids"]])
    if settings.get("date_min"):
        params["ad_delivery_date_min"] = settings["date_min"]
    if settings.get("date_max"):
        params["ad_delivery_date_max"] = settings["date_max"]
    return params


def _source_query(settings: dict[str, Any], term: str) -> str:
    bits = [
        f"term={term or '[blank]'}",
        f"country={settings['country']}",
        f"ad_type={settings['ad_type']}",
        f"active={settings['ad_active_status']}",
        f"platforms={','.join(settings['platforms'])}",
    ]
    if settings.get("page_ids"):
        bits.append(f"page_ids={','.join(settings['page_ids'])}")
    if settings.get("date_min"):
        bits.append(f"date_min={settings['date_min']}")
    if settings.get("date_max"):
        bits.append(f"date_max={settings['date_max']}")
    return " | ".join(bits)


def _ad_text(ad: dict[str, Any]) -> str:
    pieces = [
        _join(ad.get("ad_creative_bodies")),
        _join(ad.get("ad_creative_link_titles")),
        _join(ad.get("ad_creative_link_captions")),
        _join(ad.get("ad_creative_link_descriptions")),
        _join(ad.get("page_name")),
        _join(ad.get("bylines")),
    ]
    return "\n".join(piece for piece in pieces if piece)


def _ad_to_row(ad: dict[str, Any], settings: dict[str, Any], source_query: str, term: str) -> list[Any]:
    text = _ad_text(ad)
    custom_terms = [t for t in [term] if t]
    categories, matched = classify_text(
        text,
        custom_terms=custom_terms,
        include_candidate=settings["include_candidate"],
        include_lobbying=settings["include_lobbying"],
    )

    spend_lower, spend_upper, spend_raw = _range_bounds(ad.get("spend"))
    impr_lower, impr_upper, impr_raw = _range_bounds(ad.get("impressions"))
    ad_id = str(ad.get("id") or "")
    public_url = f"https://www.facebook.com/ads/library/?id={ad_id}" if ad_id else ""

    notes = ""
    snapshot_url = _redact_access_token(str(ad.get("ad_snapshot_url") or ""))
    if snapshot_url and "access_token=REDACTED" in snapshot_url:
        notes = "ad_snapshot_url access_token redacted for safe CSV sharing"

    return [
        "Meta Ad Library",
        source_query,
        categories,
        matched,
        ad_id,
        public_url,
        ad.get("page_id") or "",
        ad.get("page_name") or "",
        ad.get("ad_creation_time") or "",
        ad.get("ad_delivery_start_time") or "",
        ad.get("ad_delivery_stop_time") or "",
        _join(ad.get("publisher_platforms")),
        _join(ad.get("bylines")),
        ad.get("currency") or "",
        spend_lower,
        spend_upper,
        spend_raw,
        impr_lower,
        impr_upper,
        impr_raw,
        _join(ad.get("ad_creative_bodies")),
        _join(ad.get("ad_creative_link_titles")),
        _join(ad.get("ad_creative_link_captions")),
        _join(ad.get("ad_creative_link_descriptions")),
        _compact_json(ad.get("demographic_distribution")),
        _compact_json(ad.get("delivery_by_region")),
        snapshot_url,
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        f"{GRAPH_API_VERSION}/ads_archive",
        notes,
    ]


def iter_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    settings = _settings(form)
    if not settings["access_token"]:
        raise ValueError(f"Set {ENV_TOKEN} or paste a Meta Ad Library API access token.")

    session = make_session()
    base_params = _base_params(settings)
    seen_ids: set[str] = set()

    for term in (settings["terms"] or [""]):
        source_query = _source_query(settings, term)
        returned = 0
        yielded = 0
        after = None

        while returned < settings["max_results_per_query"]:
            params = dict(base_params)
            params["limit"] = min(100, settings["max_results_per_query"] - returned)
            if term:
                params["search_terms"] = term
            if after:
                params["after"] = after

            try:
                data = _graph_get(session, params)
            except OsintApiError as exc:
                if settings["include_status_rows"]:
                    yield _status_row(source_query, f"API error: {exc}; status={exc.status_code}; body={exc.body or ''}")
                    break
                raise

            ads = data.get("data") or []
            if not ads:
                break

            for ad in ads:
                returned += 1
                if not isinstance(ad, dict):
                    continue
                ad_id = str(ad.get("id") or "")
                if ad_id and ad_id in seen_ids:
                    continue
                if ad_id:
                    seen_ids.add(ad_id)

                row = _ad_to_row(ad, settings, source_query, term)
                is_flagged = bool(row[2] or row[3])
                if settings["only_flagged"] and not is_flagged:
                    continue
                yielded += 1
                yield row

                if returned >= settings["max_results_per_query"]:
                    break

            paging = data.get("paging") or {}
            cursors = paging.get("cursors") or {}
            after = cursors.get("after")
            if not after:
                break
            if settings["delay"]:
                time.sleep(settings["delay"])

        if settings["include_status_rows"] and yielded == 0:
            yield _status_row(source_query, f"No rows yielded. API returned {returned} ads before filtering/deduplication.")
        if settings["delay"]:
            time.sleep(settings["delay"])


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return HEADERS, list(iter_rows(form))


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from iter_rows(form)
