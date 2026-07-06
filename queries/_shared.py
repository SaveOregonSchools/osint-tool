from __future__ import annotations

import os
from typing import Any, Callable, Iterable, Iterator

from common import DEFAULT_DELAY_SECONDS, get_form_bool, h, parse_int, parse_terms
from osint_common import CORE_HEADERS, compact_json, core_row, enforce_source_access, flag_text, materialize_rows


def checked(form: dict[str, Any], key: str, default: bool = False) -> str:
    return "checked" if get_form_bool(form, key, default) else ""


def parse_delay(form: dict[str, Any]) -> float:
    try:
        return max(0.0, min(float(form.get("delay") or DEFAULT_DELAY_SECONDS), 10.0))
    except Exception:
        return DEFAULT_DELAY_SECONDS


def parse_lines(raw: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in str(raw or "").replace(",", "\n").replace(";", "\n").splitlines():
        token = token.strip()
        if token and token.casefold() not in seen:
            out.append(token)
            seen.add(token.casefold())
    return out


def env_password_field(env_name: str, field_name: str = "access_token", label: str = "API token", note: str = "") -> str:
    present = bool(os.getenv(env_name))
    status = f"{env_name} is set; leave blank here." if present else f"Set {env_name} in .env/shell or paste a value for this request."
    return f"""
    <div class="row" style="grid-column: 1 / -1;">
      <label>{h(label)}</label>
      <input type="password" name="{h(field_name)}" value="" autocomplete="off" placeholder="Optional if {h(env_name)} is set">
      <div class="subtle">{h(status)} {h(note)}</div>
    </div>
    """


def flag_controls(form: dict[str, Any], *, candidate_default: bool = True, lobbying_default: bool = True) -> str:
    return f"""
    <div class="row">
      <label><input type="checkbox" name="include_candidate" {checked(form, "include_candidate", candidate_default)}> Candidate-intervention review terms</label>
      <label><input type="checkbox" name="include_lobbying" {checked(form, "include_lobbying", lobbying_default)}> Lobbying review terms</label>
      <label><input type="checkbox" name="only_flagged" {checked(form, "only_flagged", False)}> Only return flagged/term-matched rows</label>
    </div>
    """


def keyword_textarea(form: dict[str, Any], name: str = "custom_terms", label: str = "Custom flag terms") -> str:
    return f"""
    <div class="row" style="grid-column: 1 / -1;">
      <label>{h(label)}</label>
      <textarea name="{h(name)}" placeholder="Candidate Name&#10;HB 1234&#10;ballot measure">{h(form.get(name, ""))}</textarea>
    </div>
    """


def delay_field(form: dict[str, Any]) -> str:
    return f"""
    <div class="row">
      <label>Delay between API calls, seconds</label>
      <input type="number" step="0.05" min="0" max="10" name="delay" value="{h(form.get("delay", str(DEFAULT_DELAY_SECONDS)))}">
    </div>
    """


def max_field(form: dict[str, Any], name: str, label: str, default: int, max_value: int = 10000) -> str:
    return f"""
    <div class="row">
      <label>{h(label)}</label>
      <input type="number" name="{h(name)}" min="1" max="{max_value}" value="{h(form.get(name, str(default)))}">
    </div>
    """


def date_range_fields(form: dict[str, Any], min_name: str = "date_min", max_name: str = "date_max") -> str:
    return f"""
    <div class="row">
      <label>Date min</label>
      <input type="date" name="{h(min_name)}" value="{h(form.get(min_name, ""))}">
    </div>
    <div class="row">
      <label>Date max</label>
      <input type="date" name="{h(max_name)}" value="{h(form.get(max_name, ""))}">
    </div>
    """


def include_settings(form: dict[str, Any]) -> dict[str, Any]:
    return {
        "include_candidate": get_form_bool(form, "include_candidate", True),
        "include_lobbying": get_form_bool(form, "include_lobbying", True),
        "only_flagged": get_form_bool(form, "only_flagged", False),
        "custom_terms": parse_terms(form.get("custom_terms", "")),
    }


def maybe_flag(text: str, settings: dict[str, Any]) -> tuple[str, str]:
    return flag_text(
        text,
        custom_terms=settings.get("custom_terms") or [],
        include_candidate=bool(settings.get("include_candidate", True)),
        include_lobbying=bool(settings.get("include_lobbying", True)),
    )


def keep_row(row: dict[str, Any], settings: dict[str, Any]) -> bool:
    if not settings.get("only_flagged"):
        return True
    return bool(row.get("flag_categories") or row.get("matched_terms"))


def status_row(meta: dict[str, Any], target: str, note: str, *, source_platform: str | None = None) -> dict[str, Any]:
    return core_row(
        source_platform=source_platform or meta.get("name", "OSINT"),
        source_api="status",
        source_type=meta.get("source_type", "official_api"),
        target_input=target,
        notes=note,
        raw_json={"status": note, "target": target},
    )


def run_core(
    meta: dict[str, Any],
    provider: str,
    form: dict[str, Any],
    row_iter_factory: Callable[[], Iterable[dict[str, Any]]],
    headers: list[str] | None = None,
) -> tuple[list[str], list[list[Any]]]:
    enforce_source_access(meta, form)
    export_headers = headers or meta.get("headers") or CORE_HEADERS
    rows = materialize_rows(provider=provider, plugin_key=meta["key"], params=form, row_iter=row_iter_factory(), headers=export_headers)
    return export_headers, rows


def export_core(
    meta: dict[str, Any],
    provider: str,
    form: dict[str, Any],
    row_iter_factory: Callable[[], Iterable[dict[str, Any]]],
    headers: list[str] | None = None,
) -> Iterator[list[Any]]:
    _, rows = run_core(meta, provider, form, row_iter_factory, headers=headers)
    yield from rows


def metric_summary(*values: Any) -> str:
    return compact_json({key: value for key, value in values if value not in (None, "")})
