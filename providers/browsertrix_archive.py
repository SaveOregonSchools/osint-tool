from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
import zlib
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse

from providers.wacz_content import XCaptureInspection, inspect_x_wacz


DEFAULT_IMAGE = "webrecorder/browsertrix-crawler:1.14.3"
DEFAULT_BEHAVIORS = "autoscroll,autoplay,autofetch,siteSpecific"
PROFILE_REVIEW_BEHAVIORS = "autoscroll,autoplay,autofetch"
SUPPORTED_PLATFORMS = {"facebook", "instagram", "x"}
X_RATE_LIMIT_MAX_RETRIES_ENV = "OSINT_X_RATE_LIMIT_MAX_RETRIES"
X_RATE_LIMIT_INTERRUPT_COUNT_ENV = "OSINT_X_RATE_LIMIT_INTERRUPT_COUNT"
X_POST_LOAD_DELAY_SECONDS_ENV = "OSINT_X_POST_LOAD_DELAY_SECONDS"
X_AUTH_PREFLIGHT_URL = "https://x.com/settings/account"
X_AUTH_PREFLIGHT_BEHAVIOR = Path(__file__).resolve().parent / "behaviors" / "x_auth_preflight.js"
X_AUTH_INDETERMINATE_CACHE_SECONDS = 5 * 60
X_PROFILE_MAX_MEMBERS = 100_000
X_PROFILE_MAX_EXPANDED_BYTES = 16 * 1024 * 1024 * 1024


def _environment_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}.") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


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
    retain_working_files: bool = False
    expected_x_session_handle: str = ""
    x_rate_limit_max_retries: int = field(
        default_factory=lambda: _environment_integer(X_RATE_LIMIT_MAX_RETRIES_ENV, 4, -1, 20)
    )
    x_rate_limit_interrupt_count: int = field(
        default_factory=lambda: _environment_integer(X_RATE_LIMIT_INTERRUPT_COUNT_ENV, -1, -1, 1000)
    )
    x_post_load_delay_seconds: int = field(
        default_factory=lambda: _environment_integer(X_POST_LOAD_DELAY_SECONDS_ENV, 10, 0, 600)
    )


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
    validation_status: str = ""
    search_successes: int = 0
    search_rate_limits: int = 0
    search_other_statuses: int = 0
    rate_limit_remaining: int | None = None
    rate_limit_reset: int | None = None
    partial: bool = False
    pruned_files: int = 0
    pruned_bytes: int = 0
    compaction_status: str = ""
    crawler_returncode: int | None = None
    crawler_command: list[str] = field(default_factory=list)
    x_session_state: str = ""
    x_account_handle: str = ""
    x_auth_checked_at: str = ""
    x_auth_cache_hit: bool = False
    x_authenticated_requests: int = 0
    profile_refresh_status: str = ""
    reauthentication_required: bool = False
    reauthentication_command: str = ""
    reauthentication_profile_filename: str = ""
    ssh_tunnel_command: str = ""


@dataclass(frozen=True)
class XAuthenticationPreflight:
    state: str
    verified: bool
    account_handle: str
    expected_handle: str
    checked_at: str
    attempts: int
    browsertrix_returncode: int | None
    profile_refresh_status: str
    detail: str
    reauthentication_required: bool
    reauthentication_command: str
    reauthentication_profile_filename: str
    ssh_tunnel_command: str
    cache_hit: bool = False


_X_AUTH_CACHE_LOCK = threading.Lock()
_X_AUTH_CACHE: dict[tuple[str, int, int, str, str], tuple[float | None, XAuthenticationPreflight]] = {}


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
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
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


def validate_x_rate_limit_image(image: str) -> str:
    value = validate_image_name(image)
    tagged_reference = value.split("@", 1)[0]
    match = re.search(r":v?(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9._-]+)?$", tagged_reference)
    if match is not None and tuple(int(part) for part in match.groups()) < (1, 14, 0):
        raise ValueError(
            "X archiving requires a Browsertrix image tagged 1.14.0 or newer so rate-limit detection is available."
        )
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
    image = (
        validate_x_rate_limit_image(settings.image)
        if batch.platform == "x"
        else validate_image_name(settings.image)
    )
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
    if batch.platform == "x":
        command.extend(
            [
                "--postLoadDelay",
                str(settings.x_post_load_delay_seconds),
                "--rateLimitOnMatch",
                r"Something went wrong\. Try reloading\.",
                "--rateLimitOnMatch",
                "Rate limit exceeded",
                "--rateLimitMaxRetries",
                str(settings.x_rate_limit_max_retries),
                "--rateLimitInterruptCount",
                str(settings.x_rate_limit_interrupt_count),
                "--rateLimitTimeout",
                "900",
            ]
        )
    return command


