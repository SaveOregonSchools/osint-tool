from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import urlparse

from common import HTTP_TIMEOUT, OsintApiError, make_session


YOUTUBE_API_BASE = os.getenv("YOUTUBE_API_BASE", "https://www.googleapis.com/youtube/v3").rstrip("/")
ENV_API_KEY = "YOUTUBE_API_KEY"


class YouTubeDataClient:
    def __init__(self, api_key: str | None = None, session: Any | None = None):
        self.api_key = (api_key or os.getenv(ENV_API_KEY) or "").strip()
        if not self.api_key:
            raise RuntimeError(f"Set {ENV_API_KEY} or paste a YouTube Data API key.")
        self.session = session or make_session()

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{YOUTUBE_API_BASE}/{endpoint.lstrip('/')}"
        query = dict(params)
        query["key"] = self.api_key
        response = self.session.get(url, params=query, timeout=HTTP_TIMEOUT)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                wait = min(float(retry_after), 30.0) if retry_after else 3.0
            except ValueError:
                wait = 3.0
            time.sleep(wait)
            response = self.session.get(url, params=query, timeout=HTTP_TIMEOUT)
        if not response.ok:
            body = response.text[:1000] if response.text else ""
            raise OsintApiError(f"YouTube Data API request failed: HTTP {response.status_code}", status_code=response.status_code, url=response.url, body=body)
        try:
            return response.json()
        except Exception as exc:
            raise OsintApiError(f"YouTube Data API response was not valid JSON: {exc}", status_code=response.status_code, url=response.url, body=response.text[:1000]) from exc

    def normalize_channel_input(self, target: str) -> dict[str, str]:
        raw = (target or "").strip()
        if not raw:
            raise ValueError("Enter a YouTube channel URL, @handle, channel ID, or username.")
        if raw.startswith("http://") or raw.startswith("https://"):
            parsed = urlparse(raw)
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0].lower() == "channel":
                return {"id": parts[1], "kind": "id", "input": raw}
            if parts and parts[0].startswith("@"):
                return {"handle": parts[0], "kind": "handle", "input": raw}
            if len(parts) >= 2 and parts[0].lower() == "user":
                return {"forUsername": parts[1], "kind": "username", "input": raw}
            if parts and re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", parts[-1]):
                return {"id": parts[-1], "kind": "id", "input": raw}
        stripped = raw.strip("/")
        if stripped.startswith("@"):
            return {"handle": stripped, "kind": "handle", "input": raw}
        if re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", stripped):
            return {"id": stripped, "kind": "id", "input": raw}
        return {"forHandle": stripped if stripped.startswith("@") else f"@{stripped}", "kind": "handle", "input": raw}

    def resolve_channel(self, target: str) -> dict[str, Any]:
        normalized = self.normalize_channel_input(target)
        params: dict[str, Any] = {"part": "snippet,contentDetails,statistics", "maxResults": 1}
        if normalized.get("id"):
            params["id"] = normalized["id"]
        elif normalized.get("forUsername"):
            params["forUsername"] = normalized["forUsername"]
        else:
            params["forHandle"] = normalized.get("handle") or normalized.get("forHandle")
        data = self._get("channels", params)
        items = data.get("items") or []
        if not items:
            raise RuntimeError(f"No YouTube channel found for {target!r}.")
        channel = items[0]
        channel["_target_input"] = normalized["input"]
        return channel

    def iter_upload_videos(
        self,
        channel: dict[str, Any],
        *,
        max_videos: int = 50,
        delay: float = 0.0,
    ) -> Iterator[dict[str, Any]]:
        related = ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {})
        playlist_id = related.get("uploads")
        if not playlist_id:
            return
        returned = 0
        page_token = ""
        while returned < max_videos:
            data = self._get(
                "playlistItems",
                {
                    "part": "snippet,contentDetails",
                    "playlistId": playlist_id,
                    "maxResults": min(50, max_videos - returned),
                    **({"pageToken": page_token} if page_token else {}),
                },
            )
            items = data.get("items") or []
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                returned += 1
                yield item
                if returned >= max_videos:
                    break
            page_token = data.get("nextPageToken") or ""
            if not page_token:
                break
            if delay:
                time.sleep(delay)

    def search_videos(
        self,
        query: str,
        *,
        channel_id: str = "",
        published_after: str = "",
        published_before: str = "",
        max_results: int = 50,
        delay: float = 0.0,
    ) -> Iterator[dict[str, Any]]:
        returned = 0
        page_token = ""
        while returned < max_results:
            params: dict[str, Any] = {
                "part": "snippet",
                "type": "video",
                "q": query,
                "order": "date",
                "maxResults": min(50, max_results - returned),
            }
            if channel_id:
                params["channelId"] = channel_id
            if published_after:
                params["publishedAfter"] = published_after
            if published_before:
                params["publishedBefore"] = published_before
            if page_token:
                params["pageToken"] = page_token
            data = self._get("search", params)
            items = data.get("items") or []
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                returned += 1
                yield item
                if returned >= max_results:
                    break
            page_token = data.get("nextPageToken") or ""
            if not page_token:
                break
            if delay:
                time.sleep(delay)

    def iter_video_comments(self, video_id: str, *, max_comments: int = 100, delay: float = 0.0) -> Iterator[dict[str, Any]]:
        returned = 0
        page_token = ""
        while returned < max_comments:
            params: dict[str, Any] = {
                "part": "snippet,replies",
                "videoId": video_id,
                "maxResults": min(100, max_comments - returned),
                "textFormat": "plainText",
                "order": "relevance",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._get("commentThreads", params)
            items = data.get("items") or []
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                returned += 1
                yield item
                if returned >= max_comments:
                    break
            page_token = data.get("nextPageToken") or ""
            if not page_token:
                break
            if delay:
                time.sleep(delay)


def youtube_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""


def youtube_comment_url(video_id: str, comment_id: str) -> str:
    if not video_id:
        return ""
    if not comment_id:
        return youtube_video_url(video_id)
    return f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"


def iso_z_from_date(value: str, *, end_of_day: bool = False) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    suffix = "T23:59:59Z" if end_of_day else "T00:00:00Z"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw + suffix
    return raw


def within_date_window(created_at: str, date_min: str = "", date_max: str = "") -> bool:
    if not created_at:
        return True
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return True
    if date_min:
        try:
            if created < datetime.fromisoformat(iso_z_from_date(date_min).replace("Z", "+00:00")).astimezone(timezone.utc):
                return False
        except ValueError:
            pass
    if date_max:
        try:
            if created > datetime.fromisoformat(iso_z_from_date(date_max, end_of_day=True).replace("Z", "+00:00")).astimezone(timezone.utc):
                return False
        except ValueError:
            pass
    return True
