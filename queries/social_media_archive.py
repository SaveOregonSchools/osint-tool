from __future__ import annotations

import html
import uuid
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from common import h, parse_int
from job_queue import JobExecutionError, enqueue_job, register_job_handler
from providers.browsertrix_archive import (
    DEFAULT_IMAGE,
    ArchiveBatch,
    ArchiveResult,
    CrawlSettings,
    build_archive_plan,
    execute_archive_plan,
    parse_date,
    slugify,
    validate_image_name,
)


META = {
    "key": "social_media_archive",
    "name": "Social Media Archive — Facebook, Instagram & X",
    "description": (
        "Create high-fidelity WACZ archives with Browsertrix using an authenticated browser profile. "
        "Facebook and Instagram URLs are isolated into separate collections; X account/search captures "
        "can be split into non-overlapping yearly collections."
    ),
    "source_type": "manual_entry",
    "coverage": "Browser-assisted WACZ capture of user-supplied Facebook, Instagram, and X targets.",
    "limitations": [
        "Requires Docker Desktop, a Browsertrix image, and a separately created authenticated browser profile.",
        "Does not bypass login, CAPTCHA, 2FA, access controls, rate limits, or platform restrictions.",
        "Year filtering applies to X search URLs. Facebook and Instagram do not expose equivalent feed date filters.",
        "Site interfaces and Browsertrix behaviors change; every WACZ should be replayed and quality-checked.",
        "Browser profiles and WACZ files may contain session tokens or personalized information and must be stored securely.",
    ],
}

HEADERS = [
    "batch_id",
    "platform",
    "label",
    "target_url",
    "query_text",
    "period_start",
    "period_end",
    "collection",
    "status",
    "wacz_path",
    "wacz_bytes",
    "sha256",
    "log_path",
    "error",
    "started_at",
    "completed_at",
]

RUN_BUTTON_LABEL = "Preview or queue archive batches"
HIDE_PREVIEW_LIMIT = True
HIDE_CSV_EXPORT = True
DISABLE_ROW_LIMIT = True

BASE_DIR = Path(__file__).resolve().parents[1]
MODULE_DATA_DIR = BASE_DIR / "data" / "social_media_archive"
PROFILES_DIR = MODULE_DATA_DIR / "profiles"


def _lines(value: Any) -> list[str]:
    return [line.strip() for line in str(value or "").replace("\r", "\n").split("\n") if line.strip()]


def _select(name: str, current: str, options: list[tuple[str, str]]) -> str:
    rendered = []
    for value, label in options:
        selected = " selected" if value == current else ""
        rendered.append(f'<option value="{h(value)}"{selected}>{h(label)}</option>')
    return f'<select name="{h(name)}">' + "".join(rendered) + "</select>"


def _default_dates(form: dict[str, Any]) -> tuple[str, str]:
    today = date.today()
    return (
        str(form.get("x_start") or date(today.year, 1, 1).isoformat()),
        str(form.get("x_end") or today.isoformat()),
    )


def _profile_filename(form: dict[str, Any]) -> str:
    filename = str(form.get("profile_filename") or "social-auth.tar.gz").strip()
    if not filename or Path(filename).name != filename or not filename.casefold().endswith(".tar.gz"):
        raise ValueError("Browser profile filename must be a simple .tar.gz filename stored in the module profiles folder.")
    return filename


def _profile_path(form: dict[str, Any]) -> Path:
    return PROFILES_DIR / _profile_filename(form)


def _image(form: dict[str, Any]) -> str:
    return validate_image_name(str(form.get("browsertrix_image") or DEFAULT_IMAGE))


