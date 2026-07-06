from __future__ import annotations

from typing import Any, Iterator

from common import h
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.tiktok_research import TikTokResearchClient
from queries._shared import export_core, parse_lines, run_core


HEADERS = CORE_HEADERS + ["username", "display_name", "bio_description", "is_verified", "follower_count", "following_count", "likes_count", "video_count"]

META = {
    "key": "tiktok_user_info_lookup",
    "name": "TikTok Research - User Info Lookup",
    "description": "Look up TikTok public account fields through the approved TikTok Research API.",
    "source_type": "approved_research_api",
    "limitations": ["Requires approved TikTok Research API access.", "Account fields depend on Research API scopes and data availability."],
    "headers": HEADERS,
}


FIELDS = ["username", "display_name", "bio_description", "is_verified", "follower_count", "following_count", "likes_count", "video_count"]


def render_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;">
        <label>Username(s)</label>
        <textarea name="usernames">{h(form.get("usernames", ""))}</textarea>
      </div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    usernames = parse_lines(form.get("usernames"))
    if not usernames:
        raise ValueError("Enter at least one TikTok username.")
    client = TikTokResearchClient()
    for username in usernames:
        data = client.query_user_info(username, FIELDS)
        user = (data.get("data") or {}).get("user") or data.get("user") or data
        yield core_row(
            source_platform="TikTok",
            source_api="POST /v2/research/user/info/",
            source_type=META["source_type"],
            target_input=username,
            query_text=username,
            author_handle=user.get("username") or username,
            author_display_name=user.get("display_name") or "",
            canonical_url=f"https://www.tiktok.com/@{user.get('username') or username}",
            text=user.get("bio_description") or "",
            metrics_json={key: user.get(key) for key in ("follower_count", "following_count", "likes_count", "video_count")},
            raw_json=user,
            platform_item_id=user.get("username") or username,
            username=user.get("username") or username,
            display_name=user.get("display_name") or "",
            bio_description=user.get("bio_description") or "",
            is_verified=user.get("is_verified", ""),
            follower_count=user.get("follower_count", ""),
            following_count=user.get("following_count", ""),
            likes_count=user.get("likes_count", ""),
            video_count=user.get("video_count", ""),
        )


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "TikTok Research API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "TikTok Research API", form, lambda: iter_row_dicts(form), HEADERS)
