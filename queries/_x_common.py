from __future__ import annotations

from typing import Any

from common import h, parse_int
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.x_api import canonical_tweet_url
from queries._shared import env_password_field, flag_controls, include_settings, maybe_flag, parse_lines


TWEET_HEADERS = CORE_HEADERS + [
    "tweet_id",
    "conversation_id",
    "author_username",
    "lang",
    "public_metrics",
    "referenced_tweets",
    "in_reply_to_user_id",
    "possibly_sensitive",
    "attachments",
]


def x_token_field() -> str:
    return env_password_field("X_BEARER_TOKEN", "bearer_token", "X API bearer token", "Uses official X API v2 endpoints.")


def x_time_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="row">
      <label>Start time</label>
      <input type="text" name="start_time" value="{h(form.get("start_time", ""))}" placeholder="YYYY-MM-DDTHH:MM:SSZ">
    </div>
    <div class="row">
      <label>End time</label>
      <input type="text" name="end_time" value="{h(form.get("end_time", ""))}" placeholder="YYYY-MM-DDTHH:MM:SSZ">
    </div>
    """


def max_results_field(form: dict[str, Any], default: int = 100) -> str:
    return f"""
    <div class="row">
      <label>Max results</label>
      <input type="number" name="max_results" min="10" max="5000" value="{h(form.get("max_results", str(default)))}">
    </div>
    """


def base_tweet_params(form: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "tweet.fields": "author_id,created_at,conversation_id,public_metrics,referenced_tweets,lang,entities,attachments,in_reply_to_user_id,possibly_sensitive",
        "expansions": "author_id,attachments.media_keys,referenced_tweets.id",
        "user.fields": "id,name,username,created_at,description,public_metrics,verified,verified_type",
        "media.fields": "media_key,type,url,preview_image_url,alt_text,public_metrics",
    }
    if form.get("start_time"):
        params["start_time"] = str(form.get("start_time")).strip()
    if form.get("end_time"):
        params["end_time"] = str(form.get("end_time")).strip()
    return params


def _or_group(parts: list[str]) -> str:
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def quote_term(term: str) -> str:
    term = term.strip()
    if not term:
        return ""
    if " " in term and not (term.startswith('"') and term.endswith('"')):
        return f'"{term}"'
    return term


def build_recent_query(form: dict[str, Any]) -> str:
    terms = parse_lines(form.get("query_terms") or form.get("terms") or form.get("raw_queries"))
    from_handles = [handle.lstrip("@") for handle in parse_lines(form.get("from_handles") or form.get("authors"))]
    to_handles = [handle.lstrip("@") for handle in parse_lines(form.get("to_handles"))]
    mentions = [handle.lstrip("@") for handle in parse_lines(form.get("mentions"))]
    hashtags = [tag.lstrip("#") for tag in parse_lines(form.get("hashtags"))]
    url_terms = parse_lines(form.get("url_terms"))
    pieces = []
    pieces.append(_or_group([quote_term(term) for term in terms if quote_term(term)]))
    pieces.append(_or_group([f"from:{handle}" for handle in from_handles if handle]))
    pieces.append(_or_group([f"to:{handle}" for handle in to_handles if handle]))
    pieces.append(_or_group([f"@{handle}" for handle in mentions if handle]))
    pieces.append(_or_group([f"#{tag}" for tag in hashtags if tag]))
    pieces.append(_or_group([f"url:{quote_term(term)}" for term in url_terms if term]))
    if form.get("conversation_id"):
        pieces.append(f"conversation_id:{str(form.get('conversation_id')).strip()}")
    if form.get("exclude_retweets") in {"on", "true", "1", True}:
        pieces.append("-is:retweet")
    if form.get("exclude_replies") in {"on", "true", "1", True}:
        pieces.append("-is:reply")
    query = " ".join(piece for piece in pieces if piece).strip()
    if not query:
        raise ValueError("Enter query terms, handles, mentions, hashtags, URL terms, or a conversation ID.")
    return query


def tweet_row(tweet: dict[str, Any], *, source_api: str, source_type: str, target_input: str, query_text: str, settings: dict[str, Any]) -> dict[str, Any]:
    users = tweet.get("_includes_users") or {}
    author_id = str(tweet.get("author_id") or "")
    user = users.get(author_id) or {}
    username = user.get("username") or ""
    tweet_id = str(tweet.get("id") or "")
    text = str(tweet.get("text") or "")
    categories, matched = maybe_flag(text, settings)
    media = tweet.get("_includes_media") or {}
    refs = tweet.get("referenced_tweets") or []
    return core_row(
        source_platform="X",
        source_api=source_api,
        source_type=source_type,
        target_input=target_input,
        query_text=query_text,
        flag_categories=categories,
        matched_terms=matched,
        created_at=tweet.get("created_at") or "",
        author_handle=username,
        author_id=author_id,
        author_display_name=user.get("name") or "",
        canonical_url=canonical_tweet_url(username, tweet_id),
        text=text,
        media_summary=compact_json(media),
        metrics_json=tweet.get("public_metrics") or {},
        raw_json=tweet,
        platform_item_id=tweet_id,
        tweet_id=tweet_id,
        conversation_id=tweet.get("conversation_id") or "",
        author_username=username,
        lang=tweet.get("lang") or "",
        public_metrics=compact_json(tweet.get("public_metrics")),
        referenced_tweets=compact_json(refs),
        in_reply_to_user_id=tweet.get("in_reply_to_user_id") or "",
        possibly_sensitive=tweet.get("possibly_sensitive", ""),
        attachments=compact_json(tweet.get("attachments")),
    )


def x_settings(form: dict[str, Any]) -> dict[str, Any]:
    settings = include_settings(form)
    settings["max_results"] = parse_int(form.get("max_results", 100), 100, 10, 5000)
    settings["bearer_token"] = str(form.get("bearer_token") or "").strip() or None
    return settings


def x_flag_controls(form: dict[str, Any]) -> str:
    return flag_controls(form)
