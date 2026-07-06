from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import urlparse

from common import (
    DEFAULT_DELAY_SECONDS,
    HTTP_TIMEOUT,
    OsintApiError,
    classify_text,
    get_form_bool,
    h,
    make_session,
    parse_int,
    parse_terms,
)

ENV_TOKEN = "X_BEARER_TOKEN"
X_API_BASE = os.getenv("X_API_BASE", "https://api.x.com/2").rstrip("/")

HEADERS = [
    "platform",
    "source_query",
    "flag_categories",
    "matched_terms",
    "tweet_id",
    "created_at",
    "author_username",
    "author_id",
    "author_name",
    "tweet_url",
    "text",
    "is_reply",
    "is_repost",
    "is_quote",
    "conversation_id",
    "like_count",
    "retweet_count",
    "reply_count",
    "quote_count",
    "impression_count",
    "language",
    "referenced_tweets",
    "captured_at_utc",
    "source_api",
    "notes",
]

META = {
    "key": "x_recent_search",
    "name": "X - Recent public post search",
    "description": (
        "Search recent public X posts through the official X API v2 recent search endpoint. "
        "Supports keyword, account, reply, repost, and conversation filters when your X developer access allows them."
    ),
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    token_present = bool(os.getenv(ENV_TOKEN))
    token_note = (
        f"Environment variable {ENV_TOKEN} is set; leave token blank here."
        if token_present
        else f"Set {ENV_TOKEN} in your .env/shell or paste a bearer token for this one request."
    )
    raw_queries = h(form.get("raw_queries", ""))
    terms = h(form.get("terms", ""))
    authors = h(form.get("authors", ""))
    conversation_id = h(form.get("conversation_id", ""))
    start_time = h(form.get("start_time", ""))
    end_time = h(form.get("end_time", ""))
    language = h(form.get("language", ""))
    max_results = h(form.get("max_results_per_query", "100"))
    delay = h(form.get("delay", str(DEFAULT_DELAY_SECONDS)))
    include_replies = "checked" if get_form_bool(form, "include_replies", True) else ""
    include_reposts = "checked" if get_form_bool(form, "include_reposts", False) else ""
    include_candidate = "checked" if get_form_bool(form, "include_candidate", True) else ""
    include_lobbying = "checked" if get_form_bool(form, "include_lobbying", True) else ""
    only_flagged = "checked" if get_form_bool(form, "only_flagged", False) else ""
    include_status = "checked" if get_form_bool(form, "include_status_rows", True) else ""

    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;">
        <label>X API bearer token</label>
        <input type="password" name="bearer_token" value="" autocomplete="off" placeholder="Optional if {ENV_TOKEN} is set">
        <div class="subtle">{h(token_note)} This module uses the official API and does not log into X or bypass platform controls.</div>
      </div>

      <div class="row" style="grid-column: 1 / -1;">
        <label>Raw X search queries, optional</label>
        <textarea name="raw_queries" placeholder='"school board" OR "PAC name"\nfrom:example_org has:links'>{raw_queries}</textarea>
        <div class="subtle">Advanced queries run as entered, then the filters below are appended where applicable.</div>
      </div>

      <div class="row" style="grid-column: 1 / -1;">
        <label>Search terms, if not using raw queries</label>
        <textarea name="terms" placeholder="Candidate Name\n#CampaignSlogan\nHB 1234">{terms}</textarea>
      </div>

      <div class="row" style="grid-column: 1 / -1;">
        <label>Optional X usernames</label>
        <textarea name="authors" placeholder="example_org\n@another_org">{authors}</textarea>
        <div class="subtle">Limits generated term searches to posts from these accounts. Raw queries can include their own from: operators.</div>
      </div>

      <div class="row">
        <label>Conversation ID</label>
        <input type="text" name="conversation_id" value="{conversation_id}" placeholder="Optional tweet/conversation id">
        <div class="subtle">Useful for collecting replies in a specific thread when supported by your access tier.</div>
      </div>

      <div class="row">
        <label>Language</label>
        <input type="text" name="language" maxlength="8" value="{language}" placeholder="Optional, e.g. en">
      </div>

      <div class="row">
        <label>Start time</label>
        <input type="text" name="start_time" value="{start_time}" placeholder="YYYY-MM-DDTHH:MM:SSZ">
      </div>

      <div class="row">
        <label>End time</label>
        <input type="text" name="end_time" value="{end_time}" placeholder="YYYY-MM-DDTHH:MM:SSZ">
      </div>

      <div class="row">
        <label>Max results per query</label>
        <input type="number" name="max_results_per_query" min="10" max="5000" value="{max_results}">
      </div>

      <div class="row">
        <label>Delay between API calls, seconds</label>
        <input type="number" step="0.05" min="0" max="10" name="delay" value="{delay}">
      </div>

      <div class="row">
        <label><input type="checkbox" name="include_replies" {include_replies}> Include replies</label>
        <label><input type="checkbox" name="include_reposts" {include_reposts}> Include reposts/retweets</label>
      </div>

      <div class="row">
        <label><input type="checkbox" name="include_candidate" {include_candidate}> Candidate-intervention review patterns</label>
        <label><input type="checkbox" name="include_lobbying" {include_lobbying}> Lobbying review patterns</label>
      </div>

      <div class="row">
        <label><input type="checkbox" name="only_flagged" {only_flagged}> Only return flagged or term-matched posts</label>
        <label><input type="checkbox" name="include_status_rows" {include_status}> Include status/error rows</label>
      </div>
    </div>
    """


def _settings(form: dict[str, Any]) -> dict[str, Any]:
    raw_queries = [line.strip() for line in str(form.get("raw_queries") or "").splitlines() if line.strip()]
    terms = parse_terms(form.get("terms", ""))
    authors = _parse_authors(form.get("authors", ""))
    if not raw_queries and not terms and not authors and not str(form.get("conversation_id") or "").strip():
        raise ValueError("Enter raw queries, search terms, usernames, or a conversation ID.")
    try:
        delay = max(0.0, min(float(form.get("delay") or DEFAULT_DELAY_SECONDS), 10.0))
    except Exception:
        delay = DEFAULT_DELAY_SECONDS
    return {
        "bearer_token": str(form.get("bearer_token") or os.getenv(ENV_TOKEN) or "").strip(),
        "raw_queries": raw_queries,
        "terms": terms,
        "authors": authors,
        "conversation_id": str(form.get("conversation_id") or "").strip(),
        "language": str(form.get("language") or "").strip(),
        "start_time": str(form.get("start_time") or "").strip(),
        "end_time": str(form.get("end_time") or "").strip(),
        "max_results_per_query": parse_int(form.get("max_results_per_query", 100), 100, 10, 5000),
        "include_replies": get_form_bool(form, "include_replies", True),
        "include_reposts": get_form_bool(form, "include_reposts", False),
        "include_candidate": get_form_bool(form, "include_candidate", True),
        "include_lobbying": get_form_bool(form, "include_lobbying", True),
        "only_flagged": get_form_bool(form, "only_flagged", False),
        "include_status_rows": get_form_bool(form, "include_status_rows", True),
        "delay": delay,
    }


def _parse_authors(raw: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[,;\n\r\t ]+", str(raw or "")):
        token = token.strip()
        if not token:
            continue
        if token.startswith("http://") or token.startswith("https://"):
            parsed = urlparse(token)
            parts = [part for part in parsed.path.split("/") if part]
            token = parts[0] if parts else token
        token = token.lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9_]{1,15}", token) and token.casefold() not in seen:
            out.append(token)
            seen.add(token.casefold())
    return out


def _author_filter(authors: list[str]) -> str:
    if not authors:
        return ""
    parts = [f"from:{author}" for author in authors if author]
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def _build_queries(settings: dict[str, Any]) -> list[str]:
    base_queries = settings["raw_queries"] or settings["terms"] or [""]
    author_filter = _author_filter(settings["authors"])
    queries: list[str] = []
    seen: set[str] = set()
    for base in base_queries:
        pieces = [str(base).strip()]
        if author_filter and not settings["raw_queries"]:
            pieces.append(author_filter)
        if settings["conversation_id"]:
            pieces.append(f"conversation_id:{settings['conversation_id']}")
        if settings["language"]:
            pieces.append(f"lang:{settings['language']}")
        if not settings["include_replies"]:
            pieces.append("-is:reply")
        if not settings["include_reposts"]:
            pieces.append("-is:retweet")
        query = " ".join(piece for piece in pieces if piece).strip()
        if query and query not in seen:
            queries.append(query)
            seen.add(query)
    return queries


def _status_row(source_query: str, note: str) -> list[Any]:
    row = [""] * len(HEADERS)
    row[0] = "X"
    row[1] = source_query
    row[-1] = note
    return row


def _x_get(session: Any, bearer_token: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"{X_API_BASE}/tweets/search/recent"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    response = session.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            wait = min(float(retry_after), 30.0) if retry_after else 3.0
        except ValueError:
            wait = 3.0
        time.sleep(wait)
        response = session.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
    if not response.ok:
        body = response.text[:1000] if response.text else ""
        raise OsintApiError(
            f"X recent search request failed: HTTP {response.status_code}",
            status_code=response.status_code,
            url=response.url,
            body=body,
        )
    try:
        return response.json()
    except Exception as exc:
        raise OsintApiError(
            f"X recent search response was not valid JSON: {exc}",
            status_code=response.status_code,
            url=response.url,
            body=response.text[:1000],
        ) from exc


def _user_map(includes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    users = includes.get("users") if isinstance(includes, dict) else []
    if not isinstance(users, list):
        return {}
    return {str(user.get("id")): user for user in users if isinstance(user, dict) and user.get("id")}


def _metrics(tweet: dict[str, Any]) -> dict[str, Any]:
    metrics = tweet.get("public_metrics")
    return metrics if isinstance(metrics, dict) else {}


def _referenced(tweet: dict[str, Any], ref_type: str) -> bool:
    refs = tweet.get("referenced_tweets")
    if not isinstance(refs, list):
        return False
    return any(isinstance(ref, dict) and ref.get("type") == ref_type for ref in refs)


def _tweet_to_row(tweet: dict[str, Any], users: dict[str, dict[str, Any]], settings: dict[str, Any], source_query: str) -> list[Any]:
    text = str(tweet.get("text") or "")
    categories, matched = classify_text(
        text,
        custom_terms=settings["terms"],
        include_candidate=settings["include_candidate"],
        include_lobbying=settings["include_lobbying"],
    )
    author_id = str(tweet.get("author_id") or "")
    user = users.get(author_id, {})
    username = user.get("username") or ""
    tweet_id = str(tweet.get("id") or "")
    metrics = _metrics(tweet)
    refs = tweet.get("referenced_tweets") or []
    return [
        "X",
        source_query,
        categories,
        matched,
        tweet_id,
        tweet.get("created_at") or "",
        username,
        author_id,
        user.get("name") or "",
        f"https://x.com/{username}/status/{tweet_id}" if username and tweet_id else "",
        text,
        "yes" if _referenced(tweet, "replied_to") else "no",
        "yes" if _referenced(tweet, "retweeted") else "no",
        "yes" if _referenced(tweet, "quoted") else "no",
        tweet.get("conversation_id") or "",
        metrics.get("like_count", ""),
        metrics.get("retweet_count", ""),
        metrics.get("reply_count", ""),
        metrics.get("quote_count", ""),
        metrics.get("impression_count", ""),
        tweet.get("lang") or "",
        "; ".join(f"{ref.get('type')}:{ref.get('id')}" for ref in refs if isinstance(ref, dict)),
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "GET /2/tweets/search/recent",
        "",
    ]


def iter_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    settings = _settings(form)
    if not settings["bearer_token"]:
        raise ValueError(f"Set {ENV_TOKEN} or paste an X API bearer token.")

    queries = _build_queries(settings)
    if not queries:
        raise ValueError("No valid X search queries were generated.")

    session = make_session()
    seen_ids: set[str] = set()
    base_params: dict[str, Any] = {
        "tweet.fields": "author_id,created_at,conversation_id,public_metrics,referenced_tweets,lang,entities,source",
        "expansions": "author_id",
        "user.fields": "username,name,verified,public_metrics",
    }
    if settings["start_time"]:
        base_params["start_time"] = settings["start_time"]
    if settings["end_time"]:
        base_params["end_time"] = settings["end_time"]

    for source_query in queries:
        returned = 0
        yielded = 0
        next_token = None
        while returned < settings["max_results_per_query"]:
            params = dict(base_params)
            params["query"] = source_query
            params["max_results"] = max(10, min(100, settings["max_results_per_query"] - returned))
            if next_token:
                params["next_token"] = next_token
            try:
                data = _x_get(session, settings["bearer_token"], params)
            except OsintApiError as exc:
                if settings["include_status_rows"]:
                    yield _status_row(source_query, f"API error: {exc}; status={exc.status_code}; body={exc.body or ''}")
                    break
                raise

            tweets = data.get("data") or []
            if not tweets:
                break
            users = _user_map(data.get("includes") or {})
            for tweet in tweets:
                returned += 1
                if not isinstance(tweet, dict):
                    continue
                tweet_id = str(tweet.get("id") or "")
                if tweet_id and tweet_id in seen_ids:
                    continue
                if tweet_id:
                    seen_ids.add(tweet_id)
                row = _tweet_to_row(tweet, users, settings, source_query)
                is_flagged = bool(row[2] or row[3])
                if settings["only_flagged"] and not is_flagged:
                    continue
                yielded += 1
                yield row
                if returned >= settings["max_results_per_query"]:
                    break

            meta = data.get("meta") or {}
            next_token = meta.get("next_token")
            if not next_token:
                break
            if settings["delay"]:
                time.sleep(settings["delay"])

        if settings["include_status_rows"] and yielded == 0:
            yield _status_row(source_query, f"No rows yielded. API returned {returned} posts before filtering/deduplication.")
        if settings["delay"]:
            time.sleep(settings["delay"])


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return HEADERS, list(iter_rows(form))


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from iter_rows(form)
