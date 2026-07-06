from __future__ import annotations

from typing import Any, Iterator

from common import get_form_bool, h, parse_int
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.tiktok_research import TikTokResearchClient
from queries._shared import date_range_fields, export_core, flag_controls, include_settings, keep_row, maybe_flag, parse_lines, run_core


HEADERS = CORE_HEADERS + ["video_id", "username", "region_code", "hashtags", "view_count", "like_count", "comment_count", "share_count"]

VIDEO_FIELDS = [
    "id",
    "video_description",
    "create_time",
    "region_code",
    "share_count",
    "view_count",
    "like_count",
    "comment_count",
    "music_id",
    "hashtag_names",
    "username",
    "voice_to_text",
    "video_duration",
]

META = {
    "key": "tiktok_research_video_search",
    "name": "TikTok Research - Video Search",
    "description": "Query TikTok Research API public video data after project approval.",
    "source_type": "approved_research_api",
    "limitations": [
        "Requires approved TikTok Research API access for a specific public-interest research project.",
        "Research Tools cover public videos by public creators aged 18+ in supported regions and exclude Canada.",
        "Results may be incomplete due to deletions, privacy changes, and API scope limits.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    include_comments = "checked" if get_form_bool(form, "include_comments", False) else ""
    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;">
        <label>Username(s)</label>
        <textarea name="usernames" placeholder="creator1&#10;creator2">{h(form.get("usernames", ""))}</textarea>
      </div>
      <div class="row" style="grid-column: 1 / -1;">
        <label>Keyword terms</label>
        <textarea name="keyword_terms">{h(form.get("keyword_terms", ""))}</textarea>
      </div>
      <div class="row" style="grid-column: 1 / -1;">
        <label>Hashtags</label>
        <textarea name="hashtags">{h(form.get("hashtags", ""))}</textarea>
      </div>
      <div class="row"><label>Region code</label><input type="text" name="region_code" value="{h(form.get("region_code", ""))}" placeholder="US"></div>
      {date_range_fields(form)}
      <div class="row"><label>Max videos</label><input type="number" name="max_videos" min="1" max="1000" value="{h(form.get("max_videos", "100"))}"></div>
      <div class="row"><label><input type="checkbox" name="include_comments" {include_comments}> Include comments after video search</label></div>
      <div class="row"><label>Max comments per video</label><input type="number" name="max_comments_per_video" min="0" max="500" value="{h(form.get("max_comments_per_video", "25"))}"></div>
      {flag_controls(form)}
      <div class="row" style="grid-column: 1 / -1;"><label>Custom flag terms</label><textarea name="custom_terms">{h(form.get("custom_terms", ""))}</textarea></div>
    </div>
    """


def _query(form: dict[str, Any]) -> dict[str, Any]:
    conditions = []
    usernames = parse_lines(form.get("usernames"))
    keywords = parse_lines(form.get("keyword_terms"))
    hashtags = [tag.lstrip("#") for tag in parse_lines(form.get("hashtags"))]
    if usernames:
        conditions.append({"operation": "IN", "field_name": "username", "field_values": usernames})
    if keywords:
        conditions.append({"operation": "IN", "field_name": "keyword", "field_values": keywords})
    if hashtags:
        conditions.append({"operation": "IN", "field_name": "hashtag_name", "field_values": hashtags})
    if form.get("region_code"):
        conditions.append({"operation": "EQ", "field_name": "region_code", "field_values": [str(form.get("region_code")).upper()]})
    if form.get("date_min"):
        conditions.append({"operation": "GTE", "field_name": "create_date", "field_values": [str(form.get("date_min"))]})
    if form.get("date_max"):
        conditions.append({"operation": "LTE", "field_name": "create_date", "field_values": [str(form.get("date_max"))]})
    return {"and": conditions} if conditions else {}


def _video_rows(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = include_settings(form)
    client = TikTokResearchClient()
    max_videos = parse_int(form.get("max_videos", 100), 100, 1, 1000)
    data = client.query_videos(_query(form), VIDEO_FIELDS, max_videos)
    videos = (data.get("data") or {}).get("videos") or data.get("videos") or []
    for video in videos[:max_videos]:
        if not isinstance(video, dict):
            continue
        text = "\n".join(str(video.get(key) or "") for key in ("video_description", "voice_to_text"))
        categories, matched = maybe_flag(text, settings)
        video_id = str(video.get("id") or "")
        row = core_row(
            source_platform="TikTok",
            source_api="POST /v2/research/video/query/",
            source_type=META["source_type"],
            target_input=compact_json(_query(form)),
            query_text=str(form.get("keyword_terms") or form.get("usernames") or form.get("hashtags") or ""),
            flag_categories=categories,
            matched_terms=matched,
            created_at=str(video.get("create_time") or ""),
            author_handle=video.get("username") or "",
            author_display_name=video.get("username") or "",
            canonical_url=f"https://www.tiktok.com/@{video.get('username')}/video/{video_id}" if video.get("username") and video_id else "",
            text=text,
            media_summary="video",
            metrics_json={key: video.get(key) for key in ("view_count", "like_count", "comment_count", "share_count")},
            raw_json=video,
            platform_item_id=video_id,
            video_id=video_id,
            username=video.get("username") or "",
            region_code=video.get("region_code") or "",
            hashtags=compact_json(video.get("hashtag_names")),
            view_count=video.get("view_count", ""),
            like_count=video.get("like_count", ""),
            comment_count=video.get("comment_count", ""),
            share_count=video.get("share_count", ""),
        )
        if keep_row(row, settings):
            yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "TikTok Research API", form, lambda: _video_rows(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "TikTok Research API", form, lambda: _video_rows(form), HEADERS)
