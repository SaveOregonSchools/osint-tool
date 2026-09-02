from __future__ import annotations

import csv
import gzip
import hashlib
import json
import mimetypes
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from urllib.parse import urlparse


MAX_MEDIA_FILE_BYTES = 25 * 1024 * 1024
MAX_MEDIA_TOTAL_BYTES = 500 * 1024 * 1024
IMAGE_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


@dataclass(frozen=True)
class XCaptureInspection:
    classification: str
    search_successes: int
    search_rate_limits: int
    search_other_statuses: int
    rate_limit_remaining: int | None
    rate_limit_reset: int | None
    error_shell: bool
    detail: str
    authentication_state: str = "unknown"
    authenticated_requests: int = 0
    guest_requests: int = 0
    logged_in_ui: bool = False
    login_ui: bool = False

    @property
    def is_valid(self) -> bool:
        return self.classification == "valid"

    @property
    def is_partial(self) -> bool:
        return self.classification in {"rate_limited_partial", "invalid_partial"}

    @property
    def is_retryable(self) -> bool:
        return self.classification in {"rate_limited_empty", "rate_limited_shell"}


def _warc_records(stream: BinaryIO) -> Iterator[tuple[dict[str, str], bytes]]:
    while True:
        line = stream.readline()
        while line in {b"\r\n", b"\n"}:
            line = stream.readline()
        if not line:
            return
        if not line.startswith(b"WARC/"):
            continue
        headers: dict[str, str] = {}
        while True:
            line = stream.readline()
            if not line:
                return
            if line in {b"\r\n", b"\n"}:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if ":" in text:
                name, value = text.split(":", 1)
                headers[name.casefold().strip()] = value.strip()
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            return
        block = stream.read(max(0, length))
        yield headers, block


def _http_payload(block: bytes) -> tuple[dict[str, str], bytes]:
    separator = b"\r\n\r\n"
    index = block.find(separator)
    separator_length = len(separator)
    if index < 0:
        separator = b"\n\n"
        index = block.find(separator)
        separator_length = len(separator)
    if index < 0:
        return {}, b""
    header_lines = block[:index].decode("iso-8859-1", errors="replace").splitlines()[1:]
    headers: dict[str, str] = {}
    for line in header_lines:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.casefold().strip()] = value.strip()
    return headers, block[index + separator_length :]


def _decode_text(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()


def _text_target_url(target: str) -> str:
    for prefix in ("urn:textFinal:", "urn:text:"):
        if target.startswith(prefix):
            return target[len(prefix) :]
    return ""


def _extension(content_type: str, source_url: str) -> str:
    clean_type = content_type.split(";", 1)[0].casefold().strip()
    if clean_type in IMAGE_EXTENSIONS:
        return IMAGE_EXTENSIONS[clean_type]
    suffix = Path(urlparse(source_url).path).suffix.casefold()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        return suffix
    return mimetypes.guess_extension(clean_type) or ".img"


def _page_entries(archive: zipfile.ZipFile) -> dict[str, dict[str, Any]]:
    pages: dict[str, dict[str, Any]] = {}
    for name in archive.namelist():
        if not name.startswith("pages/") or not name.endswith(".jsonl"):
            continue
        with archive.open(name) as handle:
            for raw_line in handle:
                try:
                    item = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                url = str(item.get("url") or "").strip()
                if url:
                    pages[url] = item
    return pages


def _http_status(block: bytes) -> int | None:
    first_line = block.splitlines()[0] if block else b""
    match = re.match(rb"HTTP/\S+\s+(\d{3})(?:\s|$)", first_line)
    return int(match.group(1)) if match else None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _retry_after_epoch(value: str, now_epoch: int) -> int | None:
    text = str(value or "").strip()
    seconds = _positive_int(text)
    if seconds is not None:
        return now_epoch + seconds
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int(parsed.timestamp()))


def _cookie_names(value: str) -> set[str]:
    names: set[str] = set()
    for part in str(value or "").split(";"):
        name, separator, _cookie_value = part.strip().partition("=")
        if separator and name:
            names.add(name.casefold())
    return names


def _is_x_request(target: str) -> bool:
    host = (urlparse(target).hostname or "").casefold()
    return host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "api.x.com"} or host.endswith(
        (".x.com", ".twitter.com")
    )


