from __future__ import annotations

from typing import Any, Iterator

from common import HTTP_TIMEOUT, OsintApiError, make_session


AVAILABILITY_URL = "https://archive.org/wayback/available"
CDX_URL = "https://web.archive.org/cdx"
SAVE_URL = "https://web.archive.org/save"


class WaybackClient:
    def __init__(self, session: Any | None = None):
        self.session = session or make_session()

    def availability(self, url: str, timestamp: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {"url": url}
        if timestamp:
            params["timestamp"] = timestamp
        response = self.session.get(AVAILABILITY_URL, params=params, timeout=HTTP_TIMEOUT)
        if not response.ok:
            raise OsintApiError("Wayback Availability API request failed.", status_code=response.status_code, url=response.url, body=response.text[:1000])
        return response.json()

    def iter_cdx(self, url: str, *, from_ts: str = "", to_ts: str = "", limit: int = 100) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"url": url, "output": "json", "fl": "timestamp,original,statuscode,mimetype,digest"}
        if from_ts:
            params["from"] = from_ts
        if to_ts:
            params["to"] = to_ts
        if limit:
            params["limit"] = limit
        response = self.session.get(CDX_URL, params=params, timeout=HTTP_TIMEOUT)
        if not response.ok:
            raise OsintApiError("Wayback CDX API request failed.", status_code=response.status_code, url=response.url, body=response.text[:1000])
        rows = response.json()
        if not rows:
            return
        headers = rows[0]
        for values in rows[1:]:
            yield dict(zip(headers, values))

    def save_page_now(self, url: str) -> dict[str, Any]:
        response = self.session.get(f"{SAVE_URL}/{url}", timeout=HTTP_TIMEOUT)
        if response.status_code not in {200, 302}:
            raise OsintApiError("Wayback Save Page Now request failed.", status_code=response.status_code, url=response.url, body=response.text[:1000])
        return {"archive_url": response.headers.get("Content-Location") or response.url, "status_code": response.status_code}


def normalize_archive_url(timestamp: str, original: str) -> str:
    if not timestamp or not original:
        return ""
    return f"https://web.archive.org/web/{timestamp}/{original}"
