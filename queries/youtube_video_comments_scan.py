from __future__ import annotations

import re
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

from common import h, parse_int
from osint_common import CORE_HEADERS, core_row
from providers.youtube_data import YouTubeDataClient, youtube_comment_url
from queries._shared import env_password_field, export_core, flag_controls, include_settings, keep_row, maybe_flag, max_field, run_core


HEADERS = CORE_HEADERS + ["video_id", "comment_id", "like_count", "reply_count"]

META = {
    "key": "youtube_video_comments_scan",
    "name": "YouTube - Video Comments Scan",
    "description": "Collect top-level public comments for one or more YouTube videos through the official YouTube Data API.",
    "source_type": "official_api",
    "coverage": "Data source: YouTube Data API commentThreads.list.",
    "limitations": [
        "Only comments attached to supplied videos are searched.",
        "Unavailable, disabled, deleted, or held comments cannot be retrieved.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="grid">
      {env_password_field("YOUTUBE_API_KEY", "api_key", "YouTube Data API key")}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Video URLs or IDs</label>
        <textarea name="video_inputs" placeholder="https://www.youtube.com/watch?v=...&#10;dQw4w9WgXcQ">{h(form.get("video_inputs", ""))}</textarea>
      </div>
      {max_field(form, "max_comments_per_video", "Max comments per video", 100, 1000)}
      {flag_controls(form)}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Custom terms</label>
        <textarea name="custom_terms">{h(form.get("custom_terms", ""))}</textarea>
      </div>
    </div>
    """


def _video_ids(raw: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in str(raw or "").replace(",", "\n").replace(";", "\n").splitlines():
        item = line.strip()
        if not item:
            continue
        video_id = item
        if item.startswith("http://") or item.startswith("https://"):
            parsed = urlparse(item)
            query = parse_qs(parsed.query)
            video_id = (query.get("v") or [""])[0]
            if not video_id and parsed.netloc.endswith("youtu.be"):
                video_id = parsed.path.strip("/")
            if not video_id:
                parts = [part for part in parsed.path.split("/") if part]
                video_id = parts[-1] if parts else ""
        match = re.search(r"[A-Za-z0-9_-]{8,}", video_id)
        if match:
            video_id = match.group(0)
        if video_id and video_id not in seen:
            out.append(video_id)
            seen.add(video_id)
    return out


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = include_settings(form)
    settings["max_comments_per_video"] = parse_int(form.get("max_comments_per_video", 100), 100, 1, 1000)
    ids = _video_ids(form.get("video_inputs", ""))
    if not ids:
        raise ValueError("Enter at least one YouTube video URL or ID.")
    client = YouTubeDataClient(str(form.get("api_key") or "").strip() or None)
    for video_id in ids:
        for item in client.iter_video_comments(video_id, max_comments=settings["max_comments_per_video"]):
            top = ((item.get("snippet") or {}).get("topLevelComment") or {})
            snippet = top.get("snippet") or {}
            comment_id = top.get("id") or item.get("id") or ""
            text = str(snippet.get("textDisplay") or snippet.get("textOriginal") or "")
            categories, matched = maybe_flag(text, settings)
            author_channel = snippet.get("authorChannelId") or {}
            row = core_row(
                source_platform="YouTube",
                source_api="youtube.data.v3.commentThreads",
                source_type=META["source_type"],
                target_input=video_id,
                query_text="video comments",
                flag_categories=categories,
                matched_terms=matched,
                created_at=snippet.get("publishedAt") or "",
                author_handle=snippet.get("authorDisplayName") or "",
                author_id=(author_channel.get("value") if isinstance(author_channel, dict) else "") or "",
                author_display_name=snippet.get("authorDisplayName") or "",
                canonical_url=youtube_comment_url(video_id, comment_id),
                text=text,
                media_summary="comment",
                metrics_json={"like_count": snippet.get("likeCount"), "total_reply_count": (item.get("snippet") or {}).get("totalReplyCount")},
                raw_json=item,
                platform_item_id=comment_id,
                video_id=video_id,
                comment_id=comment_id,
                like_count=snippet.get("likeCount", ""),
                reply_count=(item.get("snippet") or {}).get("totalReplyCount", ""),
            )
            if keep_row(row, settings):
                yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "YouTube Data API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "YouTube Data API", form, lambda: iter_row_dicts(form), HEADERS)
