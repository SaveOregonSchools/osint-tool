from __future__ import annotations

import html
import json
import uuid
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from flask import has_request_context, url_for

from common import get_form_bool, h, parse_int
from job_queue import JobExecutionError, JobRetry, enqueue_job, register_job_handler, set_job_throttle
from providers.browsertrix_archive import (
    DEFAULT_IMAGE,
    PROFILE_REVIEW_BEHAVIORS,
    ArchiveBatch,
    ArchiveResult,
    CrawlSettings,
    build_interactive_profile_command,
    build_archive_plan,
    execute_archive_plan,
    normalize_x_handle,
    parse_date,
    slugify,
    validate_image_name,
    validate_x_image,
)
from providers.wacz_content import extract_wacz_content


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
        "Requires Docker Engine or Docker Desktop, a Browsertrix image, and a separately created authenticated browser profile.",
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
    "validation_status",
    "search_successes",
    "search_rate_limits",
    "search_other_statuses",
    "rate_limit_remaining",
    "rate_limit_reset",
    "partial",
    "pruned_files",
    "pruned_bytes",
    "compaction_status",
]

RUN_BUTTON_LABEL = "Preview or queue archive batches"
HIDE_PREVIEW_LIMIT = True
HIDE_CSV_EXPORT = True
DISABLE_ROW_LIMIT = True

BASE_DIR = Path(__file__).resolve().parents[1]
MODULE_DATA_DIR = BASE_DIR / "data" / "social_media_archive"
PROFILES_DIR = MODULE_DATA_DIR / "profiles"
X_RATE_LIMIT_MAX_RETRIES = 3
X_RATE_LIMIT_FALLBACK_SECONDS = 15 * 60
X_RATE_LIMIT_GRACE_SECONDS = 15
X_RATE_LIMIT_MAX_HEADER_WAIT_SECONDS = 60 * 60
X_RATE_LIMIT_MAX_HEADER_AGE_SECONDS = 24 * 60 * 60
BROWSERTRIX_RESOURCE_KEY = "social-media-archive:browsertrix"


