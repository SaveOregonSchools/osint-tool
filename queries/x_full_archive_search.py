from __future__ import annotations

from typing import Any, Iterator

from common import get_form_bool, h
from providers.x_api import XApiClient
from queries._shared import export_core, keep_row, run_core
from queries._x_common import TWEET_HEADERS, base_tweet_params, build_recent_query, max_results_field, tweet_row, x_flag_controls, x_settings, x_time_fields, x_token_field


HEADERS = TWEET_HEADERS

META = {
    "key": "x_full_archive_search",
    "name": "X - Full Archive Search",
    "description": "Search the X full archive endpoint when your X access tier supports /2/tweets/search/all.",
    "source_type": "official_api",
    "limitations": [
        "Full archive access requires the appropriate X paid/self-serve or enterprise access.",
        "Replies are reconstructed with search operators such as conversation_id, to:, and mentions; there is no separate comments API.",
        "Use dry run to inspect query strings before spending read credits.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    exclude_retweets = "checked" if get_form_bool(form, "exclude_retweets", True) else ""
    exclude_replies = "checked" if get_form_bool(form, "exclude_replies", False) else ""
    dry_run = "checked" if get_form_bool(form, "dry_run", False) else ""
    return f"""
    <div class="grid">
      {x_token_field()}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Query terms</label>
        <textarea name="query_terms" placeholder='"vote for"&#10;endorse&#10;ballot measure'>{h(form.get("query_terms", ""))}</textarea>
      </div>
      <div class="row"><label>From handles</label><textarea name="from_handles">{h(form.get("from_handles", ""))}</textarea></div>
      <div class="row"><label>Mentions</label><textarea name="mentions">{h(form.get("mentions", ""))}</textarea></div>
      <div class="row"><label>Conversation ID</label><input type="text" name="conversation_id" value="{h(form.get("conversation_id", ""))}"></div>
      {x_time_fields(form)}
      {max_results_field(form, 100)}
      <div class="row">
        <label><input type="checkbox" name="exclude_retweets" {exclude_retweets}> Exclude retweets</label>
        <label><input type="checkbox" name="exclude_replies" {exclude_replies}> Exclude replies</label>
        <label><input type="checkbox" name="dry_run" {dry_run}> Dry run only</label>
      </div>
      {x_flag_controls(form)}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Custom flag terms</label>
        <textarea name="custom_terms">{h(form.get("custom_terms", ""))}</textarea>
      </div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = x_settings(form)
    query = build_recent_query(form)
    if get_form_bool(form, "dry_run", False):
        from osint_common import core_row

        yield core_row(
            source_platform="X",
            source_api="dry-run /2/tweets/search/all",
            source_type=META["source_type"],
            target_input=query,
            query_text=query,
            notes=f"Dry run query. Max results would be {settings['max_results']}.",
            raw_json={"query": query, "max_results": settings["max_results"]},
        )
        return
    client = XApiClient(settings["bearer_token"])
    params = base_tweet_params(form)
    for tweet in client.iter_search(query, endpoint="tweets/search/all", max_results=settings["max_results"], params=params):
        row = tweet_row(tweet, source_api="GET /2/tweets/search/all", source_type=META["source_type"], target_input=query, query_text=query, settings=settings)
        if keep_row(row, settings):
            yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "X API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "X API", form, lambda: iter_row_dicts(form), HEADERS)