def find_wacz(run_dir: Path, collection: str) -> Path | None:
    collection_dir = run_dir / "collections" / collection
    matches = sorted(collection_dir.rglob("*.wacz")) if collection_dir.exists() else []
    if not matches:
        matches = sorted(run_dir.rglob(f"{collection}.wacz"))
    if len(matches) > 1:
        raise RuntimeError(f"Browsertrix produced multiple WACZ files for collection {collection!r}.")
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
    if returncode == 18:
        return "rate limited", "Browsertrix detected rate-limited content and stopped the crawl."
    return (
        "failed",
        f"Browsertrix exited with status {returncode}. Inspect the retained batch log or the crawler logs inside the WACZ.",
    )


def docker_executable() -> str:
    executable = shutil.which("docker")
    if not executable:
        raise RuntimeError(
            "Docker was not found. Install Docker Engine or Docker Desktop, start its daemon, and pull the configured "
            "Browsertrix image before archiving."
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
        raise RuntimeError("The Docker daemon did not respond. Start Docker Engine or Docker Desktop and try again.") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Docker server unavailable").strip()
        raise RuntimeError(f"The Docker daemon is unavailable: {detail}")


def _simple_profile_filename(value: str) -> str:
    filename = str(value or "").strip()
    if not filename or Path(filename).name != filename or not filename.casefold().endswith(".tar.gz"):
        raise ValueError("Browser profile filenames must be simple .tar.gz filenames.")
    return filename


def _host_command(args: list[str]) -> str:
    if os.name != "nt":
        return shlex.join(args)

    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    return " ".join(quote(value) if re.search(r"[\s'\"`$]", value) else value for value in args)


def build_interactive_profile_command(
    *,
    profiles_dir: Path,
    image: str,
    output_filename: str,
    existing_profile_filename: str = "",
    docker_command: str = "docker",
    start_url: str = X_AUTH_PREFLIGHT_URL,
) -> str:
    """Build a host-appropriate, loopback-only Browsertrix profile command."""
    safe_image = validate_image_name(image)
    output_name = _simple_profile_filename(output_filename)
    command = [
        docker_command,
        "run",
        "--rm",
        "-it",
        "-p",
        "127.0.0.1:6080:6080",
        "-p",
        "127.0.0.1:9223:9223",
        "-v",
        f"{profiles_dir.resolve()}:/crawls",
    ]
    existing_name = ""
    if existing_profile_filename:
        existing_name = _simple_profile_filename(existing_profile_filename)
        command.extend(
            [
                "-v",
                f"{(profiles_dir / existing_name).resolve()}:/profile/old-profile.tar.gz:ro",
            ]
        )
    command.extend(
        [
            safe_image,
            "create-login-profile",
            "--url",
            start_url,
            "--filename",
            f"/crawls/{output_name}",
        ]
    )
    if existing_name:
        command.extend(["--profile", "/profile/old-profile.tar.gz"])
    docker_line = _host_command(command)
    if os.name == "nt":
        directory_line = f"New-Item -ItemType Directory -Force -Path {_host_command([str(profiles_dir.resolve())])}"
    else:
        directory_line = _host_command(["mkdir", "-p", str(profiles_dir.resolve())])
    return directory_line + "\n" + docker_line


def _reauthentication_details(profile_path: Path, image: str) -> tuple[str, str, str]:
    original_name = _simple_profile_filename(profile_path.name)
    base_name = original_name[: -len(".tar.gz")]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    replacement_name = f"{base_name}-reauth-{timestamp}.tar.gz"
    command = build_interactive_profile_command(
        profiles_dir=profile_path.parent,
        image=image,
        output_filename=replacement_name,
        existing_profile_filename=original_name,
    )
    tunnel = (
        "ssh -N -L 9223:127.0.0.1:9223 -L 6080:127.0.0.1:6080 "
        "<user>@<server>"
    )
    return replacement_name, command, tunnel


def build_x_auth_preflight_command(
    *,
    docker_executable: str,
    work_dir: Path,
    profile_path: Path,
    image: str,
    behavior_path: Path,
    container_name: str,
) -> list[str]:
    """Build a one-page X session verification crawl in temporary storage."""
    return [
        docker_executable,
        "run",
        "--rm",
        "--name",
        container_name,
        "-v",
        f"{work_dir.resolve()}:/crawls",
        "-v",
        f"{profile_path.resolve()}:/profile/profile.tar.gz:ro",
        "-v",
        f"{behavior_path.resolve()}:/behaviors/x_auth_preflight.js:ro",
        validate_x_rate_limit_image(image),
        "crawl",
        "--url",
        X_AUTH_PREFLIGHT_URL,
        "--collection",
        "x-auth-preflight",
        "--scopeType",
        "page",
        "--pageLimit",
        "1",
        "--workers",
        "1",
        "--behaviors",
        "siteSpecific",
        "--customBehaviors",
        "/behaviors/x_auth_preflight.js",
        "--behaviorTimeout",
        "20",
        "--postLoadDelay",
        "2",
        "--timeLimit",
        "60",
        "--maxPageRetries",
        "0",
        "--profile",
        "/profile/profile.tar.gz",
        "--saveProfile",
        "/crawls/refreshed-profile.tar.gz",
        "--failOnFailedSeed",
        "--failOnContentCheck",
    ]


def _profile_fingerprint(profile_path: Path) -> tuple[str, int, int]:
    stat_result = profile_path.stat()
    return os.path.normcase(str(profile_path.resolve())), stat_result.st_size, stat_result.st_mtime_ns


def _preflight_signal(text: str) -> tuple[str, str]:
    # A later redirect or reinjection can leave multiple markers in the logs.
    # Fail closed if any page observed the logged-out state.
    if "x_auth_preflight_logged_out" in text or re.search(r"(?:reason[\"']?\s*[:=]\s*[\"']?)not_logged_in", text):
        return "logged_out", ""
    verified = re.search(r"x_auth_preflight_verified(?:\s+handle=@([A-Za-z0-9_]{1,15}))?", text)
    if verified:
        return "verified", verified.group(1) or ""
    if "x_auth_preflight_indeterminate" in text or "x_auth_indeterminate" in text:
        return "indeterminate", ""
    return "indeterminate", ""


def _read_preflight_logs(work_dir: Path, process_output: str) -> tuple[str, str]:
    state, handle = _preflight_signal(process_output)
    if state == "logged_out":
        return state, handle
    scanned_bytes = 0
    for path in sorted(work_dir.rglob("*.log")) + sorted(work_dir.rglob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as source:
                for line in source:
                    scanned_bytes += len(line.encode("utf-8", errors="replace"))
                    log_state, log_handle = _preflight_signal(line)
                    if log_state == "logged_out":
                        return log_state, log_handle
                    if log_state == "verified" and state != "verified":
                        state, handle = log_state, log_handle
                    if scanned_bytes >= 5 * 1024 * 1024:
                        return state, handle
        except OSError:
            continue
    return state, handle


def _validate_browser_profile_archive(profile_path: Path) -> None:
    if not profile_path.is_file() or profile_path.stat().st_size == 0:
        raise RuntimeError("Browsertrix did not save a refreshed profile archive")
    try:
        with tarfile.open(profile_path, mode="r:gz") as archive:
            file_names: list[str] = []
            members = archive.getmembers()
            if len(members) > X_PROFILE_MAX_MEMBERS:
                raise RuntimeError("the refreshed browser profile contains too many archive members")
            expanded_bytes = 0
            for member in members:
                normalized = member.name.replace("\\", "/")
                parts = [part for part in normalized.split("/") if part not in {"", "."}]
                if normalized.startswith("/") or ".." in parts:
                    raise RuntimeError("the refreshed browser profile contains an unsafe member path")
                if not (member.isfile() or member.isdir()):
                    raise RuntimeError("the refreshed browser profile contains an unsafe archive member type")
                if member.isfile():
                    expanded_bytes += member.size
                    if expanded_bytes > X_PROFILE_MAX_EXPANDED_BYTES:
                        raise RuntimeError("the refreshed browser profile is unreasonably large when expanded")
                    file_names.append(normalized)
    except (OSError, tarfile.TarError) as exc:
        raise RuntimeError("the refreshed browser profile is not a valid tar.gz archive") from exc
    if not any(name.endswith("/Local State") or name == "Local State" for name in file_names):
        raise RuntimeError("the refreshed browser profile is missing Chromium Local State")
    if not any(Path(name).name == "Cookies" for name in file_names):
        raise RuntimeError("the refreshed browser profile is missing its cookie database")


def _promote_refreshed_profile(candidate: Path, profile_path: Path) -> str:
    _validate_browser_profile_archive(candidate)
    original_mode = profile_path.stat().st_mode
    with tempfile.NamedTemporaryFile(
        prefix=f".{profile_path.name}.", suffix=".refresh", dir=profile_path.parent, delete=False
    ) as staged_handle:
        staged_path = Path(staged_handle.name)
        with candidate.open("rb") as source:
            shutil.copyfileobj(source, staged_handle, length=1024 * 1024)
        staged_handle.flush()
        os.fsync(staged_handle.fileno())
    try:
        os.chmod(staged_path, original_mode)
        _validate_browser_profile_archive(staged_path)
        staged_path.replace(profile_path)
    finally:
        if staged_path.exists():
            staged_path.unlink()
    return "refreshed"


def _stop_container(executable: str, container_name: str) -> None:
    try:
        stopped = subprocess.run(
            [executable, "stop", container_name], capture_output=True, text=True, timeout=20, check=False
        )
        if stopped.returncode != 0:
            subprocess.run(
                [executable, "kill", container_name], capture_output=True, text=True, timeout=10, check=False
            )
    except (OSError, subprocess.TimeoutExpired):
        try:
            subprocess.run(
                [executable, "kill", container_name], capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_x_auth_preflight_once(
    *,
    executable: str,
    profile_path: Path,
    image: str,
    expected_handle: str,
) -> tuple[str, str, int | None, str, str]:
    if not X_AUTH_PREFLIGHT_BEHAVIOR.is_file():
        return "indeterminate", "", None, "not_attempted", "The X authentication behavior file is missing."
    with tempfile.TemporaryDirectory(prefix="osint-x-auth-") as temporary:
        work_dir = Path(temporary)
        container_name = slugify(f"osint-x-auth-{os.getpid()}-{time.time_ns()}", 62)
        command = build_x_auth_preflight_command(
            docker_executable=executable,
            work_dir=work_dir,
            profile_path=profile_path,
            image=image,
            behavior_path=X_AUTH_PREFLIGHT_BEHAVIOR,
            container_name=container_name,
        )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _stop_container(executable, container_name)
            return "indeterminate", "", None, "not_saved", "The X authentication check timed out."
        except OSError as exc:
            return "indeterminate", "", None, "not_saved", f"Browsertrix could not start the X authentication check: {exc}"

        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        state, account_handle = _read_preflight_logs(work_dir, output)
        if state == "verified" and completed.returncode != 0:
            state = "indeterminate"
            account_handle = ""
        if state == "verified" and expected_handle:
            if not account_handle:
                state = "indeterminate"
            elif account_handle.casefold() != expected_handle.casefold():
                state = "wrong_account"

        refresh_status = "not_saved"
        candidate = work_dir / "refreshed-profile.tar.gz"
        if state == "verified" and candidate.is_file():
            try:
                refresh_status = _promote_refreshed_profile(candidate, profile_path)
            except (OSError, RuntimeError) as exc:
                refresh_status = "refresh_failed"
                state = "indeterminate"
                account_handle = ""
                detail = (
                    "X displayed an authenticated session, but the refreshed profile could not be promoted safely: "
                    f"{exc}"
                )
            else:
                detail = "X authentication was verified and the refreshed Browsertrix profile was saved."
        elif state == "verified":
            state = "indeterminate"
            account_handle = ""
            detail = (
                "X displayed an authenticated session, but Browsertrix did not emit the required refreshed profile. "
                "The original profile was retained and no capture will start."
            )
        elif state == "logged_out":
            detail = "The saved Browsertrix profile reached X's login flow; its X session is logged out or expired."
        elif state == "wrong_account":
            detail = (
                f"X is authenticated as @{account_handle}, but the expected session account is @{expected_handle}."
            )
        elif expected_handle and not account_handle:
            detail = f"X loaded, but the preflight could not verify the expected session account @{expected_handle}."
        else:
            detail = (
                "The X authentication check was indeterminate because authenticated navigation did not appear. "
                f"Browsertrix exited with status {completed.returncode}."
            )
        return state, account_handle, completed.returncode, refresh_status, detail


def preflight_x_authentication(
    *,
    executable: str,
    profile_path: Path,
    image: str,
    expected_handle: str = "",
) -> XAuthenticationPreflight:
    """Verify X in a real browser, refresh a valid profile, and fail closed otherwise."""
    normalized_expected = normalize_x_handle(expected_handle) if str(expected_handle or "").strip() else ""
    fingerprint = _profile_fingerprint(profile_path)
    cache_key = (*fingerprint, validate_x_rate_limit_image(image), normalized_expected.casefold())
    now = time.monotonic()
    with _X_AUTH_CACHE_LOCK:
        cached = _X_AUTH_CACHE.get(cache_key)
        if cached and (cached[0] is None or cached[0] > now):
            return replace(cached[1], cache_hit=True)

    state = "indeterminate"
    account_handle = ""
    returncode: int | None = None
    refresh_status = "not_attempted"
    detail = "The X authentication check did not run."
    attempts = 0
    for attempts in range(1, 3):
        state, account_handle, returncode, refresh_status, detail = _run_x_auth_preflight_once(
            executable=executable,
            profile_path=profile_path,
            image=image,
            expected_handle=normalized_expected,
        )
        if state != "indeterminate":
            break

    replacement_name, command, tunnel = _reauthentication_details(profile_path, image)
    requires_reauthentication = state in {"logged_out", "wrong_account"}
    result = XAuthenticationPreflight(
        state=state,
        verified=state == "verified",
        account_handle=account_handle,
        expected_handle=normalized_expected,
        checked_at=utc_now_iso(),
        attempts=attempts,
        browsertrix_returncode=returncode,
        profile_refresh_status=refresh_status,
        detail=detail,
        reauthentication_required=requires_reauthentication,
        reauthentication_command=command,
        reauthentication_profile_filename=replacement_name,
        ssh_tunnel_command=tunnel,
    )

    final_fingerprint = _profile_fingerprint(profile_path)
    final_key = (*final_fingerprint, validate_x_rate_limit_image(image), normalized_expected.casefold())
    if result.state == "indeterminate":
        expires_at = now + X_AUTH_INDETERMINATE_CACHE_SECONDS
    else:
        expires_at = None
    with _X_AUTH_CACHE_LOCK:
        stale_keys = [key for key in _X_AUTH_CACHE if key[0] == final_key[0] and key != final_key]
        for stale_key in stale_keys:
            _X_AUTH_CACHE.pop(stale_key, None)
        # A verified profile is intentionally not cached: every X job gets a
        # current positive browser check. Negative results remain keyed to the
        # profile fingerprint and disappear as soon as it is replaced.
        if result.verified:
            _X_AUTH_CACHE.pop(final_key, None)
        else:
            _X_AUTH_CACHE[final_key] = (expires_at, result)
    return result


def _relative_or_absolute(path: Path, base_dir: Path) -> str:
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        return str(path)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Replace a JSON metadata file only after its complete contents are on disk."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _write_manifest(run_dir: Path, plan_payload: dict[str, Any], results: list[ArchiveResult]) -> None:
    manifest_payload = {
        **plan_payload,
        "updated_at": utc_now_iso(),
        "results": [asdict(item) for item in results],
    }
    _write_json_atomic(run_dir / "manifest.json", manifest_payload)


def _path_size_and_files(path: Path) -> tuple[int, int]:
    if path.is_file():
        return path.stat().st_size, 1
    total_bytes = 0
    total_files = 0
    if path.is_dir():
        for item in path.rglob("*"):
            if item.is_file():
                total_bytes += item.stat().st_size
                total_files += 1
    return total_bytes, total_files


def _validate_wacz_package(wacz: Path) -> None:
    """Verify package structure, ZIP CRCs, and the WACZ-declared sizes and hashes."""
    if not wacz.is_file():
        raise RuntimeError("the WACZ file is missing")
    try:
        with zipfile.ZipFile(wacz) as package:
            member_names = package.namelist()
            names = set(member_names)
            if len(member_names) != len(names):
                raise RuntimeError("the WACZ contains duplicate ZIP member names")
            if "datapackage.json" not in names:
                raise RuntimeError("the WACZ is missing datapackage.json")
            datapackage_bytes = package.read("datapackage.json")
            try:
                datapackage = json.loads(datapackage_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RuntimeError("datapackage.json is not valid JSON") from exc
            resources = datapackage.get("resources") if isinstance(datapackage, dict) else None
            if not isinstance(resources, list) or not resources:
                raise RuntimeError("datapackage.json has no declared resources")

            declared_paths: set[str] = set()
            for index, resource in enumerate(resources, start=1):
                if not isinstance(resource, dict):
                    raise RuntimeError(f"datapackage resource {index} is not an object")
                resource_path = str(resource.get("path") or "").strip()
                if not resource_path or resource_path in declared_paths:
                    raise RuntimeError(f"datapackage resource {index} has a missing or duplicate path")
                declared_paths.add(resource_path)
                if resource_path not in names:
                    raise RuntimeError(f"declared WACZ resource {resource_path!r} is missing")
                expected_bytes = resource.get("bytes")
                if not isinstance(expected_bytes, int) or expected_bytes < 0:
                    raise RuntimeError(f"declared WACZ resource {resource_path!r} has an invalid byte count")
                if package.getinfo(resource_path).file_size != expected_bytes:
                    raise RuntimeError(f"declared WACZ resource {resource_path!r} has the wrong byte count")
                expected_hash = str(resource.get("hash") or "").casefold().strip()
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_hash):
                    raise RuntimeError(f"declared WACZ resource {resource_path!r} has an invalid SHA-256")
                digest = hashlib.sha256()
                validate_gzip = resource_path.startswith("archive/") and resource_path.endswith(".warc.gz")
                gzip_validator: Any = None
                gzip_members = 0
                with package.open(resource_path) as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                        pending = chunk if validate_gzip else b""
                        while pending:
                            if gzip_validator is None:
                                gzip_validator = zlib.decompressobj(16 + zlib.MAX_WBITS)
                            try:
                                gzip_validator.decompress(pending)
                            except zlib.error as exc:
                                raise RuntimeError(f"declared WACZ resource {resource_path!r} is not valid gzip") from exc
                            if gzip_validator.eof:
                                pending = gzip_validator.unused_data
                                gzip_members += 1
                                gzip_validator = None
                            else:
                                pending = b""
                if validate_gzip and (gzip_validator is not None or gzip_members == 0):
                    raise RuntimeError(f"declared WACZ resource {resource_path!r} is a truncated gzip stream")
                if digest.hexdigest() != expected_hash.removeprefix("sha256:"):
                    raise RuntimeError(f"declared WACZ resource {resource_path!r} failed SHA-256 validation")

            missing: list[str] = []
            if not any(
                name.startswith("archive/") and (name.endswith(".warc") or name.endswith(".warc.gz"))
                for name in declared_paths
            ):
                missing.append("archive WARC")
            if not any(name.startswith("pages/") and name.endswith(".jsonl") for name in declared_paths):
                missing.append("pages JSONL")
            if not any(
                name.startswith("indexes/") and (name.endswith(".cdx") or name.endswith(".cdx.gz"))
                for name in declared_paths
            ):
                missing.append("index CDX")
            if missing:
                raise RuntimeError("the WACZ is missing " + ", ".join(missing))

            if "datapackage-digest.json" in names:
                try:
                    digest_record = json.loads(package.read("datapackage-digest.json"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise RuntimeError("datapackage-digest.json is not valid JSON") from exc
                expected_package_hash = (
                    str(digest_record.get("hash") or "").casefold().strip()
                    if isinstance(digest_record, dict) and digest_record.get("path") == "datapackage.json"
                    else ""
                )
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_package_hash):
                    raise RuntimeError("datapackage-digest.json has an invalid datapackage SHA-256")
                if hashlib.sha256(datapackage_bytes).hexdigest() != expected_package_hash.removeprefix("sha256:"):
                    raise RuntimeError("datapackage.json failed its declared SHA-256 validation")
            checked_members = declared_paths | {"datapackage.json", "datapackage-digest.json"}
            for member in package.infolist():
                if member.is_dir() or member.filename in checked_members:
                    continue
                with package.open(member) as handle:
                    for _chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        pass
    except zipfile.BadZipFile as exc:
        raise RuntimeError("the WACZ is not a valid ZIP") from exc


def compact_archive_output(
    run_dir: Path,
    collection: str,
    wacz: Path,
    log_path: Path,
    *,
    package_validated: bool = False,
) -> tuple[int, int]:
    """Keep the self-contained WACZ and JSON manifests, removing crawler work files."""
    run_root = run_dir.resolve()
    collections_root = (run_root / "collections").resolve()
    collection_dir = (collections_root / collection).resolve()
    wacz_path = wacz.resolve()
    if collection_dir.parent != collections_root or wacz_path.parent != collection_dir:
        raise RuntimeError("Refusing to compact an archive outside its expected collection directory.")
    if not collection_dir.is_dir():
        raise RuntimeError("Refusing to compact because the expected collection directory is missing.")
    packages = [item.resolve() for item in collection_dir.rglob("*.wacz") if item.is_file()]
    if packages != [wacz_path]:
        raise RuntimeError("Refusing to compact unless the collection contains exactly the expected WACZ file.")

    resolved_log = log_path.resolve()
    try:
        resolved_log.relative_to(run_root)
    except ValueError as exc:
        raise RuntimeError("Refusing to remove a crawler log outside its run directory.") from exc

    if not package_validated:
        try:
            _validate_wacz_package(wacz_path)
        except RuntimeError as exc:
            raise RuntimeError(f"Refusing to compact crawler work files because {exc}.") from exc

    pruned_bytes = 0
    pruned_files = 0
    for child in collection_dir.iterdir():
        if child.resolve() == wacz_path:
            continue
        child_bytes, child_files = _path_size_and_files(child)
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        pruned_bytes += child_bytes
        pruned_files += child_files

    if resolved_log.is_file():
        log_bytes, log_files = _path_size_and_files(resolved_log)
        resolved_log.unlink()
        pruned_bytes += log_bytes
        pruned_files += log_files
    return pruned_files, pruned_bytes


def execute_archive_plan(
    *,
    batches: list[ArchiveBatch],
    settings: CrawlSettings,
    module_data_dir: Path,
    profile_path: Path,
    run_id: str,
    x_authentication: XAuthenticationPreflight | None = None,
) -> tuple[Path, list[ArchiveResult]]:
    executable = docker_executable()
    check_docker(executable)
    if not profile_path.is_file():
        raise RuntimeError(
            f"Authenticated browser profile not found: {profile_path}. Create it with Browsertrix before running archives."
        )

    has_x_batches = any(batch.platform == "x" for batch in batches)
    if has_x_batches and x_authentication is None:
        x_authentication = preflight_x_authentication(
            executable=executable,
            profile_path=profile_path,
            image=settings.image,
            expected_handle=settings.expected_x_session_handle,
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
    if x_authentication is not None:
        plan_payload["x_authentication_preflight"] = asdict(x_authentication)
    _write_json_atomic(run_dir / "plan.json", plan_payload)

    results: list[ArchiveResult] = []
    for index, batch in enumerate(batches, start=1):
        started_at = utc_now_iso()
        if batch.platform == "x" and x_authentication is not None and not x_authentication.verified:
            if x_authentication.reauthentication_required:
                error = (
                    x_authentication.detail
                    + " No archive capture was started. Reopen the saved profile with the reauthentication command "
                    "shown in this job, finish X login/2FA, click Create Profile, and rerun using the new profile filename."
                )
                status = "failed — X reauthentication required"
            else:
                error = (
                    x_authentication.detail
                    + " No archive capture was started. Retry after checking Docker, network access, and any X "
                    "challenge; if the result persists, use the supplied profile reauthentication command."
                )
                status = "failed — X authentication unverified"
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
                    status=status,
                    error=error,
                    started_at=started_at,
                    completed_at=utc_now_iso(),
                    validation_status=f"authentication_{x_authentication.state}",
                    compaction_status="not_started",
                    x_session_state=x_authentication.state,
                    x_account_handle=x_authentication.account_handle,
                    x_auth_checked_at=x_authentication.checked_at,
                    x_auth_cache_hit=x_authentication.cache_hit,
                    profile_refresh_status=x_authentication.profile_refresh_status,
                    reauthentication_required=x_authentication.reauthentication_required,
                    reauthentication_command=x_authentication.reauthentication_command,
                    reauthentication_profile_filename=x_authentication.reauthentication_profile_filename,
                    ssh_tunnel_command=x_authentication.ssh_tunnel_command,
                )
            )
            _write_manifest(run_dir, plan_payload, results)
            continue
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
        crawler_returncode: int | None = None
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
            crawler_returncode = completed.returncode
            status, error = browsertrix_exit_status(completed.returncode)
        except subprocess.TimeoutExpired as exc:
            error = f"Browsertrix exceeded the {settings.time_limit_seconds}-second crawl limit plus shutdown allowance."
            shutdown_detail = ""
            try:
                stopped = subprocess.run(
                    [executable, "stop", container_name], capture_output=True, text=True, timeout=30, check=False
                )
                if stopped.returncode != 0:
                    shutdown_detail = (stopped.stderr or stopped.stdout or "docker stop failed").strip()
                    subprocess.run(
                        [executable, "kill", container_name], capture_output=True, text=True, timeout=15, check=False
                    )
            except (OSError, subprocess.TimeoutExpired) as stop_exc:
                shutdown_detail = f"container shutdown also failed: {stop_exc}"
                try:
                    subprocess.run(
                        [executable, "kill", container_name], capture_output=True, text=True, timeout=15, check=False
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if shutdown_detail:
                error += f" {shutdown_detail}"
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            log_path.write_text(f"TIMEOUT\n{error}\n\nSTDOUT\n{stdout}\n\nSTDERR\n{stderr}", encoding="utf-8")
        except OSError as exc:
            error = f"Could not start Browsertrix: {exc}"
            log_path.write_text(error, encoding="utf-8")

        crawler_reported_rate_limit = status == "rate limited"
        try:
            wacz = find_wacz(run_dir, batch.collection)
        except RuntimeError as exc:
            wacz = None
            status = "failed"
            error = str(exc)
        if status.startswith("completed") and wacz is None:
            status = "failed"
            error = "Browsertrix finished but no WACZ file was found. See the batch log."
        wacz_structure_valid = False
        package_validation_status = ""
        if wacz is not None:
            try:
                _validate_wacz_package(wacz)
            except (OSError, RuntimeError) as exc:
                status = "rate limited" if crawler_reported_rate_limit and batch.platform == "x" else "failed"
                error = f"The WACZ package could not be validated: {exc}."
                if status == "rate limited":
                    error = "Browsertrix reported an X rate limit. " + error
                package_validation_status = "package_invalid"
            else:
                wacz_structure_valid = True
                package_validation_status = "package_valid"
        inspection: XCaptureInspection | None = None
        if batch.platform == "x" and wacz is not None and wacz_structure_valid:
            try:
                inspection = inspect_x_wacz(wacz)
            except (OSError, ValueError, EOFError, zlib.error, zipfile.BadZipFile) as exc:
                status = "failed"
                error = f"The X WACZ could not be validated: {exc}"
            else:
                if inspection.authentication_state != "authenticated":
                    if inspection.authentication_state == "logged_out":
                        auth_detail = "The X capture reached a logged-out page after the preflight passed."
                        status = "failed — X authentication lost"
                    elif inspection.guest_requests:
                        auth_detail = (
                            "The X capture used a guest session after the preflight passed; no authenticated "
                            "SearchTimeline request was preserved."
                        )
                        status = "failed — X authentication lost"
                    elif inspection.authentication_state == "request_only":
                        auth_detail = (
                            "The X WACZ contains an authenticated request, but not enough matching search or "
                            "logged-in page evidence to verify that authentication persisted through capture."
                        )
                        status = "failed — X authentication unverified"
                    else:
                        auth_detail = (
                            "The X WACZ contains no positive authenticated-request evidence, so the saved session "
                            "cannot be verified for this capture."
                        )
                        status = "failed — X authentication unverified"
                    inspection = replace(
                        inspection,
                        classification="authentication_failed",
                        detail=auth_detail,
                    )
                    error = auth_detail + " Reopen or reauthenticate the saved Browsertrix profile before retrying."
                elif inspection.classification == "rate_limited_partial":
                    status = "failed — partial rate limited"
                    error = inspection.detail
                elif inspection.classification in {"rate_limited_empty", "rate_limited_shell"}:
                    status = "rate limited"
                    error = inspection.detail
                elif inspection.classification == "authentication_failed":
                    status = "failed — X authentication lost"
                    error = inspection.detail
                elif inspection.classification.startswith("invalid"):
                    status = "failed"
                    error = inspection.detail
                elif crawler_reported_rate_limit and inspection.search_successes:
                    detail = (
                        "Browsertrix stopped for rate limiting after "
                        f"{inspection.search_successes} usable X SearchTimeline response(s); the capture may be partial."
                    )
                    inspection = replace(
                        inspection,
                        classification="rate_limited_partial",
                        detail=detail,
                    )
                    status = "failed — partial rate limited"
                    error = detail
        wacz_bytes = wacz.stat().st_size if wacz else 0
        wacz_sha256 = sha256_file(wacz) if wacz else ""
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
            wacz_bytes=wacz_bytes,
            sha256=wacz_sha256,
            log_path=_relative_or_absolute(log_path, module_data_dir) if log_path.is_file() else "",
            error=error,
            started_at=started_at,
            completed_at=utc_now_iso(),
            validation_status=inspection.classification if inspection else package_validation_status,
            search_successes=inspection.search_successes if inspection else 0,
            search_rate_limits=inspection.search_rate_limits if inspection else 0,
            search_other_statuses=inspection.search_other_statuses if inspection else 0,
            rate_limit_remaining=inspection.rate_limit_remaining if inspection else None,
            rate_limit_reset=inspection.rate_limit_reset if inspection else None,
            partial=inspection.is_partial if inspection else False,
            compaction_status=(
                "retained_by_request"
                if settings.retain_working_files
                else "retained_invalid_package"
                if wacz is not None and not wacz_structure_valid
                else "not_available"
                if wacz is None
                else "pending"
            ),
            crawler_returncode=crawler_returncode,
            crawler_command=command,
            x_session_state=(
                inspection.authentication_state
                if inspection is not None
                else x_authentication.state
                if x_authentication is not None
                else ""
            ),
            x_account_handle=x_authentication.account_handle if x_authentication is not None else "",
            x_auth_checked_at=x_authentication.checked_at if x_authentication is not None else "",
            x_auth_cache_hit=x_authentication.cache_hit if x_authentication is not None else False,
            x_authenticated_requests=inspection.authenticated_requests if inspection is not None else 0,
            profile_refresh_status=x_authentication.profile_refresh_status if x_authentication is not None else "",
            reauthentication_required=(
                inspection is not None and inspection.authentication_state == "logged_out"
            ),
            reauthentication_command=(
                x_authentication.reauthentication_command if x_authentication is not None else ""
            ),
            reauthentication_profile_filename=(
                x_authentication.reauthentication_profile_filename if x_authentication is not None else ""
            ),
            ssh_tunnel_command=x_authentication.ssh_tunnel_command if x_authentication is not None else "",
        )
        results.append(result)
        # Persist the capture result before deleting any redundant crawler files.
        _write_manifest(run_dir, plan_payload, results)

        if wacz is not None and wacz_structure_valid and not settings.retain_working_files:
            try:
                result.pruned_files, result.pruned_bytes = compact_archive_output(
                    run_dir,
                    batch.collection,
                    wacz,
                    log_path,
                    package_validated=True,
                )
            except (OSError, RuntimeError) as exc:
                result.compaction_status = "failed"
                capture_status = result.status
                if result.status.startswith("completed"):
                    result.status = "failed — compaction incomplete"
                result.error = (result.error + " " if result.error else "") + (
                    f"Archive compaction was incomplete after capture status {capture_status!r}: {exc}"
                )
            else:
                result.compaction_status = "compacted"
            result.log_path = _relative_or_absolute(log_path, module_data_dir) if log_path.is_file() else ""
            _write_manifest(run_dir, plan_payload, results)

    return run_dir, results