def _route_url(endpoint: str, fallback: str) -> str:
    """Build a deployment-prefix-aware route URL when rendering in Flask."""
    if has_request_context():
        return url_for(endpoint)
    return fallback


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
        x_crawl_settings = CrawlSettings()
        x_crawl_settings_summary = (
            f"Current values: <code>OSINT_X_RATE_LIMIT_MAX_RETRIES="
            f"{x_crawl_settings.x_rate_limit_max_retries}</code>, "
            f"<code>OSINT_X_RATE_LIMIT_INTERRUPT_COUNT="
            f"{x_crawl_settings.x_rate_limit_interrupt_count}</code>, and "
            f"<code>OSINT_X_POST_LOAD_DELAY_SECONDS="
            f"{x_crawl_settings.x_post_load_delay_seconds}</code>."
        )
    except ValueError as exc:
        x_crawl_settings_summary = f"Invalid X crawler setting: {h(exc)}"
    try:
        command_image = validate_image_name(image)
    except ValueError:
        command_image = DEFAULT_IMAGE
    try:
        command_profile_filename = _profile_filename(form)
    except ValueError:
        command_profile_filename = "social-auth.tar.gz"
    profile_command = build_interactive_profile_command(
        profiles_dir=PROFILES_DIR,
        image=command_image,
        output_filename=command_profile_filename,
    )
    ssh_tunnel = "ssh -N -L 9223:127.0.0.1:9223 -L 6080:127.0.0.1:6080 <user>@<server>"
    return f"""
    <div class="notice">
      <b>Before the first archive:</b> create a dedicated Browsertrix profile, sign into only the accounts
      authorized for this research, and save it as <code>data/social_media_archive/profiles/{h(profile_filename)}</code>.
      Do not enter passwords into this module.
      <details style="margin-top:8px;">
        <summary>Show local profile-creation instructions</summary>
        <ol>
          <li>Install and start Docker Engine or Docker Desktop on the host where this app stores its data.</li>
          <li>Run the command below in that host's shell. Both Browsertrix ports bind to loopback only.</li>
          <li>For a remote Linux host, first tunnel the ports from your workstation with <code>{h(ssh_tunnel)}</code>.</li>
          <li>Open <a href="http://127.0.0.1:9223/" target="_blank" rel="noreferrer">http://127.0.0.1:9223/</a>, sign into Facebook, Instagram, and X in the embedded browser, verify the X account, then click <b>Create Profile</b>.</li>
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
        <div class="subtle">Stored under <code>data/social_media_archive/profiles/</code>. X preflight loads it read-only and atomically promotes a refreshed copy only after authentication is verified.</div>
      </div>
      <div class="row">
        <label>Expected logged-in X account (optional)</label>
        <input type="text" name="expected_x_session_handle" value="{h(form.get('expected_x_session_handle', ''))}" placeholder="your_crawler_account">
        <div class="subtle">If set, X collection fails before capture when Browsertrix detects a different account or cannot verify the handle.</div>
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
      <div class="row" style="grid-column:1/-1;">
        <label>X crawler tuning</label>
        <div class="subtle">{x_crawl_settings_summary} The retry and interrupt controls apply to Browsertrix 1.14 or newer; the post-load delay also applies to the 1.13.2 compatibility path. Set them in <code>.env</code> and restart the application after changing them. Their effective values are saved with each queued job.</div>
      </div>

      <div class="row"><label>Behavior time per page (seconds)</label><input type="number" name="behavior_timeout_seconds" min="30" max="7200" value="{h(form.get('behavior_timeout_seconds', '600'))}"></div>
      <div class="row"><label>Maximum time per WACZ batch (seconds)</label><input type="number" name="time_limit_seconds" min="60" max="86400" value="{h(form.get('time_limit_seconds', '1800'))}"></div>
      <div class="row"><label>Maximum pages per WACZ batch</label><input type="number" name="page_limit" min="1" max="5000" value="{h(form.get('page_limit', '250'))}"></div>
      <div class="row"><label>Maximum WACZ batch size (MB)</label><input type="number" name="size_limit_mb" min="100" max="10240" value="{h(form.get('size_limit_mb', '2048'))}"></div>
      <div class="row"><label>Save final screenshot</label>{_select('save_final_screenshot', str(form.get('save_final_screenshot') or 'yes'), [('yes', 'Yes'), ('no', 'No')])}</div>
      <div class="row"><label>Extract final page text</label>{_select('extract_final_text', str(form.get('extract_final_text') or 'yes'), [('yes', 'Yes'), ('no', 'No')])}</div>
      <div class="row"><label>Fail when Browsertrix detects missing login/content</label>{_select('fail_on_content_check', str(form.get('fail_on_content_check') or 'yes'), [('yes', 'Yes'), ('no', 'No')])}</div>
      <div class="row"><label>Retain Browsertrix working files</label>{_select('retain_working_files', str(form.get('retain_working_files') or 'no'), [('no', 'No — keep JSON and WACZ only'), ('yes', 'Yes — keep raw crawler workspace')])}<div class="subtle">The WACZ already contains the WARC, page records, indexes, and crawler log. The copied browser profile and external duplicates are removed by default after validation.</div></div>
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
    raw_expected_handle = str(form.get("expected_x_session_handle") or "").strip()
    expected_handle = normalize_x_handle(raw_expected_handle) if raw_expected_handle else ""
    return CrawlSettings(
        image=_image(form),
        behavior_timeout_seconds=parse_int(form.get("behavior_timeout_seconds", 600), 600, 30, 7200),
        time_limit_seconds=parse_int(form.get("time_limit_seconds", 1800), 1800, 60, 86400),
        page_limit=parse_int(form.get("page_limit", 250), 250, 1, 5000),
        size_limit_mb=parse_int(form.get("size_limit_mb", 2048), 2048, 100, 10240),
        save_final_screenshot=str(form.get("save_final_screenshot") or "yes") == "yes",
        extract_final_text=str(form.get("extract_final_text") or "yes") == "yes",
        fail_on_content_check=str(form.get("fail_on_content_check") or "yes") == "yes",
        retain_working_files=str(form.get("retain_working_files") or "no") == "yes",
        expected_x_session_handle=expected_handle,
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


def _x_throttle_key(profile_filename: str) -> str:
    return f"social-media-archive:x:{profile_filename.casefold()}"


def _x_retry_at(reset_epoch: int | None) -> str:
    now = datetime.now(timezone.utc)
    now_epoch = int(now.timestamp())
    reset = int(reset_epoch or 0)
    if not now_epoch - X_RATE_LIMIT_MAX_HEADER_AGE_SECONDS <= reset <= now_epoch + X_RATE_LIMIT_MAX_HEADER_WAIT_SECONDS:
        reset = now_epoch + X_RATE_LIMIT_FALLBACK_SECONDS
    retry_epoch = max(reset + X_RATE_LIMIT_GRACE_SECONDS, now_epoch + X_RATE_LIMIT_GRACE_SECONDS)
    retry_at = datetime.fromtimestamp(retry_epoch, tz=timezone.utc)
    return retry_at.isoformat(timespec="seconds")


def _persist_attempt_manifest(run_dir: Path, result: dict[str, Any]) -> None:
    """Link queue retries in the existing manifest without creating another sidecar file."""
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_results = payload.get("results")
    if isinstance(manifest_results, list):
        for manifest_result in manifest_results:
            if isinstance(manifest_result, dict) and manifest_result.get("batch_id") == result.get("batch_id"):
                manifest_result.update(result)
                break
    payload["job_attempt"] = {
        "attempt_number": result.get("attempt_count"),
        "previous_attempts": result.get("previous_attempts") or [],
        "retry_at": result.get("retry_at") or "",
    }
    payload["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(manifest_path)


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
    if any(batch.platform == "x" for batch in batches):
        validate_x_image(settings.image)
    submission_id = "archive-" + uuid.uuid4().hex[:12]
    results: list[ArchiveResult] = []
    for batch in batches:
        run_id = slugify(f"{submission_id}-{batch.batch_id}-{batch.collection}", 120)
        job_id = enqueue_job(
            module_key=META["key"],
            handler_key="social_media_archive.batch",
            label=f"{batch.platform.title()}: {batch.label}",
            group_id=submission_id,
            throttle_key=_x_throttle_key(_profile_filename(form)) if batch.platform == "x" else "",
            resource_key=BROWSERTRIX_RESOURCE_KEY,
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
    retry_count = max(0, int(payload.get("rate_limit_retry_count") or 0))
    base_run_id = slugify(str(payload.get("run_id") or "archive-job"), 120)
    if retry_count == 0:
        run_id = base_run_id
    else:
        suffix = f"-attempt-{retry_count + 1}"
        run_id = base_run_id[: 120 - len(suffix)].rstrip("-") + suffix
    update_progress(
        0,
        2,
        "Verifying the saved X session, then running Browsertrix capture"
        if batch.platform == "x"
        else "Running Browsertrix capture",
    )
    run_dir, results = execute_archive_plan(
        batches=[batch],
        settings=settings,
        module_data_dir=MODULE_DATA_DIR,
        profile_path=PROFILES_DIR / profile_filename,
        run_id=run_id,
    )
    result = asdict(results[0])
    result["output_dir"] = str(run_dir.resolve())
    result["attempt_count"] = retry_count + 1
    result["previous_attempts"] = list(payload.get("previous_attempts") or [])
    if payload.get("automation_request"):
        result["automation_request"] = dict(payload["automation_request"])
    status = str(result.get("status") or "")
    if batch.platform == "x":
        throttle_key = _x_throttle_key(profile_filename)
        retry_at = _x_retry_at(result.get("rate_limit_reset"))
        if result.get("validation_status") == "rate_limited_partial":
            message = (
                f"X rate-limited this batch after {result.get('search_successes') or 0} successful timeline "
                f"response(s). It is partial and was not blindly rerun; split the date range before retrying."
            )
            set_job_throttle(throttle_key, retry_at, f"X rate limit active; next attempt after {retry_at}")
            existing_error = str(result.get("error") or "")
            result["error"] = message + (f" {existing_error}" if result.get("compaction_status") == "failed" else "")
            _persist_attempt_manifest(run_dir, result)
            raise JobExecutionError(message, result=result)
        if status == "rate limited":
            message = f"X rate limit active; retrying after {retry_at}"
            set_job_throttle(throttle_key, retry_at, message)
            if retry_count < X_RATE_LIMIT_MAX_RETRIES:
                next_payload = dict(payload)
                next_payload["rate_limit_retry_count"] = retry_count + 1
                next_payload["previous_attempts"] = result["previous_attempts"] + [
                    {
                        "run_id": run_id,
                        "status": status,
                        "wacz_path": result.get("wacz_path") or "",
                        "retry_at": retry_at,
                    }
                ]
                result["status"] = "rate limited — retry scheduled"
                result["retry_at"] = retry_at
                _persist_attempt_manifest(run_dir, result)
                raise JobRetry(
                    message,
                    retry_at=retry_at,
                    payload=next_payload,
                    result=result,
                    throttle_key=throttle_key,
                )
            message = f"X remained rate limited after {retry_count + 1} attempts; last reset gate was {retry_at}."
            result["error"] = message
            _persist_attempt_manifest(run_dir, result)
            raise JobExecutionError(message, result=result)
        if status.startswith("completed") and result.get("rate_limit_remaining") == 0:
            set_job_throttle(
                throttle_key,
                retry_at,
                f"X SearchTimeline quota reached zero; next X batch waits until {retry_at}",
            )
    if payload.get("automation_request") and not status.startswith("failed"):
        wacz_value = str(result.get("wacz_path") or "")
        wacz_path = Path(wacz_value)
        if not wacz_path.is_absolute():
            wacz_path = MODULE_DATA_DIR / wacz_path
        update_progress(1, 2, "Packaging extracted text and graphics")
        try:
            result.update(extract_wacz_content(wacz_path, run_dir / "content"))
        except Exception as exc:
            result["content_error"] = f"The WACZ completed, but content packaging failed: {exc}"
            _persist_attempt_manifest(run_dir, result)
            raise JobExecutionError(result["content_error"], result=result) from exc
    update_progress(2, 2, result.get("status") or "Finished")
    _persist_attempt_manifest(run_dir, result)
    if str(result.get("status") or "").startswith("failed"):
        raise JobExecutionError(str(result.get("error") or "Browsertrix archive failed."), result=result)
    return result


def enqueue_profile_review(request_data: dict[str, Any]) -> dict[str, Any]:
    """Validate and queue one automation-friendly profile review job."""
    platform = str(request_data.get("platform") or "").casefold().strip()
    if platform not in {"facebook", "instagram", "x"}:
        raise ValueError("platform must be one of: facebook, instagram, x.")
    profile = str(request_data.get("profile") or "").strip()
    if not profile:
        raise ValueError("profile is required.")

    lookback = request_data.get("lookback") or {}
    if not isinstance(lookback, dict):
        raise ValueError("lookback must be an object with value and unit fields.")
    raw_value = lookback.get("value")
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("lookback.value must be a whole number from 1 through 520.") from exc
    if not 1 <= value <= 520:
        raise ValueError("lookback.value must be a whole number from 1 through 520.")
    unit = str(lookback.get("unit") or "").casefold().strip()
    if unit in {"day", "days"}:
        days = value
        normalized_unit = "days"
    elif unit in {"week", "weeks"}:
        days = value * 7
        normalized_unit = "weeks"
    else:
        raise ValueError("lookback.unit must be days or weeks.")

    end_date = parse_date(request_data.get("end_date") or date.today().isoformat(), "end_date")
    start_date = end_date - timedelta(days=days - 1)
    profile_filename = str(request_data.get("profile_filename") or "social-auth.tar.gz").strip()
    profile_form = {"profile_filename": profile_filename}
    profile_path = _profile_path(profile_form)
    if not profile_path.is_file():
        raise ValueError(f"Authenticated browser profile not found: {profile_path}")

    if platform == "x":
        batches = build_archive_plan(
            facebook_urls=[],
            instagram_urls=[],
            x_accounts=[profile],
            x_search_expressions=[],
            x_additional_terms="-filter:replies",
            x_start=start_date,
            x_end=end_date,
            batch_mode="single",
        )
        date_filter = "enforced"
    else:
        batches = build_archive_plan(
            facebook_urls=[profile] if platform == "facebook" else [],
            instagram_urls=[profile] if platform == "instagram" else [],
            x_accounts=[],
            x_search_expressions=[],
            x_additional_terms="",
            x_start=start_date,
            x_end=end_date,
            batch_mode="single",
        )
        batches = [
            replace(batch, period_start=start_date.isoformat(), period_end=end_date.isoformat()) for batch in batches
        ]
        date_filter = "best_effort"

    options = request_data.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("options must be an object when supplied.")
    settings = CrawlSettings(
        image=validate_image_name(str(options.get("browsertrix_image") or DEFAULT_IMAGE)),
        behaviors=PROFILE_REVIEW_BEHAVIORS,
        behavior_timeout_seconds=parse_int(options.get("behavior_timeout_seconds", 600), 600, 30, 7200),
        time_limit_seconds=parse_int(options.get("time_limit_seconds", 1800), 1800, 60, 86400),
        page_limit=parse_int(options.get("page_limit", 250), 250, 1, 5000),
        size_limit_mb=parse_int(options.get("size_limit_mb", 2048), 2048, 100, 10240),
        save_final_screenshot=False,
        extract_final_text=True,
        fail_on_content_check=get_form_bool(options, "fail_on_content_check", True),
        expected_x_session_handle=(
            normalize_x_handle(str(options.get("expected_x_session_handle") or request_data.get("expected_x_session_handle")))
            if str(options.get("expected_x_session_handle") or request_data.get("expected_x_session_handle") or "").strip()
            else ""
        ),
    )
    if platform == "x":
        validate_x_image(settings.image)

    batch = batches[0]
    submission_id = "profile-review-" + uuid.uuid4().hex[:12]
    run_id = slugify(f"{submission_id}-{batch.collection}", 120)
    job_id = enqueue_job(
        module_key=META["key"],
        handler_key="social_media_archive.batch",
        label=f"{platform.title()} profile review: {batch.label}",
        group_id=submission_id,
        throttle_key=_x_throttle_key(profile_filename) if platform == "x" else "",
        resource_key=BROWSERTRIX_RESOURCE_KEY,
        payload={
            "batch": asdict(batch),
            "settings": asdict(settings),
            "profile_filename": profile_filename,
            "run_id": run_id,
            "automation_request": {
                "profile": profile,
                "lookback": {"value": value, "unit": normalized_unit},
                "requested_start": start_date.isoformat(),
                "requested_end": end_date.isoformat(),
                "date_filter": date_filter,
            },
        },
    )
    limitations = []
    if date_filter == "best_effort":
        limitations.append(
            "Facebook and Instagram profile feeds do not provide a reliable date-bounded URL; the crawler scrolls the visible feed and the requested window is advisory."
        )
    limitations.append(
        "Browser-derived text and graphics are best-effort and should be checked when complete; platform interface changes can affect collection."
    )
    return {
        "submission_id": submission_id,
        "job_id": job_id,
        "status": "queued",
        "platform": platform,
        "profile": profile,
        "requested_start": start_date.isoformat(),
        "requested_end": end_date.isoformat(),
        "date_filter": date_filter,
        "limitations": limitations,
    }


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
    failed = sum(1 for row in rows if str(row[status_index]).startswith("failed"))
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
        run_url = _route_url("run", "/run")
        archive_action = f"""
        <form method="post" action="{h(run_url)}" onsubmit="return showRunningMessage(event, this);" style="margin:12px 0 14px;">
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
    jobs_url = _route_url("jobs_page", "/jobs")
    return f"""
    <div id="archive-plan-results">
    <div class="notice">
      <b>Archive result:</b> {h(summary)}.<br>
      <span class="subtle">You may leave this page while queued jobs run. Follow progress and open output folders on the <a href="{h(jobs_url)}"><b>Jobs page</b></a>. Authenticated profile: <code>{h(profile_path)}</code>.</span>
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