def render_fields(form: dict[str, Any]) -> str:
    x_start, x_end = _default_dates(form)
    operation = str(form.get("operation") or "plan")
    batch_mode = str(form.get("batch_mode") or "year")
    image = str(form.get("browsertrix_image") or DEFAULT_IMAGE)
    profile_filename = str(form.get("profile_filename") or "social-auth.tar.gz")
    try:
        command_image = validate_image_name(image)
    except ValueError:
        command_image = DEFAULT_IMAGE
    try:
        command_profile_filename = _profile_filename(form)
    except ValueError:
        command_profile_filename = "social-auth.tar.gz"
    profiles_dir = str(PROFILES_DIR.resolve())
    profile_command = (
        f'New-Item -ItemType Directory -Force -Path "{profiles_dir}"\n'
        f'docker run --rm -it -p 6080:6080 -p 9223:9223 -v "{profiles_dir}:/crawls/profiles" '
        f'{command_image} create-login-profile --url "https://www.facebook.com/" '
        f'--filename "/crawls/profiles/{command_profile_filename}"'
    )
    return f"""
    <div class="notice">
      <b>Before the first archive:</b> create a dedicated Browsertrix profile, sign into only the accounts
      authorized for this research, and save it as <code>data/social_media_archive/profiles/{h(profile_filename)}</code>.
      Do not enter passwords into this module.
      <details style="margin-top:8px;">
        <summary>Show local profile-creation instructions</summary>
        <ol>
          <li>Install and start Docker Desktop.</li>
          <li>Run the command below in PowerShell.</li>
          <li>Open <a href="http://localhost:9223/" target="_blank" rel="noreferrer">http://localhost:9223/</a>, sign into Facebook, Instagram, and X in the embedded browser, then click <b>Create Profile</b>.</li>
        </ol>
        <pre style="white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8fa;padding:10px;border-radius:6px;">{h(profile_command)}</pre>
      </details>
    </div>

    <div class="grid">
      <div class="row">
        <label>Operation</label>
        {_select('operation', operation, [('plan', 'Plan and validate only'), ('archive', 'Queue WACZ archives')])}
        <div class="subtle">Planning is the safe default and does not start Docker or visit any platform.</div>
      </div>
      <div class="row">
        <label>Authenticated profile filename</label>
        <input type="text" name="profile_filename" value="{h(profile_filename)}">
        <div class="subtle">Read only from <code>data/social_media_archive/profiles/</code>.</div>
      </div>

      <div class="row" style="grid-column:1/-1;">
        <label>Facebook profile, Page, group, photo, reel, or post URLs — one per line</label>
        <textarea name="facebook_urls" placeholder="https://www.facebook.com/example">{h(form.get('facebook_urls', ''))}</textarea>
      </div>
      <div class="row" style="grid-column:1/-1;">
        <label>Instagram profile, post, reel, story, or highlight URLs — one per line</label>
        <textarea name="instagram_urls" placeholder="https://www.instagram.com/example/">{h(form.get('instagram_urls', ''))}</textarea>
      </div>
      <div class="row" style="grid-column:1/-1;">
        <label>X accounts — one handle or profile URL per line</label>
        <textarea name="x_accounts" placeholder="example_account&#10;https://x.com/another_account">{h(form.get('x_accounts', ''))}</textarea>
        <div class="subtle">Each becomes a <code>from:account</code> search. The module appends the date range for each batch.</div>
      </div>
      <div class="row" style="grid-column:1/-1;">
        <label>Additional terms for every X account search (optional)</label>
        <input type="text" name="x_additional_terms" value="{h(form.get('x_additional_terms', ''))}" placeholder='"school funding" -filter:replies'>
      </div>
      <div class="row" style="grid-column:1/-1;">
        <label>Additional X search expressions — one per line (optional)</label>
        <textarea name="x_search_expressions" placeholder="from:example_account has:media&#10;from:another_account \"school board\"">{h(form.get('x_search_expressions', ''))}</textarea>
        <div class="subtle">Expressions may use <code>from:</code> and other X operators. Leave out <code>since:</code> and <code>until:</code>; the date fields below manage non-overlapping batches.</div>
      </div>

      <div class="row">
        <label>X start date</label>
        <input type="date" name="x_start" value="{h(x_start)}">
      </div>
      <div class="row">
        <label>X end date (inclusive)</label>
        <input type="date" name="x_end" value="{h(x_end)}">
      </div>
      <div class="row">
        <label>X archive batching</label>
        {_select('batch_mode', batch_mode, [('year', 'Separate WACZ collection per calendar year'), ('single', 'One WACZ collection for the full date range')])}
      </div>
      <div class="row">
        <label>Browsertrix container image</label>
        <input type="text" name="browsertrix_image" value="{h(image)}">
        <div class="subtle">For repeatable evidence collection, replace <code>latest</code> with a tested release tag.</div>
      </div>

      <div class="row"><label>Behavior time per page (seconds)</label><input type="number" name="behavior_timeout_seconds" min="30" max="7200" value="{h(form.get('behavior_timeout_seconds', '600'))}"></div>
      <div class="row"><label>Maximum time per WACZ batch (seconds)</label><input type="number" name="time_limit_seconds" min="60" max="86400" value="{h(form.get('time_limit_seconds', '1800'))}"></div>
      <div class="row"><label>Maximum pages per WACZ batch</label><input type="number" name="page_limit" min="1" max="5000" value="{h(form.get('page_limit', '250'))}"></div>
      <div class="row"><label>Maximum WACZ batch size (MB)</label><input type="number" name="size_limit_mb" min="100" max="10240" value="{h(form.get('size_limit_mb', '2048'))}"></div>
      <div class="row"><label>Save final screenshot</label>{_select('save_final_screenshot', str(form.get('save_final_screenshot') or 'yes'), [('yes', 'Yes'), ('no', 'No')])}</div>
      <div class="row"><label>Extract final page text</label>{_select('extract_final_text', str(form.get('extract_final_text') or 'yes'), [('yes', 'Yes'), ('no', 'No')])}</div>
      <div class="row"><label>Fail when Browsertrix detects missing login/content</label>{_select('fail_on_content_check', str(form.get('fail_on_content_check') or 'yes'), [('yes', 'Yes'), ('no', 'No')])}</div>
    </div>

    <div class="limitations">
      <b>Batching boundary</b>
      <ul>
        <li>Every Facebook and Instagram URL is always written to its own collection.</li>
        <li>X account/search captures are split by calendar year by default using <code>since:</code> and <code>until:</code>.</li>
        <li>Page, time, and size limits apply independently to every collection.</li>
      </ul>
    </div>
    """


