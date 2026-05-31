from __future__ import annotations

import time
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
    "key": "bluesky_author_feed_scan",
    "name": "Bluesky — scan account feeds for lobbying/candidate flags",
    "description": "Enter one or more Bluesky handles/DIDs, fetch public posts/reposts from each author feed, apply local keyword/pattern flags, and export the evidence log.",
    "headers": POST_HEADERS,
}

HEADERS = POST_HEADERS


def render_fields(form: dict[str, Any]) -> str:
    actors = h(form.get("actors", ""))
    custom_terms = h(form.get("custom_terms", ""))
    max_posts = h(form.get("max_posts", "250"))
    since = h(form.get("since", ""))
    until = h(form.get("until", ""))
    delay = h(form.get("delay", str(DEFAULT_DELAY_SECONDS)))
    feed_filter = form.get("feed_filter", "posts_with_replies")
    include_candidate = "checked" if get_form_bool(form, "include_candidate", True) else ""
    include_lobbying = "checked" if get_form_bool(form, "include_lobbying", True) else ""
    only_flagged = "checked" if get_form_bool(form, "only_flagged", True) else ""
    include_status = "checked" if get_form_bool(form, "include_status_rows", False) else ""

    def opt(value: str, label: str) -> str:
        return f'<option value="{h(value)}" {"selected" if feed_filter == value else ""}>{h(label)}</option>'

    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;">
        <label>Bluesky handles, DIDs, or bsky.app profile URLs</label>
        <textarea name="actors" placeholder="example.bsky.social\ndid:plc:...\nhttps://bsky.app/profile/example.bsky.social">{actors}</textarea>
        <div class="subtle">One per line is safest. @handles and bsky.app/profile URLs are accepted.</div>
      </div>
      <div class="row">
        <label>Since date</label>
        <input type="date" name="since" value="{since}">
      </div>
      <div class="row">
        <label>Until date</label>
        <input type="date" name="until" value="{until}">
        <div class="subtle">Inclusive through this date.</div>
      </div>
      <div class="row">
        <label>Max feed items per account</label>
        <input type="number" name="max_posts" min="1" max="5000" value="{max_posts}">
      </div>
      <div class="row">
        <label>Author feed filter</label>
        <select name="feed_filter">
          {opt("posts_with_replies", "Posts with replies")}
          {opt("posts_no_replies", "Posts only, no replies")}
          {opt("posts_with_media", "Posts with media")}
          {opt("posts_and_author_threads", "Posts and author threads")}
          {opt("posts_with_video", "Posts with video")}
        </select>
      </div>
      <div class="row" style="grid-column: 1 / -1;">
        <label>Custom search terms / names / bill numbers</label>
        <textarea name="custom_terms" placeholder="Candidate Name\n@candidatehandle\nHB 1234\nProposition 1">{custom_terms}</textarea>
        <div class="subtle">These are matched locally against post text and embed summaries. Include candidate names, race names, bills, ballot measures, slogans, or campaign hashtags.</div>
      </div>
      <div class="row">
        <label><input type="checkbox" name="include_candidate" {include_candidate}> Candidate-intervention review patterns</label>
        <div class="subtle">Examples: vote for, defeat, endorse, campaign, donate to candidate.</div>
      </div>
      <div class="row">
        <label><input type="checkbox" name="include_lobbying" {include_lobbying}> Lobbying review patterns</label>
        <div class="subtle">Examples: contact lawmakers, support/oppose bill, ballot measure language.</div>
      </div>
      <div class="row">
        <label><input type="checkbox" name="only_flagged" {only_flagged}> Only return flagged or custom-term matches</label>
        <div class="subtle">Uncheck to export every fetched public feed item.</div>
      </div>
      <div class="row">
        <label><input type="checkbox" name="include_status_rows" {include_status}> Include status/error rows</label>
        <div class="subtle">Useful when testing many handles and you want the CSV to show accounts with no hits.</div>
      </div>
      <div class="row">
        <label>Delay between API calls, seconds</label>
        <input type="number" step="0.05" min="0" max="10" name="delay" value="{delay}">
      </div>
    </div>
    """


def _settings(form: dict[str, Any]) -> dict[str, Any]:
    try:
        delay = max(0.0, min(float(form.get("delay") or DEFAULT_DELAY_SECONDS), 10.0))
    except Exception:
        delay = DEFAULT_DELAY_SECONDS
    return {
        "actors": parse_actor_inputs(form.get("actors", "")),
        "custom_terms": parse_terms(form.get("custom_terms", "")),
        "include_candidate": get_form_bool(form, "include_candidate", True),
        "include_lobbying": get_form_bool(form, "include_lobbying", True),
        "only_flagged": get_form_bool(form, "only_flagged", True),
        "include_status_rows": get_form_bool(form, "include_status_rows", False),
        "since_dt": parse_date_start(form.get("since", "")),
        "until_dt": parse_date_until_exclusive(form.get("until", "")),
        "max_posts": parse_int(form.get("max_posts", 250), 250, 1, 5000),
        "delay": delay,
        "feed_filter": form.get("feed_filter") or "posts_with_replies",
    }


def _status_row(actor: str, note: str) -> list[Any]:
    row = [""] * len(HEADERS)
    row[0] = "Bluesky"
    row[1] = actor
    row[2] = "author_feed_scan"
    row[-1] = note
    return row


def iter_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    settings = _settings(form)
    actors = settings["actors"]
    if not actors:
        raise ValueError("Enter at least one Bluesky handle, DID, or profile URL.")

    session = make_session()
    query_label = "author_feed:" + settings["feed_filter"]

    for actor in actors:
        rows_for_actor = 0
        scanned_for_actor = 0
        cursor = None
        seen_uris: set[str] = set()
        try:
            while scanned_for_actor < settings["max_posts"]:
                limit = min(100, settings["max_posts"] - scanned_for_actor)
                params: dict[str, Any] = {"actor": actor, "limit": limit, "filter": settings["feed_filter"]}
                if cursor:
                    params["cursor"] = cursor
                data = xrpc_get("app.bsky.feed.getAuthorFeed", params=params, session=session)
                feed = data.get("feed") or []
                if not feed:
                    break

                for item in feed:
                    scanned_for_actor += 1
                    post = item.get("post") if isinstance(item, dict) else None
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
                        actor_input=actor,
                        query=query_label,
                        custom_terms=settings["custom_terms"],
                        include_candidate=settings["include_candidate"],
                        include_lobbying=settings["include_lobbying"],
                        source_api="app.bsky.feed.getAuthorFeed",
                        reason=item.get("reason") if isinstance(item, dict) else None,
                    )
                    is_flagged = bool(row[3] or row[4])
                    if settings["only_flagged"] and not is_flagged:
                        continue
                    rows_for_actor += 1
                    yield row

                    if scanned_for_actor >= settings["max_posts"]:
                        break

                cursor = data.get("cursor")
                if not cursor:
                    break
                if settings["delay"]:
                    time.sleep(settings["delay"])

            if settings["include_status_rows"] and rows_for_actor == 0:
                yield _status_row(actor, f"No matching rows. Scanned up to {scanned_for_actor} feed items.")
        except OsintApiError as exc:
            if settings["include_status_rows"]:
                yield _status_row(actor, f"API error: {exc}; status={exc.status_code}; body={exc.body or ''}")
            else:
                raise


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return HEADERS, list(iter_rows(form))


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from iter_rows(form)