def inspect_x_wacz(wacz_path: Path, *, now_epoch: int | None = None) -> XCaptureInspection:
    """Classify an X search capture using its archived SearchTimeline traffic."""
    current_epoch = int(datetime.now(timezone.utc).timestamp()) if now_epoch is None else int(now_epoch)
    successes = 0
    rate_limits = 0
    other_statuses = 0
    reset_values: list[int] = []
    retry_after_values: list[int] = []
    remaining_by_reset: dict[int, list[int]] = {}
    unpaired_remaining: list[int] = []
    error_shell = False
    authenticated_requests = 0
    guest_requests = 0
    logged_in_ui = False
    login_ui = False

    with zipfile.ZipFile(wacz_path) as archive:
        for page in _page_entries(archive).values():
            page_url = str(page.get("url") or "").casefold()
            page_text = " ".join(str(page.get("text") or "").casefold().split())
            if "x.com/" in page_url or "twitter.com/" in page_url:
                nav_indicators = sum(
                    indicator in page_text
                    for indicator in ("notifications", "messages", "chat", "bookmarks", "communities", "profile")
                )
                if nav_indicators >= 3:
                    logged_in_ui = True
                if "/i/flow/login" in page_url or (
                    nav_indicators < 3
                    and re.search(r"(?:^|\s)(?:log in|sign in)(?:\s|$)", page_text)
                    and re.search(r"(?:^|\s)(?:sign up|create account|join today)(?:\s|$)", page_text)
                ):
                    login_ui = True
            if "x.com/search" in page_url and (
                "something went wrong. try reloading." in page_text or "rate limit exceeded" in page_text
            ):
                error_shell = True

        warc_names = [
            name
            for name in archive.namelist()
            if name.startswith("archive/") and (name.endswith(".warc") or name.endswith(".warc.gz"))
        ]
        for warc_name in warc_names:
            with archive.open(warc_name) as raw_warc:
                stream: BinaryIO = gzip.GzipFile(fileobj=raw_warc) if warc_name.endswith(".gz") else raw_warc
                for headers, block in _warc_records(stream):
                    record_type = headers.get("warc-type", "").casefold()
                    target = headers.get("warc-target-uri", "")
                    if record_type == "request" and _is_x_request(target):
                        request_headers, _request_body = _http_payload(block)
                        cookie_names = _cookie_names(request_headers.get("cookie", ""))
                        has_guest_token = bool(request_headers.get("x-guest-token"))
                        if has_guest_token:
                            guest_requests += 1
                        if (
                            request_headers.get("x-twitter-auth-type", "").casefold() == "oauth2session"
                            and request_headers.get("x-twitter-active-user", "").casefold() == "yes"
                            and bool(request_headers.get("x-csrf-token"))
                            and {"auth_token", "ct0"}.issubset(cookie_names)
                            and not has_guest_token
                        ):
                            authenticated_requests += 1
                        continue
                    if record_type not in {"response", "revisit"}:
                        continue
                    if not re.search(r"/SearchTimeline(?:\?|$)", target, flags=re.I):
                        continue
                    status = _http_status(block)
                    http_headers, body = _http_payload(block)
                    remaining = _positive_int(http_headers.get("x-rate-limit-remaining"))
                    reset = _positive_int(http_headers.get("x-rate-limit-reset"))
                    retry_after = _retry_after_epoch(http_headers.get("retry-after", ""), current_epoch)
                    if remaining is not None:
                        if reset is None:
                            unpaired_remaining.append(remaining)
                        else:
                            remaining_by_reset.setdefault(reset, []).append(remaining)
                    if reset is not None:
                        reset_values.append(reset)
                    if retry_after is not None:
                        retry_after_values.append(retry_after)
                    if status == 200:
                        if record_type == "revisit" and not body:
                            # A revisit only proves that some earlier payload was deduplicated.
                            # The original response, when present in the WACZ, is validated separately.
                            continue
                        body_text = body.decode("utf-8", errors="replace")
                        if "rate limit exceeded" in body_text.casefold():
                            rate_limits += 1
                            continue
                        try:
                            payload = json.loads(body_text)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            other_statuses += 1
                            continue
                        errors = payload.get("errors") if isinstance(payload, dict) else None
                        if errors:
                            error_text = json.dumps(errors, ensure_ascii=False).casefold()
                            if "rate limit" in error_text or re.search(r'"code"\s*:\s*(?:88|429)\b', error_text):
                                rate_limits += 1
                            else:
                                other_statuses += 1
                            continue
                        timeline = payload
                        for key in ("data", "search_by_raw_query", "search_timeline", "timeline"):
                            timeline = timeline.get(key) if isinstance(timeline, dict) else None
                        if isinstance(timeline, dict):
                            successes += 1
                        else:
                            other_statuses += 1
                    elif status in {403, 429, 503}:
                        body_text = body.decode("utf-8", errors="replace").casefold()
                        if status in {429, 503} or remaining == 0 or "rate limit" in body_text:
                            rate_limits += 1
                        else:
                            other_statuses += 1
                    else:
                        other_statuses += 1

    latest_header_reset = max(reset_values) if reset_values else None
    if latest_header_reset is not None and remaining_by_reset.get(latest_header_reset):
        remaining = min(remaining_by_reset[latest_header_reset])
    else:
        remaining = min(unpaired_remaining) if unpaired_remaining else None
    all_reset_values = reset_values + retry_after_values
    reset = max(all_reset_values) if all_reset_values else None
    if login_ui and not logged_in_ui:
        authentication_state = "logged_out"
    elif authenticated_requests and (logged_in_ui or successes or rate_limits):
        authentication_state = "authenticated"
    elif authenticated_requests:
        authentication_state = "request_only"
    else:
        authentication_state = "unknown"

    if authentication_state == "logged_out" and not successes:
        classification = "authentication_failed"
        detail = "The X capture reached a login page instead of authenticated search content."
    elif rate_limits or (error_shell and successes):
        classification = "rate_limited_partial" if successes else "rate_limited_empty"
        detail = (
            f"X SearchTimeline returned rate-limit/error content after {successes} successful response(s)."
            if successes
            else "X SearchTimeline was rate limited before any search response completed."
        )
    elif error_shell:
        classification = "rate_limited_shell"
        detail = "X returned its transient 'Something went wrong' search shell without usable timeline data."
    elif other_statuses:
        if successes:
            classification = "invalid_partial"
            detail = (
                f"X SearchTimeline returned {successes} usable response(s) and "
                f"{other_statuses} failed or malformed response(s); the capture may be partial."
            )
        else:
            classification = "invalid"
            detail = "The WACZ contains X SearchTimeline traffic, but no response had a usable timeline payload."
    elif successes:
        classification = "valid"
        detail = f"Validated {successes} successful X SearchTimeline response(s)."
    else:
        classification = "invalid"
        detail = "The X archive contains no SearchTimeline response, so capture completeness cannot be verified."

    return XCaptureInspection(
        classification=classification,
        search_successes=successes,
        search_rate_limits=rate_limits,
        search_other_statuses=other_statuses,
        rate_limit_remaining=remaining,
        rate_limit_reset=reset,
        error_shell=error_shell,
        detail=detail,
        authentication_state=authentication_state,
        authenticated_requests=authenticated_requests,
        guest_requests=guest_requests,
        logged_in_ui=logged_in_ui,
        login_ui=login_ui,
    )