def _settings(form: dict[str, Any]) -> CrawlSettings:
    return CrawlSettings(
        image=_image(form),
        behavior_timeout_seconds=parse_int(form.get("behavior_timeout_seconds", 600), 600, 30, 7200),
        time_limit_seconds=parse_int(form.get("time_limit_seconds", 1800), 1800, 60, 86400),
        page_limit=parse_int(form.get("page_limit", 250), 250, 1, 5000),
        size_limit_mb=parse_int(form.get("size_limit_mb", 2048), 2048, 100, 10240),
        save_final_screenshot=str(form.get("save_final_screenshot") or "yes") == "yes",
        extract_final_text=str(form.get("extract_final_text") or "yes") == "yes",
        fail_on_content_check=str(form.get("fail_on_content_check") or "yes") == "yes",
    )


def build_plan(form: dict[str, Any]) -> list[ArchiveBatch]:
    x_accounts = _lines(form.get("x_accounts"))
    x_expressions = _lines(form.get("x_search_expressions"))
    if x_accounts or x_expressions:
        x_start = parse_date(form.get("x_start"), "X start date")
        x_end = parse_date(form.get("x_end"), "X end date")
    else:
        today = date.today()
        x_start, x_end = date(today.year, 1, 1), today
    return build_archive_plan(
        facebook_urls=_lines(form.get("facebook_urls")),
        instagram_urls=_lines(form.get("instagram_urls")),
        x_accounts=x_accounts,
        x_search_expressions=x_expressions,
        x_additional_terms=str(form.get("x_additional_terms") or ""),
        x_start=x_start,
        x_end=x_end,
        batch_mode=str(form.get("batch_mode") or "year"),
    )


def _result_row(result: ArchiveResult) -> list[Any]:
    return [getattr(result, key) for key in HEADERS]


def _planned_result(batch: ArchiveBatch, profile_exists: bool) -> ArchiveResult:
    return ArchiveResult(
        batch_id=batch.batch_id,
        platform=batch.platform,
        label=batch.label,
        target_url=batch.seed_url,
        query_text=batch.query_text,
        period_start=batch.period_start,
        period_end=batch.period_end,
        collection=batch.collection,
        status="planned" if profile_exists else "planned — profile missing",
    )


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    batches = build_plan(form)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    profile_path = _profile_path(form)
    operation = str(form.get("operation") or "plan")
    if operation == "plan":
        results = [_planned_result(batch, profile_path.is_file()) for batch in batches]
        return HEADERS, [_result_row(item) for item in results]
    if operation != "archive":
        raise ValueError("Unknown archive operation.")

    if not profile_path.is_file():
        raise RuntimeError(f"Authenticated browser profile not found: {profile_path}")
    settings = _settings(form)
    submission_id = "archive-" + uuid.uuid4().hex[:12]
    results: list[ArchiveResult] = []
    for batch in batches:
        run_id = slugify(f"{submission_id}-{batch.batch_id}-{batch.collection}", 120)
        job_id = enqueue_job(
            module_key=META["key"],
            handler_key="social_media_archive.batch",
            label=f"{batch.platform.title()}: {batch.label}",
            group_id=submission_id,
            payload={
                "batch": asdict(batch),
                "settings": asdict(settings),
                "profile_filename": _profile_filename(form),
                "run_id": run_id,
            },
        )
        results.append(
            ArchiveResult(
                batch_id=batch.batch_id,
                platform=batch.platform,
                label=batch.label,
                target_url=batch.seed_url,
                query_text=batch.query_text,
                period_start=batch.period_start,
                period_end=batch.period_end,
                collection=batch.collection,
                status=f"queued — job {job_id[:8]}",
            )
        )
    return HEADERS, [_result_row(item) for item in results]


