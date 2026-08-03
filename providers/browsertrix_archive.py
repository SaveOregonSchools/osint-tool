from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse


DEFAULT_IMAGE = "webrecorder/browsertrix-crawler:1.14.1"
DEFAULT_BEHAVIORS = "autoscroll,autoplay,autofetch,siteSpecific"
PROFILE_REVIEW_BEHAVIORS = "autoscroll,autoplay,autofetch"
SUPPORTED_PLATFORMS = {"facebook", "instagram", "x"}


@dataclass(frozen=True)
class ArchiveBatch:
    batch_id: str
    platform: str
    label: str
    seed_url: str
    collection: str
    query_text: str = ""
    period_start: str = ""
    period_end: str = ""


@dataclass(frozen=True)
class CrawlSettings:
    image: str = DEFAULT_IMAGE
    behaviors: str = DEFAULT_BEHAVIORS
    behavior_timeout_seconds: int = 600
    time_limit_seconds: int = 1800
    page_limit: int = 250
    size_limit_mb: int = 2048
    save_final_screenshot: bool = True
    extract_final_text: bool = True
    fail_on_content_check: bool = True


@dataclass
class ArchiveResult:
    batch_id: str
    platform: str
    label: str
    target_url: str
    query_text: str
    period_start: str
    period_end: str
    collection: str
    status: str
    wacz_path: str = ""
    wacz_bytes: int = 0
    sha256: str = ""
    log_path: str = ""
    error: str = ""
    started_at: str = ""
    completed_at: str = ""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: Any, label: str) -> date:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid date in YYYY-MM-DD format.") from exc


def inclusive_date_periods(start: date, end: date, batch_mode: str) -> list[tuple[date, date]]:
    """Return [start, exclusive-end) periods for an inclusive user date range."""
    if end < start:
        raise ValueError("X end date must be on or after the start date.")
    final_exclusive = end + timedelta(days=1)
    if batch_mode == "single":
        return [(start, final_exclusive)]
    if batch_mode != "year":
        raise ValueError("Batch mode must be 'year' or 'single'.")

    periods: list[tuple[date, date]] = []
    cursor = start
    while cursor < final_exclusive:
        next_year = date(cursor.year + 1, 1, 1)
        period_end = min(next_year, final_exclusive)
        periods.append((cursor, period_end))
        cursor = period_end
    return periods


def period_label(start: date, end_exclusive: date) -> str:
    if start == date(start.year, 1, 1) and end_exclusive == date(start.year + 1, 1, 1):
        return str(start.year)
    return f"{start.isoformat()}_through_{(end_exclusive - timedelta(days=1)).isoformat()}"


def slugify(value: str, max_length: int = 70) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return (slug[:max_length].rstrip("-") or "archive")


