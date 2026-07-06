from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Iterator

from common import (
    DEFAULT_DELAY_SECONDS,
    HTTP_TIMEOUT,
    OsintApiError,
    classify_text,
    get_form_bool,
    h,
    make_session,
    parse_date_start,
    parse_date_until_exclusive,
    parse_int,
    parse_terms,
    within_date_range,
)

GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v25.0").strip() or "v25.0"
GRAPH_BASE = os.getenv("META_GRAPH_BASE", "https://graph.facebook.com").rstrip("/")
ENV_TOKEN = "META_GRAPH_ACCESS_TOKEN"
FALLBACK_ENV_TOKEN = "META_AD_LIBRARY_ACCESS_TOKEN"

HEADERS = [
    "platform",
    "page_input",
    "content_type",
    "flag_categories",
    "matched_terms",
    "page_id",
    "page_name",
    "post_id",
    "comment_id",
    "parent_comment_id",
    "created_at",
    "updated_at",
    "permalink_url",
    "author_id",
    "author_name",
    "message",
    "story",
    "like_count",
    "comment_count",
    "share_count",
    "captured_at_utc",
    "source_api",
    "notes",
]

META = {
    "key": "meta_facebook_page_content_search",
    "name": "Meta - Facebook Page posts/comments",
    "description": (
        "Collect visible Facebook Page posts and optional top-level comments through the official Meta Graph API. "
        "Requires an access token with the permissions Meta requires for the target Page/content."
    ),
    "source_type": "official_api",
    "limitations": [
        "Graph API Page endpoints are not a general-purpose public Facebook/Instagram scraping API.",
        "Access depends on Meta app review, token type, permissions, and target Page/content visibility.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    token_present = bool(os.getenv(ENV_TOKEN) or os.getenv(FALLBACK_ENV_TOKEN))
    token_note = (
        f"{ENV_TOKEN} or {FALLBACK_ENV_TOKEN} is set; leave token blank here."
        if token_present
        else f"Set {ENV_TOKEN} in your .env/shell or paste a token for this one request."
    )
    pages = h(form.get("pages", ""))
    terms = h(form.get("terms", ""))
    since = h(form.get("since", ""))
    until = h(form.get("until", ""))
    max_posts = h(form.get("max_posts_per_page", "100"))
    max_comments = h(form.get("max_comments_per_post", "25"))
    delay = h(form.get("delay", str(DEFAULT_DELAY_SECONDS)))
    include_comments = "checked" if get_form_bool(form, "include_comments", True) else ""
    include_candidate = "checked" if get_form_bool(form, "include_candidate", True) else ""
    include_lobbying = "checked" if get_form_bool(form, "include_lobbying", True) else ""
    only_flagged = "checked" if get_form_bool(form, "only_flagged", False) else ""
    include_status = "checked" if get_form_bool(form, "include_status_rows", True) else ""

    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;">
        <label>Meta Graph API access token</label>
        <input type="password" name="access_token" value="" autocomplete="off" placeholder="Optional if {ENV_TOKEN} is set">
        <div class="subtle">{h(token_note)} This module uses Meta Graph API endpoints and does not scrape or bypass login/access controls.</div>
      </div>

      <div class="row" style="grid-column: 1 / -1;">
        <label>Facebook Page IDs or handles</label>
        <textarea name="pages" placeholder="123456789\nsaveoregonschools">{pages}</textarea>
        <div class="subtle">One Page ID or resolvable Page handle per line. API access depends on Meta permissions and the target Page/content.</div>
      </div>

      <div class="row" style="grid-column: 1 / -1;">
        <label>Filter terms, optional</label>
        <textarea name="terms" placeholder="Candidate Name\nHB 1234\nschool board">{terms}</textarea>
        <div class="subtle">Terms are applied locally after collection. Leave blank to collect all returned posts/comments.</div>
      </div>

      <div class="row">
        <label>Since date</label>
        <input type="date" name="since" value="{since}">
      </div>

      <div class="row">
        <label>Until date</label>
        <input type="date" name="until" value="{until}">
      </div>

      <div class="row">
        <label>Max posts per Page</label>
        <input type="number" name="max_posts_per_page" min="1" max="1000" value="{max_posts}">
      </div>

      <div class="row">
        <label>Max comments per post</label>
        <input type="number" name="max_comments_per_post" min="0" max="500" value="{max_comments}">
      </div>

      <div class="row">
        <label>Delay between API calls, seconds</label>
        <input type="number" step="0.05" min="0" max="10" name="delay" value="{delay}">
      </div>

      <div class="row">
        <label><input type="checkbox" name="include_comments" {include_comments}> Include top-level comments</label>
        <label><input type="checkbox" name="include_candidate" {include_candidate}> Candidate-intervention review patterns</label>
        <label><input type="checkbox" name="include_lobbying" {include_lobbying}> Lobbying review patterns</label>
      </div>

      <div class="row">
        <label><input type="checkbox" name="only_flagged" {only_flagged}> Only return flagged or term-matched rows</label>
        <label><input type="checkbox" name="include_status_rows" {include_status}> Include status/error rows</label>
      </div>
    </div>
    """


def _access_token(form: dict[str, Any]) -> str:
    return str(form.get("access_token") or os.getenv(ENV_TOKEN) or os.getenv(FALLBACK_ENV_TOKEN) or "").strip()


def _parse_pages(raw: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in str(raw or "").replace(",", "\n").replace(";", "\n").splitlines():
        page = line.strip().strip("/")
        if not page:
            continue
        if page.startswith("http://") or page.startswith("https://"):
            parts = [part for part in page.split("/") if part]
            page = parts[-1] if parts else page
        if page and page not in seen:
            out.append(page)
            seen.add(page)
    return out


def _settings(form: dict[str, Any]) -> dict[str, Any]:
    pages = _parse_pages(form.get("pages", ""))
    if not pages:
        raise ValueError("Enter at least one Facebook Page ID or handle.")
    try:
        delay = max(0.0, min(float(form.get("delay") or DEFAULT_DELAY_SECONDS), 10.0))
    except Exception:
        delay = DEFAULT_DELAY_SECONDS
    return {
        "access_token": _access_token(form),
        "pages": pages,
        "terms": parse_terms(form.get("terms", "")),
        "since_dt": parse_date_start(form.get("since", "")),
        "until_dt": parse_date_until_exclusive(form.get("until", "")),
        "max_posts_per_page": parse_int(form.get("max_posts_per_page", 100), 100, 1, 1000),
        "max_comments_per_post": parse_int(form.get("max_comments_per_post", 25), 25, 0, 500),
        "include_comments": get_form_bool(form, "include_comments", True),
        "include_candidate": get_form_bool(form, "include_candidate", True),
        "include_lobbying": get_form_bool(form, "include_lobbying", True),
        "only_flagged": get_form_bool(form, "only_flagged", False),
        "include_status_rows": get_form_bool(form, "include_status_rows", True),
        "delay": delay,
    }


def _api_url(path: str) -> str:
    return f"{GRAPH_BASE}/{GRAPH_API_VERSION}/{path.lstrip('/')}"


def _graph_get(session: Any, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = path_or_url if path_or_url.startswith("http") else _api_url(path_or_url)
    response = session.get(url, params=params or {}, timeout=HTTP_TIMEOUT)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            wait = min(float(retry_after), 30.0) if retry_after else 3.0
        except ValueError:
            wait = 3.0
        time.sleep(wait)
        response = session.get(url, params=params or {}, timeout=HTTP_TIMEOUT)
    if not response.ok:
        body = response.text[:1000] if response.text else ""
        raise OsintApiError(
            f"Meta Graph API request failed: HTTP {response.status_code}",
            status_code=response.status_code,
            url=response.url,
            body=body,
        )
    try:
        return response.json()
    except Exception as exc:
        raise OsintApiError(
            f"Meta Graph API response was not valid JSON: {exc}",
            status_code=response.status_code,
            url=response.url,
            body=response.text[:1000],
        ) from exc


def _status_row(page: str, note: str) -> list[Any]:
    row = [""] * len(HEADERS)
    row[0] = "Facebook"
    row[1] = page
    row[2] = "status"
    row[-1] = note
    return row


def _page_info(session: Any, page: str, token: str) -> dict[str, Any]:
    return _graph_get(session, page, {"access_token": token, "fields": "id,name,link"})


def _terms_match(text: str, terms: list[str]) -> bool:
    if not terms:
        return True
    lowered = text.casefold()
    return any(term.casefold() in lowered for term in terms if term)


def _passes_filters(row: list[Any], settings: dict[str, Any]) -> bool:
    is_flagged = bool(row[3] or row[4])
    if settings["only_flagged"] and not is_flagged:
        return False
    if settings["terms"] and not row[4]:
        return False
    return True


def _post_row(post: dict[str, Any], page_input: str, page: dict[str, Any], settings: dict[str, Any]) -> list[Any]:
    message = str(post.get("message") or "")
    story = str(post.get("story") or "")
    text = "\n".join(piece for piece in [message, story] if piece)
    categories, matched = classify_text(
        text,
        custom_terms=settings["terms"],
        include_candidate=settings["include_candidate"],
        include_lobbying=settings["include_lobbying"],
    )
    comments = post.get("comments") if isinstance(post.get("comments"), dict) else {}
    comments_summary = comments.get("summary") if isinstance(comments, dict) else {}
    shares = post.get("shares") if isinstance(post.get("shares"), dict) else {}
    return [
        "Facebook",
        page_input,
        "post",
        categories,
        matched,
        page.get("id") or "",
        page.get("name") or "",
        post.get("id") or "",
        "",
        "",
        post.get("created_time") or "",
        post.get("updated_time") or "",
        post.get("permalink_url") or "",
        page.get("id") or "",
        page.get("name") or "",
        message,
        story,
        "",
        comments_summary.get("total_count", ""),
        shares.get("count", ""),
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        f"{GRAPH_API_VERSION}/{{page-id}}/posts",
        "",
    ]


def _comment_row(comment: dict[str, Any], page_input: str, page: dict[str, Any], post_id: str, settings: dict[str, Any]) -> list[Any]:
    message = str(comment.get("message") or "")
    categories, matched = classify_text(
        message,
        custom_terms=settings["terms"],
        include_candidate=settings["include_candidate"],
        include_lobbying=settings["include_lobbying"],
    )
    author = comment.get("from") if isinstance(comment.get("from"), dict) else {}
    parent = comment.get("parent") if isinstance(comment.get("parent"), dict) else {}
    return [
        "Facebook",
        page_input,
        "comment",
        categories,
        matched,
        page.get("id") or "",
        page.get("name") or "",
        post_id,
        comment.get("id") or "",
        parent.get("id") or "",
        comment.get("created_time") or "",
        "",
        comment.get("permalink_url") or "",
        author.get("id") or "",
        author.get("name") or "",
        message,
        "",
        comment.get("like_count", ""),
        comment.get("comment_count", ""),
        "",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        f"{GRAPH_API_VERSION}/{{post-id}}/comments",
        "top-level comment",
    ]


def _iter_posts(session: Any, page: dict[str, Any], token: str, settings: dict[str, Any]) -> Iterator[dict[str, Any]]:
    returned = 0
    next_url = ""
    params: dict[str, Any] = {
        "access_token": token,
        "limit": min(100, settings["max_posts_per_page"]),
        "fields": "id,message,story,created_time,updated_time,permalink_url,shares,comments.limit(0).summary(true)",
    }
    while returned < settings["max_posts_per_page"]:
        data = _graph_get(session, next_url or f"{page['id']}/posts", params if not next_url else None)
        posts = data.get("data") or []
        if not posts:
            break
        for post in posts:
            if not isinstance(post, dict):
                continue
            returned += 1
            if within_date_range(post.get("created_time"), settings["since_dt"], settings["until_dt"]):
                yield post
            if returned >= settings["max_posts_per_page"]:
                break
        paging = data.get("paging") if isinstance(data.get("paging"), dict) else {}
        next_url = paging.get("next") or ""
        if not next_url:
            break
        params = {}
        if settings["delay"]:
            time.sleep(settings["delay"])


def _iter_comments(session: Any, post_id: str, token: str, settings: dict[str, Any]) -> Iterator[dict[str, Any]]:
    returned = 0
    next_url = ""
    params: dict[str, Any] = {
        "access_token": token,
        "limit": min(100, max(1, settings["max_comments_per_post"])),
        "filter": "stream",
        "fields": "id,message,created_time,from,like_count,comment_count,parent,permalink_url",
    }
    while returned < settings["max_comments_per_post"]:
        data = _graph_get(session, next_url or f"{post_id}/comments", params if not next_url else None)
        comments = data.get("data") or []
        if not comments:
            break
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            returned += 1
            if within_date_range(comment.get("created_time"), settings["since_dt"], settings["until_dt"]):
                yield comment
            if returned >= settings["max_comments_per_post"]:
                break
        paging = data.get("paging") if isinstance(data.get("paging"), dict) else {}
        next_url = paging.get("next") or ""
        if not next_url:
            break
        params = {}
        if settings["delay"]:
            time.sleep(settings["delay"])


def iter_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    settings = _settings(form)
    if not settings["access_token"]:
        raise ValueError(f"Set {ENV_TOKEN} or paste a Meta Graph API access token.")

    session = make_session()
    for page_input in settings["pages"]:
        yielded = 0
        try:
            page = _page_info(session, page_input, settings["access_token"])
            for post in _iter_posts(session, page, settings["access_token"], settings):
                post_text = "\n".join(str(post.get(key) or "") for key in ("message", "story"))
                post_row = _post_row(post, page_input, page, settings)
                if _terms_match(post_text, settings["terms"]) and _passes_filters(post_row, settings):
                    yielded += 1
                    yield post_row
                if settings["include_comments"] and settings["max_comments_per_post"] > 0:
                    for comment in _iter_comments(session, str(post.get("id") or ""), settings["access_token"], settings):
                        comment_row = _comment_row(comment, page_input, page, str(post.get("id") or ""), settings)
                        if _passes_filters(comment_row, settings):
                            yielded += 1
                            yield comment_row
                        if settings["delay"]:
                            time.sleep(settings["delay"])
                if settings["delay"]:
                    time.sleep(settings["delay"])
        except OsintApiError as exc:
            if settings["include_status_rows"]:
                yield _status_row(page_input, f"API error: {exc}; status={exc.status_code}; body={exc.body or ''}")
                continue
            raise

        if settings["include_status_rows"] and yielded == 0:
            yield _status_row(page_input, "No rows yielded. Check API permissions, date/term filters, or Page visibility.")
        if settings["delay"]:
            time.sleep(settings["delay"])


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return HEADERS, list(iter_rows(form))


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from iter_rows(form)