def run_queued_job(
    payload: dict[str, Any], update_progress: Any
) -> dict[str, Any]:
    batch = ArchiveBatch(**dict(payload.get("batch") or {}))
    settings = CrawlSettings(**dict(payload.get("settings") or {}))
    profile_filename = str(payload.get("profile_filename") or "")
    if Path(profile_filename).name != profile_filename or not profile_filename.casefold().endswith(".tar.gz"):
        raise JobExecutionError("Queued job contains an invalid browser profile filename.")
    run_id = slugify(str(payload.get("run_id") or "archive-job"), 120)
    update_progress(0, 1, "Running Browsertrix capture")
    run_dir, results = execute_archive_plan(
        batches=[batch],
        settings=settings,
        module_data_dir=MODULE_DATA_DIR,
        profile_path=PROFILES_DIR / profile_filename,
        run_id=run_id,
    )
    result = asdict(results[0])
    result["output_dir"] = str(run_dir.resolve())
    update_progress(1, 1, result.get("status") or "Finished")
    if str(result.get("status") or "").startswith("failed"):
        raise JobExecutionError(str(result.get("error") or "Browsertrix archive failed."), result=result)
    return result


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterable[list[Any]]:
    """Expose the non-mutating plan if called outside the UI; never start a crawl from an export."""
    profile_exists = _profile_path(form).is_file()
    for batch in build_plan(form):
        yield _result_row(_planned_result(batch, profile_exists))


def _archive_submit_fields(form: dict[str, Any]) -> str:
    fields: list[str] = []
    values = {str(key): value for key, value in form.items() if key != "_files" and key != "operation"}
    values["qkey"] = META["key"]
    values["operation"] = "archive"
    for name, value in values.items():
        if isinstance(value, (str, int, float)):
            fields.append(
                f'<input type="hidden" name="{html.escape(name, quote=True)}" '
                f'value="{html.escape(str(value), quote=True)}">'
            )
    return "".join(fields)


def render_results(form: dict[str, Any], headers: list[str], rows: list[list[Any]]) -> str:
    status_index = headers.index("status")
    completed = sum(1 for row in rows if str(row[status_index]).startswith("completed"))
    failed = sum(1 for row in rows if row[status_index] == "failed")
    queued = sum(1 for row in rows if str(row[status_index]).startswith("queued"))
    planned = len(rows) - completed - failed - queued
    summary = f"{queued} queued, {completed} completed, {failed} failed, {planned} planned"
    profile_path = _profile_path(form)
    operation = str(form.get("operation") or "plan")
    error_index = headers.index("error")
    can_run_archive = bool(rows) and operation == "plan" and all(
        str(row[status_index]) == "planned" and not str(row[error_index]) for row in rows
    )
    archive_action = ""
    if can_run_archive:
        archive_action = f"""
        <form method="post" action="/run" onsubmit="return showRunningMessage(event, this);" style="margin:12px 0 14px;">
          {_archive_submit_fields(form)}
          <button class="success" type="submit">Run Archiving</button>
        </form>
        """

    table_rows = []
    indexes = {name: headers.index(name) for name in headers}
    for row in rows:
        target = str(row[indexes["target_url"]])
        wacz = str(row[indexes["wacz_path"]])
        error = str(row[indexes["error"]])
        table_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row[indexes['platform']]))}</td>"
            f"<td>{html.escape(str(row[indexes['label']]))}</td>"
            f"<td>{html.escape(str(row[indexes['period_start']]))}</td>"
            f"<td>{html.escape(str(row[indexes['period_end']]))}</td>"
            f"<td><a href=\"{html.escape(target, quote=True)}\" target=\"_blank\" rel=\"noreferrer\">Open target</a></td>"
            f"<td>{html.escape(str(row[indexes['status']]))}</td>"
            f"<td class=\"wrap\">{html.escape(wacz)}</td>"
            f"<td class=\"wrap\">{html.escape(error)}</td>"
            "</tr>"
        )
    auto_scroll = ""
    if operation == "plan":
        auto_scroll = """
        <script>
          window.addEventListener("load", function () {
            document.getElementById("archive-plan-results").scrollIntoView({ behavior: "smooth", block: "start" });
          });
        </script>
        """
    return f"""
    <div id="archive-plan-results">
    <div class="notice">
      <b>Archive result:</b> {h(summary)}.<br>
      <span class="subtle">You may leave this page while queued jobs run. Follow progress and open output folders on the <a href="/jobs"><b>Jobs page</b></a>. Authenticated profile: <code>{h(profile_path)}</code>.</span>
    </div>
    {archive_action}
    <div class="results"><table>
      <thead><tr><th>Platform</th><th>Batch</th><th>From</th><th>Through</th><th>Target</th><th>Status</th><th>WACZ</th><th>Error</th></tr></thead>
      <tbody>{''.join(table_rows)}</tbody>
    </table></div>
    </div>
    {auto_scroll}
    """


register_job_handler("social_media_archive.batch", run_queued_job)
