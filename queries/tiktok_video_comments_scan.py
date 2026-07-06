from __future__ import annotations

from typing import Any, Iterator

from common import h, parse_int
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.tiktok_research import TikTokResearchClient
from queries._shared import export_core, flag_controls, include_settings, keep_row, maybe_flag, parse_lines, run_core


HEADERS = CORE_HEADERS + ["video_id", "comment_id", "like_count", "reply_count"]

META = {
    "key": "tiktok_video_comments_scan",
    "name": "TikTok Research - Video Comments Scan",
    "description": "Collect TikTok video comments through the approved TikTok Research API comments endpoint.",
    "source_type": "approved_research_api",
    "limitations": [
        "Requires approved TikTok Research API access.",
        "Comment availability is subject to TikTok Research Tools scope, deletion, privacy, and endpoint limits.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;">
        <label>Video IDs</label>
        <textarea name="video_ids">{h(form.get("video_ids", ""))}</textarea>
      </div>
      <div class="row"><label>Max comments per video</label><input type="number" name="max_comments_per_video" min="1" max="1000" value="{h(form.get("max_comments_per_video", "100"))}"></div>
      {flag_controls(form)}
      <div class="row" style="grid-column: 1 / -1;"><label>Custom flag terms</label><textarea name="custom_terms">{h(form.get("custom_terms", ""))}</textarea></div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = include_settings(form)
    ids = parse_lines(form.get("video_ids"))
    if not ids:
        raise ValueError("Enter at least one TikTok video ID.")
    client = TikTokResearchClient()
    max_comments = parse_int(form.get("max_comments_per_video", 100), 100, 1, 1000)
    for video_id in ids:
        data = client.query_video_comments(video_id, max_count=max_comments)
        comments = (data.get("data") or {}).get("comments") or data.get("comments") or []
        for comment in comments[:max_comments]:
            if not isinstance(comment, dict):
                continue
            text = str(comment.get("text") or comment.get("comment_text") or "")
            categories, matched = maybe_flag(text, settings)
            comment_id = str(comment.get("id") or comment.get("comment_id") or "")
            row = core_row(
                source_platform="TikTok",
                source_api="POST /v2/research/video/comment/list/",
                source_type=META["source_type"],
                target_input=video_id,
                query_text="video comments",
                flag_categories=categories,
                matched_terms=matched,
                created_at=str(comment.get("create_time") or ""),
                author_handle=comment.get("username") or "",
                author_display_name=comment.get("username") or "",
                canonical_url=f"https://www.tiktok.com/@/video/{video_id}?comment={comment_id}",
                text=text,
                media_summary="comment",
                metrics_json={"like_count": comment.get("like_count"), "reply_count": comment.get("reply_count")},
                raw_json=comment,
                platform_item_id=comment_id,
                video_id=video_id,
                comment_id=comment_id,
                like_count=comment.get("like_count", ""),
                reply_count=comment.get("reply_count", ""),
            )
            if keep_row(row, settings):
                yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "TikTok Research API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "TikTok Research API", form, lambda: iter_row_dicts(form), HEADERS)
