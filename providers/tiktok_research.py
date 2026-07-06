from __future__ import annotations

import os
import time
from typing import Any

from common import HTTP_TIMEOUT, OsintApiError, make_session


TIKTOK_OPEN_BASE = os.getenv("TIKTOK_OPEN_BASE", "https://open.tiktokapis.com").rstrip("/")
ENV_KEY = "TIKTOK_RESEARCH_CLIENT_KEY"
ENV_SECRET = "TIKTOK_RESEARCH_CLIENT_SECRET"


class TikTokResearchClient:
    def __init__(self, client_key: str | None = None, client_secret: str | None = None, session: Any | None = None):
        self.client_key = (client_key or os.getenv(ENV_KEY) or "").strip()
        self.client_secret = (client_secret or os.getenv(ENV_SECRET) or "").strip()
        if not self.client_key or not self.client_secret:
            raise RuntimeError(f"Set {ENV_KEY} and {ENV_SECRET} after TikTok approves the research project.")
        self.session = session or make_session()
        self._token = ""
        self._token_expires_at = 0.0

    def get_client_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        url = f"{TIKTOK_OPEN_BASE}/v2/oauth/token/"
        response = self.session.post(
            url,
            data={
                "client_key": self.client_key,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=HTTP_TIMEOUT,
        )
        if not response.ok:
            raise OsintApiError("TikTok research token request failed.", status_code=response.status_code, url=response.url, body=response.text[:1000])
        data = response.json()
        token = data.get("access_token") or (data.get("data") or {}).get("access_token")
        if not token:
            raise RuntimeError("TikTok research token response did not contain access_token.")
        expires_in = int(data.get("expires_in") or (data.get("data") or {}).get("expires_in") or 3600)
        self._token = str(token)
        self._token_expires_at = time.time() + expires_in
        return self._token

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{TIKTOK_OPEN_BASE}/{path.lstrip('/')}"
        response = self.session.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {self.get_client_token()}"},
            timeout=HTTP_TIMEOUT,
        )
        if not response.ok:
            raise OsintApiError("TikTok Research API request failed.", status_code=response.status_code, url=response.url, body=response.text[:1000])
        return response.json()

    def query_videos(self, query: dict[str, Any], fields: list[str], max_count: int, cursor: int = 0, search_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query, "fields": fields, "max_count": max_count, "cursor": cursor}
        if search_id:
            payload["search_id"] = search_id
        return self._post("/v2/research/video/query/", payload)

    def query_video_comments(self, video_id: str, cursor: int = 0, max_count: int = 100) -> dict[str, Any]:
        return self._post("/v2/research/video/comment/list/", {"video_id": video_id, "cursor": cursor, "max_count": max_count})

    def query_user_info(self, username: str, fields: list[str]) -> dict[str, Any]:
        return self._post("/v2/research/user/info/", {"username": username, "fields": fields})
