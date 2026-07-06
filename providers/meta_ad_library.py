from __future__ import annotations

import os
import time
from typing import Any, Iterator

from common import HTTP_TIMEOUT, OsintApiError, make_session


GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v25.0").strip() or "v25.0"
GRAPH_BASE = os.getenv("META_GRAPH_BASE", "https://graph.facebook.com").rstrip("/")
ENV_TOKEN = "META_ACCESS_TOKEN"
FALLBACK_ENV_TOKEN = "META_AD_LIBRARY_ACCESS_TOKEN"

AD_FIELDS = [
    "id",
    "ad_creation_time",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_snapshot_url",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "ad_creative_link_titles",
    "page_id",
    "page_name",
    "publisher_platforms",
    "spend",
    "impressions",
    "demographic_distribution",
    "delivery_by_region",
    "funding_entity",
    "currency",
]


def normalize_page_ids(raw: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in str(raw or "").replace(",", " ").replace(";", " ").split():
        token = token.strip()
        if token.isdigit() and token not in seen:
            out.append(token)
            seen.add(token)
    return out


def chunk_page_ids(page_ids: list[str], size: int = 10) -> Iterator[list[str]]:
    for idx in range(0, len(page_ids), size):
        yield page_ids[idx : idx + size]


def chunk_search_terms(terms: list[str], size: int = 1) -> Iterator[list[str]]:
    for idx in range(0, len(terms), size):
        yield terms[idx : idx + size]


def dedupe_by_ad_id(items: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    seen: set[str] = set()
    for item in items:
        ad_id = str(item.get("id") or "")
        if ad_id and ad_id in seen:
            continue
        if ad_id:
            seen.add(ad_id)
        yield item


def build_ad_snapshot_url(ad_id: str) -> str:
    return f"https://www.facebook.com/ads/library/?id={ad_id}" if ad_id else ""


class MetaAdLibraryClient:
    def __init__(self, access_token: str | None = None, session: Any | None = None):
        self.access_token = (access_token or os.getenv(ENV_TOKEN) or os.getenv(FALLBACK_ENV_TOKEN) or "").strip()
        if not self.access_token:
            raise RuntimeError(f"Set {ENV_TOKEN} or {FALLBACK_ENV_TOKEN}, or paste a Meta Ad Library token.")
        self.session = session or make_session()

    @property
    def api_url(self) -> str:
        return f"{GRAPH_BASE}/{GRAPH_API_VERSION}/ads_archive"

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(self.api_url, params=params, timeout=HTTP_TIMEOUT)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                wait = min(float(retry_after), 30.0) if retry_after else 3.0
            except ValueError:
                wait = 3.0
            time.sleep(wait)
            response = self.session.get(self.api_url, params=params, timeout=HTTP_TIMEOUT)
        if not response.ok:
            body = response.text[:1000] if response.text else ""
            raise OsintApiError(f"Meta Ad Library request failed: HTTP {response.status_code}", status_code=response.status_code, url=response.url, body=body)
        try:
            return response.json()
        except Exception as exc:
            raise OsintApiError(f"Meta Ad Library response was not valid JSON: {exc}", status_code=response.status_code, url=response.url, body=response.text[:1000]) from exc

    def iter_ads(self, params: dict[str, Any], *, max_results: int = 250, delay: float = 0.0) -> Iterator[dict[str, Any]]:
        returned = 0
        after = ""
        while returned < max_results:
            request_params = dict(params)
            request_params["access_token"] = self.access_token
            request_params["fields"] = request_params.get("fields") or ",".join(AD_FIELDS)
            request_params["limit"] = min(100, max_results - returned)
            if after:
                request_params["after"] = after
            data = self._get(request_params)
            ads = data.get("data") or []
            if not ads:
                break
            for ad in ads:
                if not isinstance(ad, dict):
                    continue
                returned += 1
                yield ad
                if returned >= max_results:
                    break
            after = (((data.get("paging") or {}).get("cursors") or {}).get("after") or "")
            if not after:
                break
            if delay:
                time.sleep(delay)
