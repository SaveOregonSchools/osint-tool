from __future__ import annotations

from typing import Any, Iterator

from common import get_form_bool, h
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.spiderfoot import SpiderFootClient
from queries._shared import export_core, parse_lines, run_core, status_row


HEADERS = CORE_HEADERS + ["target", "scan_id", "event_type", "module", "data"]

META = {
    "key": "spiderfoot_entity_enrichment",
    "name": "SpiderFoot - Entity Enrichment",
    "description": "Use a configured local SpiderFoot server for discovery/enrichment around domains, emails, IPs, names, and usernames.",
    "source_type": "third_party_api",
    "limitations": [
        "SpiderFoot results are discovery/enrichment only and should not be mixed directly into evidence of lobbying/campaign intervention.",
        "Requires a running SpiderFoot server configured by the investigator.",
    ],
    "headers": HEADERS,
}


def render_fields(form: dict[str, Any]) -> str:
    start_checked = "checked" if get_form_bool(form, "start_scan", False) else ""
    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;"><label>Targets</label><textarea name="targets" placeholder="example.org&#10;person@example.org">{h(form.get("targets", ""))}</textarea></div>
      <div class="row"><label>Existing scan ID</label><input type="text" name="scan_id" value="{h(form.get("scan_id", ""))}"></div>
      <div class="row"><label>Modules</label><input type="text" name="modules" value="{h(form.get("modules", ""))}" placeholder="Optional SpiderFoot module list"></div>
      <div class="row"><label><input type="checkbox" name="start_scan" {start_checked}> Start a new scan if no scan ID is supplied</label></div>
    </div>
    """


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    client = SpiderFootClient()
    scan_id = str(form.get("scan_id") or "").strip()
    targets = parse_lines(form.get("targets"))
    if scan_id:
        data = client.scan_results(scan_id)
        rows = data.get("data") if isinstance(data, dict) else data
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            event_type = item.get("type") or item.get("event_type") or ""
            content = item.get("data") or item.get("content") or ""
            yield core_row(
                source_platform="SpiderFoot",
                source_api="SpiderFoot scaneventresults",
                source_type=META["source_type"],
                target_input=scan_id,
                query_text=scan_id,
                text=str(content),
                media_summary="enrichment event",
                raw_json=item,
                platform_item_id=str(item.get("id") or item.get("hash") or ""),
                target=item.get("target") or "",
                scan_id=scan_id,
                event_type=event_type,
                module=item.get("module") or "",
                data=str(content),
            )
        return
    if not targets:
        raise ValueError("Enter targets or an existing SpiderFoot scan ID.")
    if not get_form_bool(form, "start_scan", False):
        for target in targets:
            row = status_row(META, target, "SpiderFoot target queued in form only. Check 'Start a new scan' to call SpiderFoot.", source_platform="SpiderFoot")
            row["target"] = target
            row["scan_id"] = ""
            row["event_type"] = "not_started"
            row["module"] = ""
            row["data"] = ""
            yield row
        return
    for target in targets:
        started = client.start_scan(target, str(form.get("modules") or ""))
        row = core_row(
            source_platform="SpiderFoot",
            source_api="SpiderFoot startscan",
            source_type=META["source_type"],
            target_input=target,
            query_text=target,
            text=compact_json(started),
            media_summary="scan started",
            raw_json=started,
            platform_item_id=str(started.get("scan_id") or started.get("id") or target),
            target=target,
            scan_id=started.get("scan_id") or started.get("id") or "",
            event_type="scan_started",
            module=str(form.get("modules") or ""),
            data=compact_json(started),
        )
        yield row


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    return run_core(META, "SpiderFoot", form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    yield from export_core(META, "SpiderFoot", form, lambda: iter_row_dicts(form), HEADERS)
