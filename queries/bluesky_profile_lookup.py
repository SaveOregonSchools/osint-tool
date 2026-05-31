from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterator

from common import DEFAULT_DELAY_SECONDS, OsintApiError, get_form_bool, h, make_session, parse_actor_inputs, xrpc_get


HEADERS = [
    "platform",
    "actor_input",
    "handle",
    "did",
    "display_name",
    "description",
    "followers_count",
    "follows_count",
    "posts_count",
    "indexed_at",
    "created_at",
    "labels",
    "avatar_url",
    "banner_url",
    "profile_url",
    "captured_at_utc",
    "source_api",
    "notes",
]

META = {
    "key": "bluesky_profile_lookup",
    "name": "Bluesky — profile lookup / account validation",
    "description": "Validate Bluesky handles/DIDs before scanning feeds. Returns public profile metadata and a bsky.app profile URL.",
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    actors = h(form.get("actors", ""))
    delay = h(form.get("delay", str(DEFAULT_DELAY_SECONDS)))
    include_status = "checked" if get_form_bool(form, "include_status_rows", True) else ""
    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;">
        <label>Bluesky handles, DIDs, or bsky.app profile URLs</label>
        <textarea name="actors" placeholder="example.bsky.social\ndid:plc:...\nhttps://bsky.app/profile/example.bsky.social">{actors}</textarea>
      </div>
      <div class="row">
        <label>Delay between API calls, seconds</label>
        <input type="number" step="0.05" min="0" max="10" name="delay" value="{delay}">
      </div>
      <div class="row">
        <label><input type="checkbox" name="include_status_rows" {include_status}> Include error/status rows</label>
      </div>
    </div>
    """


def _delay(form: dict[str, Any]) -> float:
    try:
        return max(0.0, min(float(form.get("delay") or DEFAULT_DELAY_SECONDS), 10.0))
    except Exception:
        return DEFAULT_DELAY_SECONDS


def _status_row(actor: str, note: str) -> list[Any]:
    row = [""] * len(HEADERS)
    row[0] = "Bluesky"
    row[1] = actor
    row[-1] = note
    return row


def iter_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    actors = parse_actor_inputs(form.get("actors", ""))
    if not actors:
        raise ValueError("Enter at least one Bluesky handle, DID, or profile URL.")
    delay = _delay(form)
    include_status = get_form_bool(form, "include_status_rows", True)
    session = make_session()

    for actor in actors:
        try:
            profile = xrpc_get("app.bsky.actor.getProfile", params={"actor": actor}, session=session)
            labels = profile.get("labels") or []
            label_values = []
            if isinstance(labels, list):
                for item in labels:
                    if isinstance(item, dict) and item.get("val"):
                        label_values.append(str(item.get("val")))
            handle = profile.get("handle") or ""
            yield [
                "Bluesky",
                actor,
                handle,
                profile.get("did") or "",
                profile.get("displayName") or "",
                profile.get("description") or "",
                profile.get("followersCount", ""),
                profile.get("followsCount", ""),
                profile.get("postsCount", ""),
                profile.get("indexedAt") or "",
                profile.get("createdAt") or "",
                "; ".join(label_values),
                profile.get("avatar") or "",
                profile.get("banner") or "",
                f"https://bsky.app/profile/{handle or profile.get('did', actor)}",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "app.bsky.actor.getProfile",
                "",
            ]
        except OsintApiError as exc:
            if include_status:
                yield _status_row(actor, f"API error: {exc}; status={exc.status_code}; body={exc.body or ''}")
            else:
                raise
        if delay:
            time.sleep(delay)


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return HEADERS, list(iter_rows(form))


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from iter_rows(form)