def _json_payloads(body: bytes) -> Iterator[Any]:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return
    if text.startswith("for (;;);"):
        text = text[len("for (;;);") :].lstrip()
    try:
        yield json.loads(text)
        return
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        candidate = line.strip()
        if candidate.startswith("for (;;);"):
            candidate = candidate[len("for (;;);") :].lstrip()
        if not candidate:
            continue
        try:
            yield json.loads(candidate)
        except json.JSONDecodeError:
            continue


def _nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _walk_dicts(value: Any) -> Iterator[dict[str, Any]]:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))


def _iso_post_date(value: Any) -> str:
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _media_key(url: str) -> str:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").casefold()
    path = parsed.path
    if host == "pbs.twimg.com" and path.startswith("/media/"):
        path = re.sub(r"\.(?:avif|gif|jpe?g|png|webp)$", "", path, flags=re.I)
    return host + path


def _x_user(result: dict[str, Any]) -> tuple[str, str]:
    user = _nested(result, "core", "user_results", "result")
    if not isinstance(user, dict):
        return "", ""
    legacy = user.get("legacy") if isinstance(user.get("legacy"), dict) else {}
    core = user.get("core") if isinstance(user.get("core"), dict) else {}
    handle = str(legacy.get("screen_name") or core.get("screen_name") or "").strip()
    name = str(legacy.get("name") or core.get("name") or "").strip()
    return handle, name


def _unwrap_x_tweet(value: Any) -> dict[str, Any] | None:
    result = value
    for _index in range(4):
        if not isinstance(result, dict):
            return None
        if isinstance(result.get("tweet"), dict):
            result = result["tweet"]
            continue
        if not result.get("legacy") and isinstance(result.get("result"), dict):
            result = result["result"]
            continue
        break
    return result if isinstance(result, dict) and isinstance(result.get("legacy"), dict) else None


