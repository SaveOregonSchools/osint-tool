from __future__ import annotations

from typing import Any, Iterator

from common import h, parse_int
from osint_common import CORE_HEADERS, core_row
from providers.youtube_data import YouTubeDataClient, iso_z_from_date, youtube_video_url
from queries._shared import date_range_fields, env_password_field, export_core, flag_controls, include_settings, keep_row, maybe_flag, max_field, parse_lines, run_core


HEADERS = CORE_HEADERS + ["video_id", "channel_id", "channel_title"]

META = {
    "key": "youtube_keyword_search",
    "name": "YouTube - Keyword Video Search",
    "description": "Search public YouTube videos by keyword through YouTube Data API search.list.",
    "source_type": "official_api",
    "coverage": "Data source: YouTube Data API search.list.",
    "limitations": [
        "Search quota limits apply and results are not a complete archive of all YouTube content.",
        "This module searches videos, not arbitrary user comment history.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="grid">
      {env_password_field("YOUTUBE_API_KEY", "api_key", "YouTube Data API key")}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Keyword terms</label>
        <textarea name="keyword_terms" placeholder="Candidate Name&#10;ballot measure">{h(form.get("keyword_terms", ""))}</textarea>
      </div>
      <div class="row">
        <label>Optional channel ID</label>
        <input type="text" name="channel_id" value="{h(form.get("channel_id", ""))}">
      </div>
      {date_range_fields(form)}
      {max_field(form, "max_results_per_term", "Max results per term", 50, 500)}
      {flag_controls(form)}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Custom flag terms</label>
        <textarea name="custom_terms">{h(form.get("custom_terms", ""))}</textarea>
      </div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = include_settings(form)
    terms = parse_lines(form.get("keyword_terms", ""))
    if not terms:
        raise ValueError("Enter at least one YouTube keyword term.")
    max_results = parse_int(form.get("max_results_per_term", 50), 50, 1, 500)
    client = YouTubeDataClient(str(form.get("api_key") or "").strip() or None)
    for term in terms:
        for item in client.search_videos(
            term,
            channel_id=str(form.get("channel_id") or "").strip(),
            published_after=iso_z_from_date(str(form.get("date_min") or "")),
            published_before=iso_z_from_date(str(form.get("date_max") or ""), end_of_day=True),
            max_results=max_results,
        ):
            snippet = item.get("snippet") or {}
            id_obj = item.get("id") or {}
            video_id = id_obj.get("videoId") if isinstance(id_obj, dict) else ""
            text = "\n".join(str(snippet.get(key) or "") for key in ("title", "description"))
            categories, matched = maybe_flag(text, settings)
            row = core_row(
                source_platform="YouTube",
                source_api="youtube.data.v3.search",
                source_type=META["source_type"],
                target_input=term,
                query_text=term,
                flag_categories=categories,
                matched_terms=matched,
                created_at=snippet.get("publishedAt") or "",
                author_handle=snippet.get("channelTitle") or "",
                author_id=snippet.get("channelId") or "",
                author_display_name=snippet.get("channelTitle") or "",
                canonical_url=youtube_video_url(video_id),
                text=text,
                media_summary="video",
                raw_json=item,
                platform_item_id=video_id,
                video_id=video_id,
                channel_id=snippet.get("channelId") or "",
                channel_title=snippet.get("channelTitle") or "",
            )
            if keep_row(row, settings):
                yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "YouTube Data API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "YouTube Data API", form, lambda: iter_row_dicts(form), HEADERS)
