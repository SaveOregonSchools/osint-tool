from __future__ import annotations

from typing import Any, Iterator

from common import HTTP_TIMEOUT, OsintApiError, make_session


COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"


class CommonCrawlClient:
    def __init__(self, session: Any | None = None):
        self.session = session or make_session()

    def collections(self) -> list[dict[str, Any]]:
        response = self.session.get(COLLINFO_URL, timeout=HTTP_TIMEOUT)
        if not response.ok:
            raise OsintApiError("Common Crawl collection list request failed.", status_code=response.status_code, url=response.url, body=response.text[:1000])
        return [item for item in response.json() if isinstance(item, dict)]

    def iter_cdx(self, url: str, *, collection: str = "", limit: int = 100) -> Iterator[dict[str, Any]]:
        collections = self.collections()
        selected = next((item for item in collections if item.get("id") == collection), collections[0] if collections else None)
        if not selected:
            raise RuntimeError("No Common Crawl CDX collections were returned.")
        cdx_api = selected.get("cdx-api")
        response = self.session.get(cdx_api, params={"url": url, "output": "json", "limit": limit}, timeout=HTTP_TIMEOUT)
        if not response.ok:
            raise OsintApiError("Common Crawl CDX request failed.", status_code=response.status_code, url=response.url, body=response.text[:1000])
        for line in response.text.splitlines():
            line = line.strip()
            if not line:
                continue
            import json

            item = json.loads(line)
            if isinstance(item, dict):
                item["_collection"] = selected.get("id") or ""
                yield item