def _x_post_from_item(item_content: dict[str, Any], source_url: str) -> dict[str, Any] | None:
    result = _unwrap_x_tweet(_nested(item_content, "tweet_results", "result"))
    if result is None:
        return None
    legacy = result["legacy"]
    post_id = str(result.get("rest_id") or legacy.get("id_str") or "").strip()
    if not post_id:
        return None
    handle, author_name = _x_user(result)
    note = _nested(result, "note_tweet", "note_tweet_results", "result")
    note_text = str(note.get("text") or "").strip() if isinstance(note, dict) else ""
    text = note_text or str(legacy.get("full_text") or legacy.get("text") or "").strip()
    media_items = _nested(legacy, "extended_entities", "media") or _nested(legacy, "entities", "media") or []
    media: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    if isinstance(media_items, list):
        for item in media_items:
            if not isinstance(item, dict) or str(item.get("type") or "").casefold() != "photo":
                continue
            media_url = str(item.get("media_url_https") or item.get("media_url") or "").strip()
            if media_url and media_url not in seen_urls:
                seen_urls.add(media_url)
                media.append({"type": "image", "source_url": media_url})
    return {
        "platform": "x",
        "post_id": post_id,
        "url": f"https://x.com/{handle}/status/{post_id}" if handle else f"https://x.com/i/status/{post_id}",
        "account_handle": handle,
        "author_name": author_name,
        "published_at": _iso_post_date(legacy.get("created_at")),
        "text": text,
        "media": media,
        "source_response": source_url.split("?", 1)[0],
    }


def _x_posts(payload: Any, source_url: str, expected_handle: str = "") -> Iterator[dict[str, Any]]:
    for node in _walk_dicts(payload):
        item_content = node.get("itemContent")
        if not isinstance(item_content, dict) or not isinstance(item_content.get("tweet_results"), dict):
            continue
        post = _x_post_from_item(item_content, source_url)
        if post is None:
            continue
        if expected_handle and str(post.get("account_handle") or "").casefold() != expected_handle.casefold():
            continue
        yield post


def _facebook_message(node: dict[str, Any]) -> str:
    message = node.get("message")
    if isinstance(message, dict):
        return str(message.get("text") or message.get("story") or "").strip()
    return str(message or "").strip()


def _facebook_url(node: dict[str, Any]) -> str:
    for key in ("url", "wwwURL", "permalink_url", "story_url"):
        value = str(node.get(key) or "").strip()
        if value.startswith(("http://", "https://")) and (
            "/posts/" in value or "story_fbid=" in value or "/permalink/" in value
        ):
            return value
    return ""


def _facebook_images(value: Any) -> list[dict[str, str]]:
    urls: list[str] = []
    for node in _walk_dicts(value):
        typename = str(node.get("__typename") or "").casefold()
        for key in ("uri", "url"):
            candidate = str(node.get(key) or "").strip()
            if not candidate.startswith(("http://", "https://")):
                continue
            parsed = urlparse(candidate)
            description = (candidate + " " + typename).casefold()
            if not (
                (parsed.hostname or "").casefold().endswith("fbcdn.net")
                or typename in {"image", "photo", "photomedia"}
            ):
                continue
            if re.search(r"(?:emoji|profile|avatar|safe_image|static_map)", description):
                continue
            urls.append(candidate)
    return [{"type": "image", "source_url": url} for url in dict.fromkeys(urls)]


def _facebook_posts(payload: Any, source_url: str, expected_slug: str = "") -> Iterator[dict[str, Any]]:
    for node in _walk_dicts(payload):
        if str(node.get("__typename") or node.get("__isFeedUnit") or "") != "Story":
            continue
        post_id = str(node.get("post_id") or "").strip()
        url = _facebook_url(node)
        published_at = _iso_post_date(node.get("creation_time") or node.get("creation_timestamp"))
        if not post_id or not (published_at or url):
            continue
        url_slug = next((part for part in urlparse(url).path.split("/") if part), "")
        if expected_slug and url_slug.casefold() != expected_slug.casefold():
            continue
        related_nodes = [
            descendant
            for descendant in _walk_dicts(node)
            if str(descendant.get("post_id") or "").strip() == post_id
        ]
        messages = [_facebook_message(descendant) for descendant in related_nodes]
        text = max((message for message in messages if message), key=len, default="")
        if not text:
            continue
        attachments = [
            descendant.get("attachments") or descendant.get("attachment")
            for descendant in related_nodes
            if descendant.get("attachments") or descendant.get("attachment")
        ]
        actors = node.get("actors") if isinstance(node.get("actors"), list) else []
        actor = actors[0] if actors and isinstance(actors[0], dict) else node.get("actor")
        actor = actor if isinstance(actor, dict) else {}
        yield {
            "platform": "facebook",
            "post_id": post_id,
            "url": url,
            "account_handle": "",
            "author_name": str(actor.get("name") or "").strip(),
            "published_at": published_at,
            "text": text,
            "media": _facebook_images(attachments),
            "source_response": source_url.split("?", 1)[0],
        }


