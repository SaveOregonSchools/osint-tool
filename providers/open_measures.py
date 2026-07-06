from __future__ import annotations

import os
from typing import Any, Iterator

from common import HTTP_TIMEOUT, OsintApiError, make_session


OPENMEASURES_BASE_URL = os.getenv("OPENMEASURES_BASE_URL", "https://api.openmeasures.io").rstrip("/")
ENV_API_KEY = "OPENMEASURES_API_KEY"


class OpenMeasuresClient:
    def __init__(self, api_key: str | None = None, session: Any | None = None):
        self.api_key = (api_key or os.getenv(ENV_API_KEY) or "").strip()
        if not self.api_key:
            raise RuntimeError(f"Set {ENV_API_KEY} for Open Measures API access.")
        self.session = session or make_session()

    def iter_content(self, *, query: str, platforms: list[str], date_min: str = "", date_max: str = "", max_results: int = 100) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"q": query, "limit": min(max_results, 100)}
        if platforms:
            params["platforms"] = ",".join(platforms)
        if date_min:
            params["start_date"] = date_min
        if date_max:
            params["end_date"] = date_max
        response = self.session.get(
            f"{OPENMEASURES_BASE_URL}/content",
            params=params,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=HTTP_TIMEOUT,
        )
        if not response.ok:
            raise OsintApiError("Open Measures API request failed.", status_code=response.status_code, url=response.url, body=response.text[:1000])
        data = response.json()
        items = data.get("results") or data.get("data") or []
        for item in items[:max_results]:
            if isinstance(item, dict):
                yield item
