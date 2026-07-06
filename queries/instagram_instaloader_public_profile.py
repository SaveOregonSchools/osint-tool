from __future__ import annotations

from typing import Any, Iterator

from common import get_form_bool, h, parse_int
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.instaloader_runner import iter_instaloader_json, run_instaloader_profile
from queries._shared import date_range_fields, export_core, flag_controls, include_settings, keep_row, maybe_flag, run_core


HEADERS = CORE_HEADERS + ["profile_or_shortcode", "shortcode", "json_path", "media_path"]

META = {
    "key": "instagram_instaloader_public_profile",
    "name": "Instagram - Instaloader Public Profile",
    "description": "Optional local Instaloader runner for low-volume authorized public Instagram profile capture.",
    "source_type": "unofficial_local_tool",
    "limitations": [
        "Instaloader is unofficial and may break or trigger Instagram rate limits/account checks.",
        "Use only for public content or content the investigator is authorized to access.",
        "Do not put Instagram passwords in the web UI, automate login challenges, bypass privacy settings, or scrape private accounts without authorization.",
        "Prefer Meta Content Library API if approved access is available.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    media_checked = "checked" if get_form_bool(form, "download_media", False) else ""
    comments_checked = "checked" if get_form_bool(form, "download_comments", False) else ""
    fast_checked = "checked" if get_form_bool(form, "fast_update", True) else ""
    return f"""
    <div class="notice">Unofficial source. Review platform terms, legal risk, reliability, and account risk before use. Do not use to access private content or bypass access controls.</div>
    <div class="grid">
      <div class="row">
        <label>Profile or shortcode</label>
        <input type="text" name="profile_or_shortcode" value="{h(form.get("profile_or_shortcode", ""))}">
      </div>
      {date_range_fields(form)}
      <div class="row"><label>Max posts</label><input type="number" name="max_posts" min="1" max="500" value="{h(form.get("max_posts", "25"))}"></div>
      <div class="row"><label><input type="checkbox" name="download_media" {media_checked}> Download media</label><label><input type="checkbox" name="download_comments" {comments_checked}> Download comments</label><label><input type="checkbox" name="fast_update" {fast_checked}> Fast update</label></div>
      {flag_controls(form)}
      <div class="row" style="grid-column: 1 / -1;"><label>Custom terms</label><textarea name="custom_terms">{h(form.get("custom_terms", ""))}</textarea></div>
    </div>
    """


def _caption(data: dict[str, Any]) -> str:
    for key in ("edge_media_to_caption", "caption", "title"):
        value = data.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            edges = value.get("edges") or []
            if edges and isinstance(edges[0], dict):
                node = edges[0].get("node") or {}
                return str(node.get("text") or "")
    return ""


def _shortcode(data: dict[str, Any]) -> str:
    return str(data.get("shortcode") or data.get("code") or data.get("id") or "")


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = include_settings(form)
    target = str(form.get("profile_or_shortcode") or "").strip().lstrip("@")
    if not target:
        raise ValueError("Enter an Instagram public profile or post shortcode.")
    max_posts = parse_int(form.get("max_posts", 25), 25, 1, 500)
    output_dir = "data/instaloader"
    result = run_instaloader_profile(
        target,
        output_dir,
        comments=get_form_bool(form, "download_comments", False),
        max_posts=max_posts,
        fast_update=get_form_bool(form, "fast_update", True),
    )
    if result.returncode != 0:
        raise RuntimeError(f"Instaloader failed with exit code {result.returncode}: {result.stderr[:1000]}")
    yielded = 0
    for data in iter_instaloader_json(output_dir):
        if yielded >= max_posts:
            break
        caption = _caption(data)
        categories, matched = maybe_flag(caption, settings)
        shortcode = _shortcode(data)
        username = str(data.get("owner_username") or data.get("username") or target)
        row = core_row(
            source_platform="Instagram",
            source_api="Instaloader local runner",
            source_type=META["source_type"],
            target_input=target,
            query_text=target,
            flag_categories=categories,
            matched_terms=matched,
            created_at=str(data.get("date_utc") or data.get("taken_at_timestamp") or ""),
            author_handle=username,
            author_display_name=username,
            canonical_url=f"https://www.instagram.com/p/{shortcode}/" if shortcode else f"https://www.instagram.com/{username}/",
            text=caption,
            media_summary=str(data.get("__typename") or data.get("typename") or "post"),
            metrics_json={key: data.get(key) for key in ("likes", "comments", "video_view_count")},
            raw_json=data,
            platform_item_id=shortcode or str(data.get("id") or ""),
            profile_or_shortcode=target,
            shortcode=shortcode,
            json_path=data.get("_json_path") or "",
            media_path="",
        )
        if keep_row(row, settings):
            yielded += 1
            yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "Instaloader", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "Instaloader", form, lambda: iter_row_dicts(form), HEADERS)
