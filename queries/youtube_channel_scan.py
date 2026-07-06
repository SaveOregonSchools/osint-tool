from __future__ import annotations

from typing import Any, Iterator

from common import get_form_bool, h, parse_int
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.youtube_data import YouTubeDataClient, within_date_window, youtube_comment_url, youtube_video_url
from queries._shared import date_range_fields, delay_field, env_password_field, export_core, flag_controls, include_settings, keep_row, maybe_flag, max_field, run_core


HEADERS = CORE_HEADERS + ["item_type", "video_id", "comment_id", "channel_id", "channel_title", "like_count", "reply_count"]

META = {
    "key": "youtube_channel_scan",
    "name": "YouTube - Channel Video/Comment Scan",
    "description": "Scan public YouTube channel videos and optionally video comments for lobbying/candidate-review terms.",
    "source_type": "official_api",
    "coverage": "Data source: YouTube Data API.",
    "limitations": [
        "YouTube Data API can retrieve comments attached to videos, but it is not a general find-all-comments-by-user API.",
        "Comments can be disabled, deleted, held for review, or unavailable.",
        "API quota and search limits apply.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    scan_comments = "checked" if get_form_bool(form, "scan_comments", False) else ""
    return f"""
    <div class="grid">
      {env_password_field("YOUTUBE_API_KEY", "api_key", "YouTube Data API key", "Uses official YouTube Data API endpoints only.")}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Channel URL / @handle / channel ID</label>
        <textarea name="channel_input" placeholder="https://www.youtube.com/@example&#10;UC...">{h(form.get("channel_input", ""))}</textarea>
      </div>
      {date_range_fields(form)}
      {max_field(form, "max_videos", "Max videos", 50, 5000)}
      <div class="row">
        <label><input type="checkbox" name="scan_comments" {scan_comments}> Scan top-level comments</label>
      </div>
      {max_field(form, "max_comments_per_video", "Max comments per video", 25, 500)}
      {delay_field(form)}
      {flag_controls(form)}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Custom terms</label>
        <textarea name="custom_terms" placeholder="Candidate Name&#10;HB 1234">{h(form.get("custom_terms", ""))}</textarea>
      </div>
    </div>
    """


def _settings(form: dict[str, Any]) -> dict[str, Any]:
    settings = include_settings(form)
    settings.update(
        {
            "api_key": str(form.get("api_key") or "").strip() or None,
            "channel_input": str(form.get("channel_input") or "").strip(),
            "date_min": str(form.get("date_min") or "").strip(),
            "date_max": str(form.get("date_max") or "").strip(),
            "max_videos": parse_int(form.get("max_videos", 50), 50, 1, 5000),
            "scan_comments": get_form_bool(form, "scan_comments", False),
            "max_comments_per_video": parse_int(form.get("max_comments_per_video", 25), 25, 0, 500),
        }
    )
    return settings


def _video_row(item: dict[str, Any], channel: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    snippet = item.get("snippet") or {}
    content = item.get("contentDetails") or {}
    video_id = (content.get("videoId") or (snippet.get("resourceId") or {}).get("videoId") or "")
    title = str(snippet.get("title") or "")
    description = str(snippet.get("description") or "")
    text = "\n".join(piece for piece in [title, description] if piece)
    categories, matched = maybe_flag(text, settings)
    channel_snippet = channel.get("snippet") or {}
    return core_row(
        source_platform="YouTube",
        source_api="youtube.data.v3.playlistItems",
        source_type=META["source_type"],
        target_input=settings["channel_input"],
        query_text="channel uploads",
        flag_categories=categories,
        matched_terms=matched,
        created_at=snippet.get("publishedAt") or content.get("videoPublishedAt") or "",
        author_handle=channel_snippet.get("customUrl") or "",
        author_id=channel.get("id") or "",
        author_display_name=channel_snippet.get("title") or "",
        canonical_url=youtube_video_url(video_id),
        text=text,
        media_summary="video",
        raw_json=item,
        platform_item_id=video_id,
        item_type="video",
        video_id=video_id,
        comment_id="",
        channel_id=channel.get("id") or "",
        channel_title=channel_snippet.get("title") or "",
        like_count="",
        reply_count="",
    )


def _comment_row(item: dict[str, Any], video_id: str, channel: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    top = ((item.get("snippet") or {}).get("topLevelComment") or {})
    snippet = top.get("snippet") or {}
    comment_id = top.get("id") or item.get("id") or ""
    text = str(snippet.get("textDisplay") or snippet.get("textOriginal") or "")
    categories, matched = maybe_flag(text, settings)
    author_channel = snippet.get("authorChannelId") or {}
    author_id = author_channel.get("value") if isinstance(author_channel, dict) else ""
    channel_snippet = channel.get("snippet") or {}
    return core_row(
        source_platform="YouTube",
        source_api="youtube.data.v3.commentThreads",
        source_type=META["source_type"],
        target_input=settings["channel_input"],
        query_text=f"comments for {video_id}",
        flag_categories=categories,
        matched_terms=matched,
        created_at=snippet.get("publishedAt") or "",
        author_handle=snippet.get("authorDisplayName") or "",
        author_id=author_id or "",
        author_display_name=snippet.get("authorDisplayName") or "",
        canonical_url=youtube_comment_url(video_id, comment_id),
        text=text,
        media_summary="comment",
        metrics_json={"like_count": snippet.get("likeCount"), "total_reply_count": (item.get("snippet") or {}).get("totalReplyCount")},
        raw_json=item,
        platform_item_id=comment_id,
        item_type="comment",
        video_id=video_id,
        comment_id=comment_id,
        channel_id=channel.get("id") or "",
        channel_title=channel_snippet.get("title") or "",
        like_count=snippet.get("likeCount", ""),
        reply_count=(item.get("snippet") or {}).get("totalReplyCount", ""),
    )


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = _settings(form)
    if not settings["channel_input"]:
        raise ValueError("Enter a YouTube channel URL, @handle, or channel ID.")
    client = YouTubeDataClient(settings["api_key"])
    channel = client.resolve_channel(settings["channel_input"])
    for item in client.iter_upload_videos(channel, max_videos=settings["max_videos"]):
        created = (item.get("snippet") or {}).get("publishedAt") or ""
        if not within_date_window(created, settings["date_min"], settings["date_max"]):
            continue
        video_row = _video_row(item, channel, settings)
        if keep_row(video_row, settings):
            yield video_row
        video_id = str(video_row.get("video_id") or "")
        if settings["scan_comments"] and video_id and settings["max_comments_per_video"] > 0:
            for comment in client.iter_video_comments(video_id, max_comments=settings["max_comments_per_video"]):
                row = _comment_row(comment, video_id, channel, settings)
                if keep_row(row, settings):
                    yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "YouTube Data API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "YouTube Data API", form, lambda: iter_row_dicts(form), HEADERS)