def _merge_post(posts: dict[str, dict[str, Any]], post: dict[str, Any]) -> None:
    key = f"{post.get('platform')}:{post.get('post_id')}"
    existing = posts.get(key)
    if existing is None:
        posts[key] = post
        return
    if len(str(post.get("text") or "")) > len(str(existing.get("text") or "")):
        existing["text"] = post["text"]
    for field in ("url", "account_handle", "author_name", "published_at"):
        if not existing.get(field) and post.get(field):
            existing[field] = post[field]
    combined = list(existing.get("media") or []) + list(post.get("media") or [])
    existing["media"] = list(
        {str(item.get("source_url") or ""): item for item in combined if item.get("source_url")}.values()
    )


def _write_posts_csv(path: Path, posts: list[dict[str, Any]]) -> None:
    fields = [
        "platform",
        "post_id",
        "url",
        "account_handle",
        "author_name",
        "published_at",
        "text",
        "image_count",
        "image_files",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for post in posts:
            media = list(post.get("media") or [])
            writer.writerow(
                {
                    **{field: post.get(field, "") for field in fields[:-2]},
                    "image_count": len(media),
                    "image_files": " | ".join(str(item.get("file") or item.get("source_url") or "") for item in media),
                }
            )


def extract_wacz_content(
    wacz_path: Path,
    output_dir: Path,
    *,
    platform: str = "",
    expected_x_handle: str = "",
    expected_facebook_slug: str = "",
    period_start: str = "",
    period_end: str = "",
    validation_status: str = "",
) -> dict[str, Any]:
    """Create an n8n-friendly text and image bundle from a Browsertrix WACZ."""
    output_dir.mkdir(parents=True, exist_ok=True)
    media_dir = output_dir / "media"
    media_dir.mkdir(exist_ok=True)

    final_text: dict[str, str] = {}
    initial_text: dict[str, str] = {}
    posts_by_key: dict[str, dict[str, Any]] = {}

    with zipfile.ZipFile(wacz_path) as archive:
        pages = _page_entries(archive)
        warc_names = [
            name
            for name in archive.namelist()
            if name.startswith("archive/") and (name.endswith(".warc") or name.endswith(".warc.gz"))
        ]
        for warc_name in warc_names:
            with archive.open(warc_name) as raw_warc:
                stream: BinaryIO
                if warc_name.endswith(".gz"):
                    stream = gzip.GzipFile(fileobj=raw_warc)
                else:
                    stream = raw_warc
                for headers, block in _warc_records(stream):
                    target = headers.get("warc-target-uri", "")
                    record_type = headers.get("warc-type", "").casefold()
                    text_url = _text_target_url(target)
                    if record_type == "resource" and text_url:
                        text = _decode_text(block)
                        if target.startswith("urn:textFinal:"):
                            final_text[text_url] = text
                        elif text_url not in initial_text:
                            initial_text[text_url] = text
                        continue
                    if record_type != "response" or not target.startswith(("http://", "https://")):
                        continue
                    http_headers, body = _http_payload(block)
                    content_type = http_headers.get("content-type", "").split(";", 1)[0].casefold().strip()
                    if not body or not (
                        "json" in content_type
                        or "/SearchTimeline" in target
                        or ("facebook.com" in target and "/graphql" in target)
                    ):
                        continue
                    for payload in _json_payloads(body):
                        if platform == "x" or "/SearchTimeline" in target:
                            for post in _x_posts(payload, target, expected_x_handle):
                                _merge_post(posts_by_key, post)
                        if platform == "facebook" and "facebook.com" in target:
                            for post in _facebook_posts(payload, target, expected_facebook_slug):
                                _merge_post(posts_by_key, post)

        posts = sorted(
            posts_by_key.values(),
            key=lambda item: (str(item.get("published_at") or ""), str(item.get("post_id") or "")),
        )
        desired_media_keys = {
            _media_key(str(item.get("source_url") or ""))
            for post in posts
            for item in list(post.get("media") or [])
            if item.get("source_url")
        }
        focused_media = platform in {"facebook", "x"}
        media: list[dict[str, Any]] = []
        media_by_key: dict[str, dict[str, Any]] = {}
        media_by_digest: dict[str, dict[str, Any]] = {}
        total_media_bytes = 0
        for warc_name in warc_names:
            with archive.open(warc_name) as raw_warc:
                stream = gzip.GzipFile(fileobj=raw_warc) if warc_name.endswith(".gz") else raw_warc
                for headers, block in _warc_records(stream):
                    target = headers.get("warc-target-uri", "")
                    if headers.get("warc-type", "").casefold() != "response" or not target.startswith(
                        ("http://", "https://")
                    ):
                        continue
                    http_headers, body = _http_payload(block)
                    content_type = http_headers.get("content-type", "").split(";", 1)[0].casefold().strip()
                    key = _media_key(target)
                    if not content_type.startswith("image/") or not body or (focused_media and key not in desired_media_keys):
                        continue
                    if len(body) > MAX_MEDIA_FILE_BYTES or total_media_bytes + len(body) > MAX_MEDIA_TOTAL_BYTES:
                        continue
                    digest = hashlib.sha256(body).hexdigest()
                    existing = media_by_digest.get(digest)
                    if existing is not None:
                        media_by_key[key] = existing
                        continue
                    filename = f"{len(media) + 1:04d}-{digest[:12]}{_extension(content_type, target)}"
                    destination = media_dir / filename
                    destination.write_bytes(body)
                    total_media_bytes += len(body)
                    entry = {
                        "source_url": target,
                        "content_type": content_type,
                        "bytes": len(body),
                        "sha256": digest,
                        "file": f"media/{filename}",
                    }
                    media.append(entry)
                    media_by_key[key] = entry
                    media_by_digest[digest] = entry

        for post in posts:
            linked_media: list[dict[str, Any]] = []
            for item in list(post.get("media") or []):
                captured = media_by_key.get(_media_key(str(item.get("source_url") or "")))
                linked_media.append({**item, **({"file": captured["file"], "sha256": captured["sha256"]} if captured else {})})
            post["media"] = linked_media

    document_urls = set(pages) | set(initial_text) | set(final_text)
    documents: list[dict[str, Any]] = []
    for url in sorted(document_urls):
        page = pages.get(url, {})
        text = final_text.get(url) or str(page.get("text") or "").strip() or initial_text.get(url, "")
        if not text:
            continue
        documents.append(
            {
                "url": url,
                "title": str(page.get("title") or ""),
                "captured_at": str(page.get("ts") or ""),
                "text": text,
            }
        )

    published_dates = [str(post.get("published_at") or "") for post in posts if post.get("published_at")]
    if validation_status in {"rate_limited_partial", "invalid_partial"}:
        completeness_status = "partial"
    elif posts:
        completeness_status = "best_effort_no_errors_observed"
    else:
        completeness_status = "unknown_no_structured_posts"
    bundle = {
        "format": "social-profile-content-2.0",
        "source_wacz": str(wacz_path.resolve()),
        "platform": platform,
        "requested_period": {"start": period_start, "end": period_end},
        "completeness_status": completeness_status,
        "posts": posts,
        "post_count": len(posts),
        "oldest_post_at": min(published_dates) if published_dates else "",
        "newest_post_at": max(published_dates) if published_dates else "",
        "documents": documents,
        "media": media,
        "document_count": len(documents),
        "media_count": len(media),
        "notes": [
            "Structured posts are extracted from archived platform responses when available; page text remains as a replay/QA fallback.",
            "Completeness is best-effort because a platform may stop returning older results without an explicit error.",
            "For Facebook and X, copied media is limited to image URLs associated with extracted posts when structured records are available.",
        ],
    }
    content_path = output_dir / "content.json"
    content_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    posts_csv_path = output_dir / "posts.csv"
    if posts:
        _write_posts_csv(posts_csv_path, posts)
    return {
        "content_path": str(content_path.resolve()),
        "content_dir": str(output_dir.resolve()),
        "posts_csv_path": str(posts_csv_path.resolve()) if posts else "",
        "post_count": len(posts),
        "oldest_post_at": bundle["oldest_post_at"],
        "newest_post_at": bundle["newest_post_at"],
        "completeness_status": completeness_status,
        "document_count": len(documents),
        "media_count": len(media),
    }
