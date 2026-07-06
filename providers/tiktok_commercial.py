from __future__ import annotations

import os
from typing import Any, Iterator

from common import HTTP_TIMEOUT, OsintApiError, make_session


TIKTOK_COMMERCIAL_BASE = os.getenv("TIKTOK_COMMERCIAL_BASE", "https://open.tiktokapis.com").rstrip("/")
ENV_KEY = "TIKTOK_COMMERCIAL_CLIENT_KEY"
ENV_SECRET = "TIKTOK_COMMERCIAL_CLIENT_SECRET"


class TikTokCommercialClient:
    def __init__(self, client_key: str | None = None, client_secret: str | None = None, session: Any | None = None):
        self.client_key = (client_key or os.getenv(ENV_KEY) or "").strip()
        self.client_secret = (client_secret or os.getenv(ENV_SECRET) or "").strip()
        if not self.client_key or not self.client_secret:
            raise RuntimeError(f"Set {ENV_KEY} and {ENV_SECRET} after TikTok Commercial Content API access is approved.")
        self.session = session or make_session()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            f"{TIKTOK_COMMERCIAL_BASE}/{path.lstrip('/')}",
            json=payload,
            auth=(self.client_key, self.client_secret),
            timeout=HTTP_TIMEOUT,
        )
        if not response.ok:
            raise OsintApiError("TikTok Commercial Content API request failed.", status_code=response.status_code, url=response.url, body=response.text[:1000])
        return response.json()

    def iter_ads(self, query: dict[str, Any], *, max_results: int = 100) -> Iterator[dict[str, Any]]:
        cursor = 0
        returned = 0
        while returned < max_results:
            data = self._post("/v2/research/adlib/ad/query/", {"query": query, "max_count": min(100, max_results - returned), "cursor": cursor})
            ads = (data.get("data") or {}).get("ads") or data.get("ads") or []
            if not ads:
                break
            for ad in ads:
                if not isinstance(ad, dict):
                    continue
                returned += 1
                yield ad
                if returned >= max_results:
                    break
            cursor = int((data.get("data") or {}).get("cursor") or data.get("cursor") or 0)
            has_more = bool((data.get("data") or {}).get("has_more") or data.get("has_more"))
            if not has_more:
                break
