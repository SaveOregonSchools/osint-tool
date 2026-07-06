from __future__ import annotations

from typing import Any, Iterator

from common import h
from osint_common import CORE_HEADERS
from queries._shared import export_core, parse_lines, run_core, status_row


HEADERS = CORE_HEADERS + ["username", "network_type"]

META = {
    "key": "tiktok_user_network_lookup",
    "name": "TikTok Research - User Network Lookup",
    "description": "Placeholder adapter for TikTok Research API account network endpoints after approval.",
    "source_type": "approved_research_api",
    "limitations": [
        "Requires approved TikTok Research API access.",
        "Network endpoint availability depends on TikTok Research Tools scopes and project approval.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    network_type = form.get("network_type", "followers")
    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;">
        <label>Username(s)</label>
        <textarea name="usernames">{h(form.get("usernames", ""))}</textarea>
      </div>
      <div class="row">
        <label>Network type</label>
        <select name="network_type">
          <option value="followers" {"selected" if network_type == "followers" else ""}>Followers</option>
          <option value="following" {"selected" if network_type == "following" else ""}>Following</option>
          <option value="liked_videos" {"selected" if network_type == "liked_videos" else ""}>Liked videos</option>
          <option value="pinned_videos" {"selected" if network_type == "pinned_videos" else ""}>Pinned videos</option>
          <option value="reposted_videos" {"selected" if network_type == "reposted_videos" else ""}>Reposted videos</option>
        </select>
      </div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    usernames = parse_lines(form.get("usernames"))
    if not usernames:
        raise ValueError("Enter at least one TikTok username.")
    for username in usernames:
        row = status_row(
            META,
            username,
            "TikTok network lookup requires project-specific endpoint confirmation before collection.",
            source_platform="TikTok",
        )
        row["username"] = username
        row["network_type"] = form.get("network_type") or ""
        yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "TikTok Research API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "TikTok Research API", form, lambda: iter_row_dicts(form), HEADERS)
