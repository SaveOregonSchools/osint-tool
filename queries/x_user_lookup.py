from __future__ import annotations

from typing import Any, Iterator

from common import h
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.x_api import XApiClient
from queries._shared import export_core, parse_lines, run_core
from queries._x_common import x_token_field


HEADERS = CORE_HEADERS + ["user_id", "username", "verified", "verified_type", "public_metrics"]

META = {
    "key": "x_user_lookup",
    "name": "X - User Lookup",
    "description": "Resolve X usernames to user IDs and public profile metadata through the official X API.",
    "source_type": "official_api",
    "limitations": ["Requires X developer access and an app bearer token.", "Returned fields depend on X API access and policy."],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    return f"""
    <div class="grid">
      {x_token_field()}
      <div class="row" style="grid-column: 1 / -1;">
        <label>Usernames</label>
        <textarea name="usernames" placeholder="org_handle&#10;candidate_handle">{h(form.get("usernames", ""))}</textarea>
      </div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    usernames = [name.lstrip("@") for name in parse_lines(form.get("usernames"))]
    if not usernames:
        raise ValueError("Enter at least one X username.")
    client = XApiClient(str(form.get("bearer_token") or "").strip() or None)
    for user in client.lookup_users(usernames):
        username = user.get("username") or ""
        yield core_row(
            source_platform="X",
            source_api="GET /2/users/by",
            source_type=META["source_type"],
            target_input=",".join(usernames),
            query_text=username,
            created_at=user.get("created_at") or "",
            author_handle=username,
            author_id=user.get("id") or "",
            author_display_name=user.get("name") or "",
            canonical_url=f"https://x.com/{username}" if username else "",
            text=user.get("description") or "",
            metrics_json=user.get("public_metrics") or {},
            raw_json=user,
            platform_item_id=user.get("id") or username,
            user_id=user.get("id") or "",
            username=username,
            verified=user.get("verified", ""),
            verified_type=user.get("verified_type") or "",
            public_metrics=compact_json(user.get("public_metrics")),
        )


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "X API", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "X API", form, lambda: iter_row_dicts(form), HEADERS)
