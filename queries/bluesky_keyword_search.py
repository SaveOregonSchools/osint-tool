from __future__ import annotations

import time
from datetime import timezone
from typing import Any, Iterator

from common import (
    DEFAULT_DELAY_SECONDS,
    OsintApiError,
    POST_HEADERS,
    get_form_bool,
    h,
    make_session,
    parse_actor_inputs,
    parse_date_start,
    parse_date_until_exclusive,
    parse_int,
    parse_terms,
    post_view_to_row,
    selected_query_terms,
    within_date_range,
    xrpc_get,
)


META = {
    "key": "bluesky_keyword_search",
    "name": "Bluesky — API keyword search by account",
    "description": "Search Bluesky public posts with app.bsky.feed.searchPosts, optionally limited to one or more accounts. Best for targeted terms such as candidate names, bill numbers, slogans, or ballot measures.",
    "headers": POST_HEADERS,
}

HEADERS = POST_HEADERS


def render_fields(form: dict[str, Any]) -> str:
    actors = h(form.get("actors", ""))
    custom_terms = h(form.get("custom_terms", ""))
    max_terms = h(form.get("max_terms", "25"))
    max_results = h(form.get("max_results_per_term", "100"))
    since = h(form.get("since", ""))
    until = h(form.get("until", ""))
    delay = h(form.get("delay", str(DEFAULT_DELAY_SECONDS)))
    include_candidate = "checked" if get_form_bool(form, "include_candidate", False) else ""
    include_lobbying = "checked" if get_form_bool(form, "include_lobbying", False) else ""
    include_status = "checked" if get_form_bool(form, "include_status_rows", False) else ""

    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;">
        <label>Optional Bluesky handles, DIDs, or bsky.app profile URLs</label>
        <textarea name="actors" placeholder="example.bsky.social\ndid:plc:...">{actors}</textarea>
        <div class="subtle">Leave blank for a broader Bluesky search. For nonprofit review, account-limited searches are usually cleaner.</div>
      </div>
      <div class="row" style="grid-column: 1 / -1;">
        <label>Search terms / names / bill numbers</label>
        <textarea name="custom_terms" placeholder="Candidate Name\n#CampaignSlogan\nHB 1234\nProposition 1">{custom_terms}</textarea>
        <div class="subtle">The plugin runs one API search per term per account, then deduplicates posts by URI.</div>
      </div>
      <div class="row">
        <label><input type="checkbox" name="include_candidate" {include_candidate}> Add candidate-intervention preset terms</label>
      </div>
      <div class="row">
        <label><input type="checkbox" name="include_lobbying" {include_lobbying}> Add lobbying preset terms</label>
      </div>
      <div class="row">
        <label>Since date</label>
        <input type="date" name="since" value="{since}">
      </div>
      <div class="row">
        <label>Until date</label>
        <input type="date" name="until" value="{until}">
        <div class="subtle">Inclusive through this date; API date parameters are attempted and then locally rechecked.</div>
      </div>
      <div class="row">
        <label>Max terms to run</label>
        <input type="number" name="max_terms" min="1" max="100" value="{max_terms}">
      </div>
      <div class="row">
        <label>Max results per term/account</label>
        <input type="number" name="max_results_per_term" min="1" max="1000" value="{max_results}">
      </div>
      <div class="row">
        <label>Delay between API calls, seconds</label>
        <input type="number" step="0.05" min="0" max="10" name="delay" value="{delay}">
      </div>
      <div class="row">
        <label><input type="checkbox" name="include_status_rows" {include_status}> Include status/error rows</label>
      </div>
    </div>
    """


def _settings(form: dict[str, Any]) -> dict[str, Any]:
    custom_terms = parse_terms(form.get("custom_terms", ""))
    max_terms = parse_int(form.get("max_terms", 25), 25, 1, 100)
    try:
        delay = max(0.0, min(float(form.get("delay") or DEFAULT_DELAY_SECONDS), 10.0))
    except Exception:
        delay = DEFAULT_DELAY_SECONDS
    return {
        "actors": parse_actor_inputs(form.get("actors", "")),
        "custom_terms": custom_terms,
        "include_candidate": get_form_bool(form, "include_candidate", False),
        "include_lobbying": get_form_bool(form, "include_lobbying", False),
        "include_status_rows": get_form_bool(form, "include_status_rows", False),
        "since_dt": parse_date_start(form.get("since", "")),
        "until_dt": parse_date_until_exclusive(form.get("until", "")),
        "since_raw": form.get("since", ""),
        "until_raw": form.get("until", ""),
        "max_results_per_term": parse_int(form.get("max_results_per_term", 100), 100, 1, 1000),
        "max_terms": max_terms,
        "terms": selected_query_terms(get_form_bool(form, "include_candidate", False), get_form_bool(form, "include_lobbying", False), custom_terms, max_terms=max_terms),
        "delay": delay,
    }


def _status_row(actor: str, term: str, note: str) -> list[Any]:
    row = [""] * len(HEADERS)
    row[0] = "Bluesky"
    row[1] = actor
    row[2] = term
    row[-1] = note
    return row


def _api_since_until(settings: dict[str, Any]) -> dict[str, str]:
    params: dict[str, str] = {}
    if settings.get("since_dt"):
        params["since"] = settings["since_dt"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if settings.get("until_dt"):
        # searchPosts treats until as a timestamp; this is the exclusive next-day boundary.
        params["until"] = settings["until_dt"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return params


def iter_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    settings = _settings(form)
    terms = settings["terms"]
    if not terms:
        raise ValueError("Enter at least one custom search term or enable a preset term group.")

    actors: list[str | None] = settings["actors"] or [None]
    session = make_session()
    seen_uris: set[str] = set()

    for actor in actors:
        actor_label = actor or "<global>"
        for term in terms:
            returned_for_query = 0
            cursor = None
            pagination_failed_note = ""
            while returned_for_query < settings["max_results_per_term"]:
                limit = min(100, settings["max_results_per_term"] - returned_for_query)
                params: dict[str, Any] = {"q": term, "limit": limit}
                if actor:
                    params["author"] = actor
                params.update(_api_since_until(settings))
                if cursor:
                    params["cursor"] = cursor

                try:
                    data = xrpc_get("app.bsky.feed.searchPosts", params=params, session=session)
                except OsintApiError as exc:
                    # Some public search configurations have historically been picky about
                    # pagination/date parameters. Retry once without date filters on first page;
                    # local filtering still happens below.
                    if not cursor and ("since" in params or "until" in params):
                        retry_params = {k: v for k, v in params.items() if k not in {"since", "until"}}
                        data = xrpc_get("app.bsky.feed.searchPosts", params=retry_params, session=session)
                        pagination_failed_note = "API rejected date filter; local date filtering applied."
                    elif cursor and exc.status_code in {400, 401, 403}:
                        pagination_failed_note = f"Stopped pagination after API error: HTTP {exc.status_code}."
                        break
                    else:
                        if settings["include_status_rows"]:
                            yield _status_row(actor_label, term, f"API error: {exc}; status={exc.status_code}; body={exc.body or ''}")
                            break
                        raise

                posts = data.get("posts") or []
                if not posts:
                    break

                for post in posts:
                    returned_for_query += 1
                    if not isinstance(post, dict):
                        continue
                    uri = post.get("uri") or ""
                    if uri in seen_uris:
                        continue
                    seen_uris.add(uri)
                    record = post.get("record") or {}
                    created_at = record.get("createdAt") or post.get("indexedAt")
                    if not within_date_range(created_at, settings["since_dt"], settings["until_dt"]):
                        continue
                    row = post_view_to_row(
                        post,
                        actor_input=actor_label,
                        query=term,
                        custom_terms=list(settings["custom_terms"]) + [term],
                        include_candidate=settings["include_candidate"],
                        include_lobbying=settings["include_lobbying"],
                        source_api="app.bsky.feed.searchPosts",
                        notes=pagination_failed_note,
                    )
                    yield row
                    if returned_for_query >= settings["max_results_per_term"]:
                        break

                cursor = data.get("cursor")
                if not cursor:
                    break
                if settings["delay"]:
                    time.sleep(settings["delay"])

            if settings["include_status_rows"] and returned_for_query == 0:
                yield _status_row(actor_label, term, "No API results returned for this term/account.")
            if settings["delay"]:
                time.sleep(settings["delay"])


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return HEADERS, list(iter_rows(form))


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from iter_rows(form)
