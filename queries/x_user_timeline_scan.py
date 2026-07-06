from __future__ import annotations

from typing import Any, Iterator

from common import h
from providers.x_api import XApiClient
from queries._shared import export_core, keep_row, parse_lines, run_core
from queries._x_common import TWEET_HEADERS, base_tweet_params, max_results_field, tweet_row, x_flag_controls, x_settings, x_time_fields, x_token_field


HEADERS = TWEET_HEADERS

META = {
    "key": "x_user_timeline_scan",
    "name": "X - User Timeline Scan",
    "description": "Fetch recent posts from selected X users by user ID or username, then apply review-term flags locally.",
    "source_type": "official_api",
    "limitations": [
        "Timeline depth and fields depend on X API access.",
        "Deleted/protected/private content cannot be retrieved.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="grid">
      {x_token_field()}
      <div class="row" style="grid-column: 1 / -1;">
        <label>User IDs or usernames</label>
        <textarea name="users" placeholder="123456789&#10;@org_handle">{h(form.get("users", ""))}</textarea>
      </div>
      {x_time_fields(form)}
      {max_results_field(form, 100)}
      {x_flag_controls(form)}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Custom flag terms</label>
        <textarea name="custom_terms">{h(form.get("custom_terms", ""))}</textarea>
      </div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = x_settings(form)
    targets = parse_lines(form.get("users"))
    if not targets:
        raise ValueError("Enter at least one X user ID or username.")
    client = XApiClient(settings["bearer_token"])
    user_ids: list[tuple[str, str]] = []
    username_targets = [target.lstrip("@") for target in targets if not target.isdigit()]
    for target in targets:
        if target.isdigit():
            user_ids.append((target, target))
    if username_targets:
        for user in client.lookup_users(username_targets):
            user_ids.append((str(user.get("id") or ""), user.get("username") or ""))
    params = base_tweet_params(form)
    for user_id, label in user_ids:
        if not user_id:
            continue
        for tweet in client.iter_user_timeline(user_id, max_results=settings["max_results"], params=params):
            row = tweet_row(tweet, source_api="GET /2/users/:id/tweets", source_type=META["source_type"], target_input=label, query_text=f"user_id:{user_id}", settings=settings)
            if keep_row(row, settings):
                yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "X API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "X API", form, lambda: iter_row_dicts(form), HEADERS)
