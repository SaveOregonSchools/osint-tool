from __future__ import annotations

import os
from typing import Any

from common import HTTP_TIMEOUT, OsintApiError, make_session


SPIDERFOOT_BASE_URL = os.getenv("SPIDERFOOT_BASE_URL", "http://127.0.0.1:5001").rstrip("/")
ENV_API_KEY = "SPIDERFOOT_API_KEY"


class SpiderFootClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, session: Any | None = None):
        self.base_url = (base_url or SPIDERFOOT_BASE_URL).rstrip("/")
        self.api_key = (api_key or os.getenv(ENV_API_KEY) or "").strip()
        self.session = session or make_session()

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self.api_key} if self.api_key else {}

    def start_scan(self, target: str, modules: str = "") -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/startscan",
            data={"scanname": f"osint-tool {target}", "scantarget": target, "modulelist": modules},
            headers=self._headers(),
            timeout=HTTP_TIMEOUT,
        )
        if not response.ok:
            raise OsintApiError("SpiderFoot startscan request failed.", status_code=response.status_code, url=response.url, body=response.text[:1000])
        return response.json()

    def scan_results(self, scan_id: str) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/scaneventresults", params={"id": scan_id}, headers=self._headers(), timeout=HTTP_TIMEOUT)
        if not response.ok:
            raise OsintApiError("SpiderFoot results request failed.", status_code=response.status_code, url=response.url, body=response.text[:1000])
        return response.json()
