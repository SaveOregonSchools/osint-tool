from __future__ import annotations

import html
import os
import re
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

import requests


APPVIEW_BASE = os.getenv("BSKY_APPVIEW_BASE", "https://public.api.bsky.app").rstrip("/")
HTTP_TIMEOUT = float(os.getenv("OSINT_HTTP_TIMEOUT", "30"))
USER_AGENT = os.getenv("OSINT_USER_AGENT", "social-osint-console/0.1 (+local research tool)")
DEFAULT_DELAY_SECONDS = float(os.getenv("OSINT_REQUEST_DELAY", "0.2"))


class OsintApiError(RuntimeError):
    """Raised when a remote OSINT/API query fails."""

    def __init__(self, message: str, *, status_code: int | None = None, url: str | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.body = body


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def xrpc_get(endpoint: str, params: dict[str, Any] | None = None, *, session: requests.Session | None = None, base_url: str = APPVIEW_BASE) -> dict[str, Any]:
    """GET an AT Protocol XRPC endpoint and return JSON.

    The MVP uses unauthenticated public AppView reads. If Bluesky or another
    provider changes an endpoint to require authentication, the UI will surface
    the HTTP status/body instead of silently failing.
    """
    if session is None:
        session = make_session()
    endpoint = endpoint.lstrip("/")
    url = f"{base_url}/xrpc/{endpoint}"

    resp = session.get(url, params=params or {}, timeout=HTTP_TIMEOUT)
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        try:
            wait = min(float(retry_after), 30.0) if retry_after else 3.0
        except ValueError:
            wait = 3.0
        time.sleep(wait)
        resp = session.get(url, params=params or {}, timeout=HTTP_TIMEOUT)

    if not resp.ok:
        body = resp.text[:1000] if resp.text else ""
        raise OsintApiError(f"XRPC request failed: HTTP {resp.status_code} for {endpoint}", status_code=resp.status_code, url=resp.url, body=body)
    try:
        return resp.json()
    except Exception as exc:
        raise OsintApiError(f"XRPC response was not valid JSON for {endpoint}: {exc}", status_code=resp.status_code, url=resp.url, body=resp.text[:1000]) from exc


def h(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def get_form_bool(form: dict[str, Any], key: str, default: bool = False) -> bool:
    value = form.get(key)
    if value is None:
        return default
    return str(value).lower() in {"1", "true", "yes", "on", "checked"}


def parse_int(value: Any, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        n = int(str(value).strip())
    except Exception:
        n = default
    if min_value is not None:
        n = max(min_value, n)
    if max_value is not None:
        n = min(max_value, n)
    return n


def clean_actor(raw: str) -> str:
    """Accept a handle, DID, @handle, or bsky.app profile URL and normalize it."""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("@"):
        s = s[1:]
    if s.startswith("at://"):
        # at://did:plc:abc/app.bsky.feed.post/rkey -> did:plc:abc
        parts = s.split("/")
        return parts[2] if len(parts) > 2 else s
    if s.startswith("http://") or s.startswith("https://"):
        parsed = urlparse(s)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.netloc.endswith("bsky.app") and parts and parts[0] == "profile" and len(parts) >= 2:
            return parts[1].lstrip("@")
    return s


def parse_actor_inputs(text: str) -> list[str]:
    tokens = re.split(r"[,;\n\r\t ]+", text or "")
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        actor = clean_actor(token)
        if actor and actor not in seen:
            out.append(actor)
            seen.add(actor)
    return out


def parse_terms(text: str) -> list[str]:
    """Parse user-supplied terms/phrases. One per line is safest; commas/semicolons also work."""
    raw = text or ""
    parts: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        # Preserve phrases on each line, but allow simple comma/semicolon lists.
        for piece in re.split(r"[;,]", line):
            piece = piece.strip().strip('"').strip("'")
            if piece:
                parts.append(piece)
    out: list[str] = []
    seen: set[str] = set()
    for term in parts:
        key = term.casefold()
        if key not in seen:
            out.append(term)
            seen.add(key)
    return out


CANDIDATE_QUERY_TERMS = [
    "vote for",
    "vote against",
    "elect",
    "re-elect",
    "reelect",
    "defeat",
    "endorse",
    "endorsement",
    "candidate",
    "campaign",
    "for Congress",
    "for Senate",
    "for Mayor",
    "donate",
]

LOBBYING_QUERY_TERMS = [
    "call your senator",
    "call your representative",
    "contact your legislator",
    "tell Congress",
    "urge Congress",
    "lawmakers",
    "HB",
    "SB",
    "AB",
    "bill",
    "ballot measure",
    "Proposition",
    "Prop",
    "referendum",
    "initiative",
    "vote yes",
    "vote no",
]

CANDIDATE_PATTERNS: list[tuple[str, str]] = [
    ("vote for", r"\bvote\s+for\b"),
    ("vote against", r"\bvote\s+against\b"),
    ("elect / elected", r"\belect(?:ed|ing)?\b"),
    ("re-elect / reelect", r"\bre[-\s]?elect(?:ed|ing)?\b"),
    ("defeat", r"\bdefeat(?:ed|ing)?\b"),
    ("endorse / endorsement", r"\bendors(?:e|ed|ing|ement|ements)\b"),
    ("candidate", r"\bcandidate\b"),
    ("campaign", r"\bcampaign\b"),
    ("donate to campaign/candidate", r"\bdonat(?:e|ed|ing|ion)\b.{0,80}\b(campaign|candidate|for\s+(?:congress|senate|mayor|governor|president))\b"),
    ("for Congress/Senate/Mayor/Governor", r"\bfor\s+(congress|senate|mayor|governor|president|school\s+board|city\s+council)\b"),
]

LOBBYING_PATTERNS: list[tuple[str, str]] = [
    ("call your senator/representative", r"\bcall\s+your\s+(senator|representative|rep|assembly(?:member)?|council(?:member)?)\b"),
    ("contact lawmakers", r"\b(contact|email|write|phone|call|tell|urge)\b.{0,80}\b(congress|senate|house|legislator|lawmakers?|council|committee|representatives?|senators?)\b"),
    ("support/oppose bill", r"\b(support|oppose|pass|kill|stop|amend|vote\s+yes|vote\s+no)\b.{0,80}\b(HB|SB|AB|HR|S\.?\s?\d+|H\.?R\.?\s?\d+|bill|act|ordinance|resolution)\b"),
    ("bill number", r"\b(HB|SB|AB|HR)\s*[-#]?\s*\d{1,6}\b"),
    ("ballot measure / proposition", r"\b(ballot\s+measure|proposition|prop\.?\s*\d+|referendum|initiative)\b"),
    ("vote yes/no on measure", r"\bvote\s+(yes|no)\s+on\b"),
    ("sign petition", r"\bsign\s+(the\s+)?petition\b"),
]


def selected_query_terms(include_candidate: bool, include_lobbying: bool, custom_terms: Iterable[str], max_terms: int = 30) -> list[str]:
    terms: list[str] = []
    if include_candidate:
        terms.extend(CANDIDATE_QUERY_TERMS)
    if include_lobbying:
        terms.extend(LOBBYING_QUERY_TERMS)
    terms.extend(custom_terms)

    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        term = str(term).strip()
        key = term.casefold()
        if term and key not in seen:
            out.append(term)
            seen.add(key)
        if len(out) >= max_terms:
            break
    return out


def classify_text(text: str, *, custom_terms: Iterable[str] = (), include_candidate: bool = True, include_lobbying: bool = True) -> tuple[str, str]:
    """Return ('categories', 'matched_terms') for a post-like text block."""
    text = text or ""
    categories: list[str] = []
    matches: list[str] = []

    def add_category(cat: str):
        if cat not in categories:
            categories.append(cat)

    def add_match(label: str):
        if label not in matches:
            matches.append(label)

    if include_candidate:
        for label, pattern in CANDIDATE_PATTERNS:
            if re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
                add_category("candidate_intervention_review")
                add_match(label)

    if include_lobbying:
        for label, pattern in LOBBYING_PATTERNS:
            if re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
                add_category("lobbying_review")
                add_match(label)

    lowered = text.casefold()
    for term in custom_terms:
        term = str(term).strip()
        if not term:
            continue
        if term.casefold() in lowered:
            add_category("custom_term")
            add_match(term)

    return "; ".join(categories), "; ".join(matches)


def parse_iso_datetime(value: Any) -> datetime | None:
    s = str(value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # AT Protocol timestamps are normally ISO 8601. If a server returns a
        # slightly odd string, let the row through rather than crashing.
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_date_start(value: Any) -> datetime | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return None
    return datetime.combine(d, dt_time.min, tzinfo=timezone.utc)


def parse_date_until_exclusive(value: Any) -> datetime | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return None
    return datetime.combine(d + timedelta(days=1), dt_time.min, tzinfo=timezone.utc)


def within_date_range(created_at: Any, since_dt: datetime | None, until_exclusive_dt: datetime | None) -> bool:
    dt = parse_iso_datetime(created_at)
    if dt is None:
        return True
    if since_dt and dt < since_dt:
        return False
    if until_exclusive_dt and dt >= until_exclusive_dt:
        return False
    return True


def at_uri_rkey(uri: str) -> str:
    return (uri or "").rstrip("/").split("/")[-1]


def bsky_post_url(post_view: dict[str, Any]) -> str:
    uri = post_view.get("uri") or ""
    rkey = at_uri_rkey(uri)
    author = post_view.get("author") or {}
    actor = author.get("handle") or author.get("did") or ""
    if actor and rkey:
        return f"https://bsky.app/profile/{actor}/post/{rkey}"
    return ""


def summarize_embed(embed: Any) -> str:
    if not isinstance(embed, dict):
        return ""
    typ = str(embed.get("$type") or embed.get("py_type") or "")

    if "images" in embed and isinstance(embed.get("images"), list):
        alts = []
        for img in embed.get("images", [])[:4]:
            if isinstance(img, dict) and img.get("alt"):
                alts.append(str(img.get("alt"))[:160])
        suffix = f" | alt: {' || '.join(alts)}" if alts else ""
        return f"images:{len(embed.get('images', []))}{suffix}"

    if "external" in embed and isinstance(embed.get("external"), dict):
        ext = embed.get("external") or {}
        title = ext.get("title") or ""
        uri = ext.get("uri") or ""
        desc = ext.get("description") or ""
        bits = ["external"]
        if title:
            bits.append(f"title={title[:180]}")
        if uri:
            bits.append(f"url={uri[:220]}")
        if desc:
            bits.append(f"desc={desc[:180]}")
        return " | ".join(bits)

    if "video" in typ:
        return "video"

    if "record" in embed and isinstance(embed.get("record"), dict):
        record = embed.get("record") or {}
        uri = record.get("uri") or ""
        author = record.get("author") or {}
        handle = author.get("handle") or author.get("did") or ""
        text = ""
        value = record.get("value")
        if isinstance(value, dict):
            text = value.get("text") or ""
        return f"quoted_record author={handle} uri={uri} text={text[:220]}".strip()

    return typ or "embed"


def post_view_to_row(
    post_view: dict[str, Any],
    *,
    actor_input: str,
    query: str,
    custom_terms: Iterable[str],
    include_candidate: bool,
    include_lobbying: bool,
    source_api: str,
    reason: dict[str, Any] | None = None,
    notes: str = "",
) -> list[Any]:
    author = post_view.get("author") or {}
    record = post_view.get("record") or {}
    text = record.get("text") or ""
    embed_summary = summarize_embed(post_view.get("embed") or record.get("embed"))
    combined_text = f"{text}\n{embed_summary}".strip()
    categories, matched = classify_text(combined_text, custom_terms=custom_terms, include_candidate=include_candidate, include_lobbying=include_lobbying)

    reply = record.get("reply") or {}
    root_uri = ""
    parent_uri = ""
    if isinstance(reply, dict):
        root_uri = ((reply.get("root") or {}).get("uri") or "") if isinstance(reply.get("root"), dict) else ""
        parent_uri = ((reply.get("parent") or {}).get("uri") or "") if isinstance(reply.get("parent"), dict) else ""

    reason_type = ""
    if isinstance(reason, dict):
        reason_type = reason.get("$type") or reason.get("py_type") or ""

    labels = post_view.get("labels") or []
    label_values = []
    if isinstance(labels, list):
        for item in labels:
            if isinstance(item, dict) and item.get("val"):
                label_values.append(str(item.get("val")))

    return [
        "Bluesky",
        actor_input,
        query,
        categories,
        matched,
        record.get("createdAt") or post_view.get("indexedAt") or "",
        post_view.get("indexedAt") or "",
        author.get("handle") or "",
        author.get("did") or "",
        author.get("displayName") or "",
        bsky_post_url(post_view),
        text,
        "yes" if reply else "no",
        "yes" if reason_type.endswith("#reasonRepost") or "reasonRepost" in reason_type else "no",
        post_view.get("likeCount", 0),
        post_view.get("repostCount", 0),
        post_view.get("replyCount", 0),
        post_view.get("quoteCount", 0),
        embed_summary,
        post_view.get("uri") or "",
        post_view.get("cid") or "",
        root_uri,
        parent_uri,
        "; ".join(label_values),
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_api,
        notes,
    ]


POST_HEADERS = [
    "platform",
    "actor_input",
    "query",
    "flag_categories",
    "matched_terms",
    "created_at",
    "indexed_at",
    "author_handle",
    "author_did",
    "display_name",
    "post_url",
    "text",
    "is_reply",
    "is_repost",
    "like_count",
    "repost_count",
    "reply_count",
    "quote_count",
    "embed_summary",
    "uri",
    "cid",
    "root_uri",
    "parent_uri",
    "labels",
    "captured_at_utc",
    "source_api",
    "notes",
]
