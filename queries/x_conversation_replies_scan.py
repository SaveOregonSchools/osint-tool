from __future__ import annotations

from typing import Any, Iterator

from common import h, parse_int
from providers.x_api import XApiClient
from queries._shared import export_core, keep_row, parse_lines, run_core
from queries._x_common import TWEET_HEADERS, base_tweet_params, tweet_row, x_flag_controls, x_settings, x_token_field


HEADERS = TWEET_HEADERS

META = {
    "key": "x_conversation_replies_scan",
    "name": "X - Conversation Replies Scan",
    "description": "Collect replies around selected X conversation IDs using recent-search conversation_id operators.",
    "source_type": "official_api",
    "limitations": [
        "Replies/comments are reconstructed with search operators; X does not expose a separate comments API.",
        "Recent search normally covers only the recent window allowed by your X access tier.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="grid">
      {x_token_field()}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Conversation IDs / post IDs</label>
        <textarea name="conversation_ids" placeholder="1234567890">{h(form.get("conversation_ids", ""))}</textarea>
      </div>
      <div class="row">
        <label>Additional query terms</label>
        <input type="text" name="query_terms" value="{h(form.get("query_terms", ""))}" placeholder="Optional">
      </div>
      <div class="row">
        <label>Max results per conversation</label>
        <input type="number" name="max_results" min="10" max="5000" value="{h(form.get("max_results", "100"))}">
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
    ids = parse_lines(form.get("conversation_ids"))
    if not ids:
        raise ValueError("Enter at least one X conversation ID or post ID.")
    client = XApiClient(settings["bearer_token"])
    params = base_tweet_params(form)
    extra = str(form.get("query_terms") or "").strip()
    max_results = parse_int(form.get("max_results", 100), 100, 10, 5000)
    for conversation_id in ids:
        query = f"conversation_id:{conversation_id}" + (f" {extra}" if extra else "")
        for tweet in client.iter_search(query, endpoint="tweets/search/recent", max_results=max_results, params=params):
            row = tweet_row(tweet, source_api="GET /2/tweets/search/recent", source_type=META["source_type"], target_input=conversation_id, query_text=query, settings=settings)
            if keep_row(row, settings):
                yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "X API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "X API", form, lambda: iter_row_dicts(form), HEADERS)
