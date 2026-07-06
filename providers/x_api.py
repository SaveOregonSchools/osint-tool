from __future__ import annotations

import os
import time
from typing import Any, Iterator

from common import HTTP_TIMEOUT, OsintApiError, make_session


X_API_BASE = os.getenv("X_API_BASE", "https://api.x.com/2").rstrip("/")
ENV_TOKEN = "X_BEARER_TOKEN"


class XApiClient:
    def __init__(self, bearer_token: str | None = None, session: Any | None = None):
        self.bearer_token = (bearer_token or os.getenv(ENV_TOKEN) or "").strip()
        if not self.bearer_token:
            raise RuntimeError(f"Set {ENV_TOKEN} or paste an X API bearer token.")
        self.session = session or make_session()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{X_API_BASE}/{path.lstrip('/')}"
        response = self.session.get(url, params=params, headers={"Authorization": f"Bearer {self.bearer_token}"}, timeout=HTTP_TIMEOUT)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                wait = min(float(retry_after), 30.0) if retry_after else 3.0
            except ValueError:
                wait = 3.0
            time.sleep(wait)
            response = self.session.get(url, params=params, headers={"Authorization": f"Bearer {self.bearer_token}"}, timeout=HTTP_TIMEOUT)
        if not response.ok:
            body = response.text[:1000] if response.text else ""
            raise OsintApiError(f"X API request failed: HTTP {response.status_code}", status_code=response.status_code, url=response.url, body=body)
        try:
            return response.json()
        except Exception as exc:
            raise OsintApiError(f"X API response was not valid JSON: {exc}", status_code=response.status_code, url=response.url, body=response.text[:1000]) from exc

    def iter_search(self, query: str, *, endpoint: str = "tweets/search/recent", max_results: int = 100, params: dict[str, Any] | None = None, delay: float = 0.0) -> Iterator[dict[str, Any]]:
        returned = 0
        next_token = ""
        base_params = dict(params or {})
        while returned < max_results:
            request_params = dict(base_params)
            request_params["query"] = query
            request_params["max_results"] = max(10, min(100, max_results - returned))
            if next_token:
                request_params["next_token"] = next_token
            data = self._get(endpoint, request_params)
            tweets = data.get("data") or []
            users = {str(user.get("id")): user for user in ((data.get("includes") or {}).get("users") or []) if isinstance(user, dict)}
            media = {str(item.get("media_key")): item for item in ((data.get("includes") or {}).get("media") or []) if isinstance(item, dict)}
            for tweet in tweets:
                if not isinstance(tweet, dict):
                    continue
                returned += 1
                tweet["_includes_users"] = users
                tweet["_includes_media"] = media
                tweet["_source_query"] = query
                tweet["_raw_response_meta"] = data.get("meta") or {}
                yield tweet
                if returned >= max_results:
                    break
            next_token = (data.get("meta") or {}).get("next_token") or ""
            if not next_token:
                break
            if delay:
                time.sleep(delay)

    def lookup_users(self, usernames: list[str]) -> list[dict[str, Any]]:
        if not usernames:
            return []
        data = self._get(
            "users/by",
            {
                "usernames": ",".join(username.lstrip("@") for username in usernames),
                "user.fields": "id,name,username,created_at,description,public_metrics,verified,verified_type",
            },
        )
        return [item for item in data.get("data") or [] if isinstance(item, dict)]

    def iter_user_timeline(self, user_id: str, *, max_results: int = 100, params: dict[str, Any] | None = None, delay: float = 0.0) -> Iterator[dict[str, Any]]:
        returned = 0
        pagination_token = ""
        base_params = dict(params or {})
        while returned < max_results:
            request_params = dict(base_params)
            request_params["max_results"] = max(5, min(100, max_results - returned))
            if pagination_token:
                request_params["pagination_token"] = pagination_token
            data = self._get(f"users/{user_id}/tweets", request_params)
            users = {str(user.get("id")): user for user in ((data.get("includes") or {}).get("users") or []) if isinstance(user, dict)}
            for tweet in data.get("data") or []:
                if not isinstance(tweet, dict):
                    continue
                returned += 1
                tweet["_includes_users"] = users
                tweet["_source_query"] = f"user_id:{user_id}"
                yield tweet
                if returned >= max_results:
                    break
            pagination_token = (data.get("meta") or {}).get("next_token") or ""
            if not pagination_token:
                break
            if delay:
                time.sleep(delay)


def canonical_tweet_url(username: str, tweet_id: str) -> str:
    return f"https://x.com/{username}/status/{tweet_id}" if username and tweet_id else ""