def normalize_x_handle(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("X account names cannot be blank.")
    if "://" in text:
        parsed = urlparse(text)
        if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            raise ValueError(f"Not an X account URL: {text}")
        text = next((part for part in parsed.path.split("/") if part), "")
    handle = text.lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", handle):
        raise ValueError(f"Invalid X account name: {value}")
    return handle


def validate_social_url(value: str, platform: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"A {platform.title()} URL cannot be blank.")
    if not re.match(r"^https?://", text, flags=re.I):
        text = "https://" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").casefold()
    allowed = {
        "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com"},
        "instagram": {"instagram.com", "www.instagram.com"},
        "x": {"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
    }
    if platform not in allowed:
        raise ValueError(f"Unsupported platform: {platform}")
    if host not in allowed[platform]:
        raise ValueError(f"Expected a {platform.title()} URL, received: {value}")
    if parsed.scheme.casefold() != "https":
        raise ValueError("Archive targets must use HTTPS.")
    return parsed.geturl()


def build_x_search_query(base_query: str, start: date, end_exclusive: date) -> str:
    query = " ".join(str(base_query or "").split())
    if not query:
        raise ValueError("An X search expression cannot be blank.")
    if re.search(r"(?:^|\s)(?:since|until):", query, flags=re.I):
        raise ValueError(
            "Do not include since: or until: in X search expressions; use the module date fields so yearly batches do not overlap."
        )
    return f"{query} since:{start.isoformat()} until:{end_exclusive.isoformat()}"


def build_x_search_url(query: str) -> str:
    return "https://x.com/search?" + urlencode({"q": query, "src": "typed_query", "f": "live"})


def _unique_collection(base: str, seen: set[str]) -> str:
    candidate = slugify(base, 90)
    if candidate not in seen:
        seen.add(candidate)
        return candidate
    suffix = 2
    while f"{candidate}-{suffix}" in seen:
        suffix += 1
    result = f"{candidate}-{suffix}"
    seen.add(result)
    return result


def build_archive_plan(
    *,
    facebook_urls: Iterable[str],
    instagram_urls: Iterable[str],
    x_accounts: Iterable[str],
    x_search_expressions: Iterable[str],
    x_additional_terms: str,
    x_start: date,
    x_end: date,
    batch_mode: str,
) -> list[ArchiveBatch]:
    batches: list[ArchiveBatch] = []
    seen_collections: set[str] = set()

    def add_direct(platform: str, raw_url: str, index: int) -> None:
        url = validate_social_url(raw_url, platform)
        parsed = urlparse(url)
        path_label = "-".join(part for part in parsed.path.split("/") if part) or f"target-{index}"
        label = path_label[:80]
        collection = _unique_collection(f"{platform}-{label}", seen_collections)
        batches.append(
            ArchiveBatch(
                batch_id=f"batch-{len(batches) + 1:04d}",
                platform=platform,
                label=label,
                seed_url=url,
                collection=collection,
            )
        )

    for index, raw_url in enumerate(facebook_urls, start=1):
        add_direct("facebook", raw_url, index)
    for index, raw_url in enumerate(instagram_urls, start=1):
        add_direct("instagram", raw_url, index)

    expressions: list[tuple[str, str]] = []
    terms = " ".join(str(x_additional_terms or "").split())
    for raw_account in x_accounts:
        handle = normalize_x_handle(raw_account)
        expression = f"from:{handle}"
        if terms:
            expression += f" {terms}"
        expressions.append((handle, expression))
    for index, raw_expression in enumerate(x_search_expressions, start=1):
        expression = " ".join(str(raw_expression or "").split())
        if expression:
            label_match = re.search(r"(?:^|\s)from:([A-Za-z0-9_]+)", expression, flags=re.I)
            label = label_match.group(1) if label_match else f"search-{index}"
            expressions.append((label, expression))

    if expressions:
        for start, end_exclusive in inclusive_date_periods(x_start, x_end, batch_mode):
            p_label = period_label(start, end_exclusive)
            for label, expression in expressions:
                query = build_x_search_query(expression, start, end_exclusive)
                collection = _unique_collection(f"x-{label}-{p_label}", seen_collections)
                batches.append(
                    ArchiveBatch(
                        batch_id=f"batch-{len(batches) + 1:04d}",
                        platform="x",
                        label=f"{label} — {p_label}",
                        seed_url=build_x_search_url(query),
                        collection=collection,
                        query_text=query,
                        period_start=start.isoformat(),
                        period_end=(end_exclusive - timedelta(days=1)).isoformat(),
                    )
                )

    if not batches:
        raise ValueError(
            "Specify at least one target profile or URL for Facebook, Instagram, and/or X before previewing or queueing an archive."
        )
    return batches


def validate_image_name(image: str) -> str:
    value = str(image or DEFAULT_IMAGE).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]{0,240}", value):
        raise ValueError("Browsertrix image name contains unsupported characters.")
    return value


def build_docker_command(
    *,
    docker_executable: str,
    run_dir: Path,
    profile_path: Path,
    batch: ArchiveBatch,
    settings: CrawlSettings,
    container_name: str,
) -> list[str]:
    image = validate_image_name(settings.image)
    behaviors = str(settings.behaviors or DEFAULT_BEHAVIORS).strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:,[A-Za-z][A-Za-z0-9]*)*", behaviors):
        raise ValueError("Browsertrix behaviors contain unsupported characters.")
    command = [
        docker_executable,
        "run",
        "--rm",
        "--name",
        container_name,
        "-v",
        f"{run_dir.resolve()}:/crawls",
        "-v",
        f"{profile_path.resolve()}:/profile/profile.tar.gz:ro",
        image,
        "crawl",
        "--url",
        batch.seed_url,
        "--generateWACZ",
        "--collection",
        batch.collection,
        "--scopeType",
        "page-spa",
        "--workers",
        "1",
        "--behaviors",
        behaviors,
        "--alwaysAddBehaviorLinks",
        "--profile",
        "/profile/profile.tar.gz",
        "--behaviorTimeout",
        str(settings.behavior_timeout_seconds),
        "--timeLimit",
        str(settings.time_limit_seconds),
        "--pageLimit",
        str(settings.page_limit),
        "--sizeLimit",
        str(settings.size_limit_mb * 1024 * 1024),
    ]
    if settings.save_final_screenshot:
        command.extend(["--screenshot", "fullPageFinal"])
    if settings.extract_final_text:
        command.extend(["--text", "to-pages", "--text", "final-to-warc"])
    if settings.fail_on_content_check:
        command.append("--failOnContentCheck")
    return command


