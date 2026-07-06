from __future__ import annotations

import os
from typing import Any, Iterator

from common import HTTP_TIMEOUT, OsintApiError, make_session


GDELT_DOC_API = os.getenv("GDELT_DOC_API", "https://api.gdeltproject.org/api/v2/doc/doc")


class GdeltClient:
    def __init__(self, session: Any | None = None):
        self.session = session or make_session()

    def iter_articles(self, *, query: str, date_min: str = "", date_max: str = "", max_results: int = 100) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": max(1, min(max_results, 250)),
            "sort": "hybridrel",
        }
        if date_min:
            params["startdatetime"] = date_min.replace("-", "") + "000000"
        if date_max:
            params["enddatetime"] = date_max.replace("-", "") + "235959"
        response = self.session.get(GDELT_DOC_API, params=params, timeout=HTTP_TIMEOUT)
        if not response.ok:
            raise OsintApiError("GDELT API request failed.", status_code=response.status_code, url=response.url, body=response.text[:1000])
        data = response.json()
        for item in (data.get("articles") or [])[:max_results]:
            if isinstance(item, dict):
                yield item