def find_wacz(run_dir: Path, collection: str) -> Path | None:
    collection_dir = run_dir / "collections" / collection
    matches = sorted(collection_dir.rglob("*.wacz")) if collection_dir.exists() else []
    if not matches:
        matches = sorted(run_dir.rglob(f"{collection}.wacz"))
    return matches[0] if matches else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def browsertrix_exit_status(returncode: int) -> tuple[str, str]:
    if returncode == 0:
        return "completed", ""
    if returncode == 14:
        return "completed — size limit reached", "Browsertrix stopped at the configured WACZ size limit."
    if returncode == 15:
        return "completed — time limit reached", "Browsertrix stopped at the configured crawl time limit."
    return "failed", f"Browsertrix exited with status {returncode}. See the batch log."


def docker_executable() -> str:
    executable = shutil.which("docker")
    if not executable:
        raise RuntimeError(
            "Docker was not found. Install and start Docker Desktop, then pull the configured Browsertrix image before archiving."
        )
    return executable


def check_docker(executable: str) -> None:
    try:
        result = subprocess.run(
            [executable, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Docker Desktop did not respond. Start Docker Desktop and try again.") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Docker server unavailable").strip()
        raise RuntimeError(f"Docker Desktop is unavailable: {detail}")


def _relative_or_absolute(path: Path, base_dir: Path) -> str:
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        return str(path)


def execute_archive_plan(
    *,
    batches: list[ArchiveBatch],
    settings: CrawlSettings,
    module_data_dir: Path,
    profile_path: Path,
    run_id: str,
) -> tuple[Path, list[ArchiveResult]]:
    executable = docker_executable()
    check_docker(executable)
    if not profile_path.is_file():
        raise RuntimeError(
            f"Authenticated browser profile not found: {profile_path}. Create it with Browsertrix before running archives."
        )

    run_dir = module_data_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    settings_payload = asdict(settings)
    plan_payload = {
        "run_id": run_id,
        "created_at": utc_now_iso(),
        "profile_file": _relative_or_absolute(profile_path, module_data_dir),
        "settings": settings_payload,
        "batches": [asdict(batch) for batch in batches],
    }
    (run_dir / "plan.json").write_text(json.dumps(plan_payload, indent=2), encoding="utf-8")

    results: list[ArchiveResult] = []
    for index, batch in enumerate(batches, start=1):
        started_at = utc_now_iso()
        container_name = slugify(f"osint-btx-{run_id}-{index}", 62)
        command = build_docker_command(
            docker_executable=executable,
            run_dir=run_dir,
            profile_path=profile_path,
            batch=batch,
            settings=settings,
            container_name=container_name,
        )
        log_path = run_dir / f"{batch.batch_id}-{batch.collection}.log"
        status = "failed"
        error = ""
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=settings.time_limit_seconds + 300,
                check=False,
            )
            log_path.write_text(
                "COMMAND\n" + json.dumps(command) + "\n\nSTDOUT\n" + completed.stdout + "\n\nSTDERR\n" + completed.stderr,
                encoding="utf-8",
            )
            status, error = browsertrix_exit_status(completed.returncode)
        except subprocess.TimeoutExpired as exc:
            error = f"Browsertrix exceeded the {settings.time_limit_seconds}-second crawl limit plus shutdown allowance."
            subprocess.run([executable, "stop", container_name], capture_output=True, timeout=30, check=False)
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            log_path.write_text(f"TIMEOUT\n{error}\n\nSTDOUT\n{stdout}\n\nSTDERR\n{stderr}", encoding="utf-8")
        except OSError as exc:
            error = f"Could not start Browsertrix: {exc}"
            log_path.write_text(error, encoding="utf-8")

        wacz = find_wacz(run_dir, batch.collection)
        if status.startswith("completed") and wacz is None:
            status = "failed"
            error = "Browsertrix finished but no WACZ file was found. See the batch log."
        result = ArchiveResult(
            batch_id=batch.batch_id,
            platform=batch.platform,
            label=batch.label,
            target_url=batch.seed_url,
            query_text=batch.query_text,
            period_start=batch.period_start,
            period_end=batch.period_end,
            collection=batch.collection,
            status=status,
            wacz_path=_relative_or_absolute(wacz, module_data_dir) if wacz else "",
            wacz_bytes=wacz.stat().st_size if wacz else 0,
            sha256=sha256_file(wacz) if wacz else "",
            log_path=_relative_or_absolute(log_path, module_data_dir),
            error=error,
            started_at=started_at,
            completed_at=utc_now_iso(),
        )
        results.append(result)
        manifest_payload = {**plan_payload, "updated_at": utc_now_iso(), "results": [asdict(item) for item in results]}
        (run_dir / "manifest.json").write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")

    return run_dir, results
