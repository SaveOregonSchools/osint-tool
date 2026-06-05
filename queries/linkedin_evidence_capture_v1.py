"""
LinkedIn Evidence Capture v1

Drop-in query plugin for the Flask Social OSINT query console.

Purpose:
- Accept a CSV/pasted list of already-identified LinkedIn profile URLs.
- Launch a visible Playwright browser using a persistent local browser profile.
- Let the user manually log in and complete any 2FA/challenges.
- For each supplied profile, capture HTML + screenshots for:
  - main profile page after scrolling to bottom
  - Experience detail page
  - Education detail page
  - Volunteering detail page
  - Licenses & certifications detail page
- Store all output under data/linkedin_evidence_capture_v1/ so it does not mix
  with the previous LinkedIn collector module.

Boundaries:
- This does not store your LinkedIn password.
- This does not bypass CAPTCHA, 2FA, login challenges, rate limits, or access controls.
- It only captures pages visible to the logged-in browser session you control.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except Exception:  # pragma: no cover - lets plugin load before dependency install
    sync_playwright = None
    PlaywrightTimeoutError = Exception


META = {
    "key": "linkedin_evidence_capture_v1",
    "name": "LinkedIn Evidence Capture v1",
    "description": (
        "Browser-assisted LinkedIn evidence capture. Upload or paste CSV rows with names and "
        "LinkedIn URLs. The module opens a visible browser so you can log in and complete any "
        "2FA/challenge manually. It then scrolls/captures the main profile page and the detail "
        "pages for Experience, Education, Volunteering, and Licenses & certifications. Output is "
        "stored separately under data/linkedin_evidence_capture_v1/."
    ),
}

HEADERS = [
    "target_id",
    "input_name",
    "linkedin_url",
    "status",
    "main_html",
    "main_screenshot",
    "experience_html",
    "experience_screenshot",
    "education_html",
    "education_screenshot",
    "volunteering_html",
    "volunteering_screenshot",
    "certifications_html",
    "certifications_screenshot",
    "error_message",
    "collected_at",
]

BASE_DIR = Path(__file__).resolve().parents[1]
MODULE_DATA_DIR = BASE_DIR / "data" / "linkedin_evidence_capture_v1"
RUNS_DIR = MODULE_DATA_DIR / "runs"
USER_DATA_DIR = MODULE_DATA_DIR / "browser_profile"
DB_PATH = MODULE_DATA_DIR / "linkedin_evidence_capture.sqlite"

SECTION_SPECS = [
    ("main", ""),
    ("experience", "experience"),
    ("education", "education"),
    ("volunteering", "volunteering-experiences"),
    ("certifications", "certifications"),
]


@dataclass
class Target:
    input_name: str
    linkedin_url: str
    notes: str = ""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")


def _slug(text: str, max_len: int = 90) -> str:
    text = re.sub(r"https?://", "", text or "")
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return (text[:max_len] or "profile")


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _short_path(path: Optional[Path]) -> str:
    if not path:
        return ""
    try:
        return str(path.relative_to(BASE_DIR))
    except Exception:
        return str(path)


def _profile_slug_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if "in" in parts:
        idx = parts.index("in")
        if len(parts) > idx + 1:
            return parts[idx + 1]
    return _slug(url)


def normalize_linkedin_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if "linkedin.com" not in netloc:
        return url
    # Keep only the profile path. Drop query, fragment, and any section/detail tails.
    parts = [p for p in parsed.path.split("/") if p]
    if "in" in parts:
        idx = parts.index("in")
        if len(parts) > idx + 1:
            path = f"/in/{parts[idx + 1]}/"
        else:
            path = parsed.path
    else:
        path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
    return urlunparse(("https", "www.linkedin.com", path, "", "", ""))


def detail_url(profile_url: str, detail_slug: str) -> str:
    base = normalize_linkedin_url(profile_url).rstrip("/")
    if not detail_slug:
        return base + "/"
    return f"{base}/details/{detail_slug}/"


def init_storage() -> None:
    MODULE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_label TEXT,
                target_count INTEGER,
                notes TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_captures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                target_id INTEGER,
                input_name TEXT,
                linkedin_url TEXT,
                status TEXT,
                main_html TEXT,
                main_screenshot TEXT,
                experience_html TEXT,
                experience_screenshot TEXT,
                education_html TEXT,
                education_screenshot TEXT,
                volunteering_html TEXT,
                volunteering_screenshot TEXT,
                certifications_html TEXT,
                certifications_screenshot TEXT,
                error_message TEXT,
                collected_at TEXT
            )
            """
        )


def read_uploaded_csv(form: Dict[str, Any]) -> str:
    files = form.get("_files")
    if files:
        uploaded = files.get("csv_file")
        if uploaded and getattr(uploaded, "filename", ""):
            raw = uploaded.read()
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    return raw.decode(enc)
                except Exception:
                    continue
            return raw.decode("utf-8", errors="replace")
    return str(form.get("csv_text", "") or "")


def _looks_like_url(value: str) -> bool:
    return "linkedin.com/in/" in (value or "").lower() or "linkedin.com/pub/" in (value or "").lower()


def parse_targets(csv_text: str) -> List[Target]:
    csv_text = (csv_text or "").strip()
    if not csv_text:
        raise ValueError("Provide a CSV upload or paste names and LinkedIn URLs into the text box.")

    sample = csv_text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except Exception:
        dialect = csv.excel

    rows = list(csv.reader(io.StringIO(csv_text), dialect))
    rows = [[cell.strip() for cell in row] for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("The CSV/text input did not contain any usable rows.")

    first = [c.lower().strip().replace(" ", "_") for c in rows[0]]
    url_header_names = {"linkedin_url", "url", "profile_url", "linkedin", "link"}
    name_header_names = {"person_name", "name", "full_name", "input_name", "person"}
    notes_header_names = {"notes", "note", "description"}

    has_header = any(h in url_header_names for h in first) or any(h in name_header_names for h in first)
    targets: List[Target] = []

    if has_header:
        reader = csv.DictReader(io.StringIO(csv_text), dialect=dialect)
        headers = [str(h or "").lower().strip().replace(" ", "_") for h in (reader.fieldnames or [])]
        header_map = {str(h or "").lower().strip().replace(" ", "_"): h for h in (reader.fieldnames or [])}
        url_key = next((header_map[h] for h in headers if h in url_header_names), None)
        name_key = next((header_map[h] for h in headers if h in name_header_names), None)
        notes_key = next((header_map[h] for h in headers if h in notes_header_names), None)
        if not url_key:
            raise ValueError(f"Could not find a LinkedIn URL column. Headers found: {reader.fieldnames}")
        for row in reader:
            url = normalize_linkedin_url(row.get(url_key, ""))
            if not url:
                continue
            targets.append(Target(input_name=(row.get(name_key, "") if name_key else "").strip(), linkedin_url=url, notes=(row.get(notes_key, "") if notes_key else "").strip()))
    else:
        for row in rows:
            url_idx = next((i for i, cell in enumerate(row) if _looks_like_url(cell)), None)
            if url_idx is None:
                continue
            url = normalize_linkedin_url(row[url_idx])
            name = ""
            notes = ""
            if url_idx > 0:
                name = row[0]
                notes = " | ".join(row[1:url_idx])
            elif len(row) > 1:
                name = row[1]
                notes = " | ".join(row[2:])
            targets.append(Target(input_name=name, linkedin_url=url, notes=notes))

    if not targets:
        raise ValueError("No usable LinkedIn profile URLs were found in the input.")
    return targets


def render_fields(form: Dict[str, Any]) -> str:
    def val(name: str, default: str = "") -> str:
        return html.escape(str(form.get(name, default) or ""), quote=True)

    return f"""
    <div class="grid">
      <div>
        <label for="csv_file">CSV file</label>
        <input type="file" id="csv_file" name="csv_file" accept=".csv,.txt,text/csv,text/plain">
        <div class="subtle">Optional. Expected columns: person_name, linkedin_url, notes. Headerless two-column rows also work.</div>
      </div>
      <div>
        <label for="max_profiles">Max profiles this run</label>
        <input type="number" id="max_profiles" name="max_profiles" min="1" value="{val('max_profiles','25')}">
      </div>
      <div>
        <label for="delay_seconds">Delay between pages, seconds</label>
        <input type="number" id="delay_seconds" name="delay_seconds" min="1" value="{val('delay_seconds','4')}">
      </div>
      <div>
        <label for="login_wait_seconds">Login/2FA wait, seconds</label>
        <input type="number" id="login_wait_seconds" name="login_wait_seconds" min="60" value="{val('login_wait_seconds','900')}">
      </div>
      <div>
        <label for="max_scrolls">Max scroll passes per page</label>
        <input type="number" id="max_scrolls" name="max_scrolls" min="3" value="{val('max_scrolls','18')}">
      </div>
      <div>
        <label for="scroll_pause_seconds">Scroll pause, seconds</label>
        <input type="number" id="scroll_pause_seconds" name="scroll_pause_seconds" min="1" value="{val('scroll_pause_seconds','2')}">
      </div>
    </div>

    <div class="row">
      <label for="csv_text">Paste CSV / name+URL rows</label>
      <textarea id="csv_text" name="csv_text" style="min-height:150px" placeholder="person_name,linkedin_url,notes&#10;Stephen Abbott,https://www.linkedin.com/in/stephen-e-abbott/,sample">{html.escape(str(form.get('csv_text','') or ''))}</textarea>
    </div>

    <div class="row">
      <label><input type="checkbox" name="capture_detail_pages" value="1" {'checked' if _truthy(form.get('capture_detail_pages', '1')) else ''}> Capture Experience, Education, Volunteering, and Licenses & certifications detail pages</label>
      <label><input type="checkbox" name="save_screenshots" value="1" {'checked' if _truthy(form.get('save_screenshots', '1')) else ''}> Save screenshots as well as HTML</label>
      <label><input type="checkbox" name="headless" value="1" {'checked' if _truthy(form.get('headless', '0')) else ''}> Run headless; not recommended because login/2FA usually requires a visible browser</label>
    </div>

    <div class="notice">
      <b>Output location:</b> <code>data/linkedin_evidence_capture_v1/runs/&lt;timestamp&gt;/</code><br>
      This module keeps a separate browser profile at <code>data/linkedin_evidence_capture_v1/browser_profile/</code> so it does not mix with the earlier LinkedIn collector.
    </div>
    """


def is_logged_in(context, page) -> bool:
    try:
        cookies = context.cookies("https://www.linkedin.com")
        cookie_names = {c.get("name") for c in cookies}
        if "li_at" in cookie_names or "JSESSIONID" in cookie_names:
            # A JSESSIONID alone can exist before auth, so also check the page if possible.
            if "login" not in (page.url or "").lower() and "checkpoint" not in (page.url or "").lower():
                return True
    except Exception:
        pass
    try:
        url = (page.url or "").lower()
        if "/feed" in url or "/in/" in url or "/mynetwork" in url:
            body_text = page.locator("body").inner_text(timeout=3000).lower()
            login_markers = ["sign in", "join linkedin", "email or phone", "password", "security verification"]
            if not any(marker in body_text for marker in login_markers):
                return True
    except Exception:
        pass
    return False


def wait_for_login(context, page, wait_seconds: int) -> None:
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)
    start = time.time()
    while time.time() - start < wait_seconds:
        if is_logged_in(context, page):
            return
        time.sleep(2)
    raise TimeoutError(
        "Timed out waiting for LinkedIn login. Complete login/2FA in the browser window, then rerun or increase Login/2FA wait seconds."
    )


def dismiss_obvious_popups(page) -> None:
    # Best-effort only. Avoid anything destructive; mostly cookie/chat overlays.
    labels = [
        "Dismiss",
        "Close",
        "Got it",
        "Not now",
        "Skip",
        "No thanks",
        "Accept",
        "Accept cookies",
    ]
    for label in labels:
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if btn.count() > 0:
                btn.first.click(timeout=1000)
                time.sleep(0.5)
        except Exception:
            pass


def scroll_to_bottom(page, max_scrolls: int, pause_seconds: float) -> Dict[str, Any]:
    """Scroll in passes until height stops increasing. Returns basic scroll stats."""
    last_height = 0
    stable_count = 0
    scrolls = 0
    for i in range(max_scrolls):
        scrolls = i + 1
        try:
            current_height = page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
            page.evaluate("() => window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight))")
            time.sleep(pause_seconds)
            new_height = page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
            if int(new_height) <= int(current_height) and int(new_height) <= int(last_height or 0):
                stable_count += 1
            else:
                stable_count = 0
            last_height = new_height
            if stable_count >= 2:
                break
        except Exception:
            time.sleep(pause_seconds)
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
        time.sleep(0.8)
    except Exception:
        pass
    return {"scrolls": scrolls, "last_height": last_height}


def save_capture(page, target_dir: Path, section: str, save_screenshot: bool) -> Tuple[Path, Optional[Path], Dict[str, Any]]:
    target_dir.mkdir(parents=True, exist_ok=True)
    html_path = target_dir / f"{section}.html"
    png_path = target_dir / f"{section}.png"
    meta_path = target_dir / f"{section}.json"

    content = page.content()
    html_path.write_text(content, encoding="utf-8", errors="replace")

    screenshot_path: Optional[Path] = None
    if save_screenshot:
        try:
            page.screenshot(path=str(png_path), full_page=True, timeout=60000)
            screenshot_path = png_path
        except Exception as exc:
            # Keep HTML even if screenshot fails.
            screenshot_path = None
            (target_dir / f"{section}_screenshot_error.txt").write_text(str(exc), encoding="utf-8", errors="replace")

    meta = {
        "section": section,
        "url": page.url,
        "title": "",
        "captured_at": _now(),
        "html_path": _short_path(html_path),
        "screenshot_path": _short_path(screenshot_path) if screenshot_path else "",
    }
    try:
        meta["title"] = page.title(timeout=3000)
    except Exception:
        pass
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return html_path, screenshot_path, meta


def capture_one_section(page, url: str, target_dir: Path, section: str, max_scrolls: int, scroll_pause: float, delay_seconds: float, save_screenshot: bool) -> Tuple[str, str, str]:
    """Return status, html_path, screenshot_path for a single page/section."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(delay_seconds)
        dismiss_obvious_popups(page)
        scroll_to_bottom(page, max_scrolls=max_scrolls, pause_seconds=scroll_pause)
        html_path, screenshot_path, _meta = save_capture(page, target_dir, section, save_screenshot)
        return "ok", _short_path(html_path), _short_path(screenshot_path)
    except Exception as exc:
        err_path = target_dir / f"{section}_error.txt"
        err_path.write_text(str(exc), encoding="utf-8", errors="replace")
        return "error", "", ""


def build_row(target_id: int, target: Target, status: str, paths: Dict[str, Dict[str, str]], error_message: str, collected_at: str) -> List[str]:
    return [
        str(target_id),
        target.input_name,
        target.linkedin_url,
        status,
        paths.get("main", {}).get("html", ""),
        paths.get("main", {}).get("screenshot", ""),
        paths.get("experience", {}).get("html", ""),
        paths.get("experience", {}).get("screenshot", ""),
        paths.get("education", {}).get("html", ""),
        paths.get("education", {}).get("screenshot", ""),
        paths.get("volunteering", {}).get("html", ""),
        paths.get("volunteering", {}).get("screenshot", ""),
        paths.get("certifications", {}).get("html", ""),
        paths.get("certifications", {}).get("screenshot", ""),
        error_message,
        collected_at,
    ]


def save_row_to_db(run_id: str, row: List[str]) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO evidence_captures (
                run_id, target_id, input_name, linkedin_url, status,
                main_html, main_screenshot,
                experience_html, experience_screenshot,
                education_html, education_screenshot,
                volunteering_html, volunteering_screenshot,
                certifications_html, certifications_screenshot,
                error_message, collected_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [run_id] + row,
        )


def write_manifest(run_dir: Path, rows: List[List[str]]) -> Path:
    manifest = run_dir / "manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)
        writer.writerows(rows)
    return manifest


def run(form: Dict[str, Any]) -> Tuple[List[str], List[List[str]]]:
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed. Run: python -m pip install playwright && python -m playwright install chromium")

    init_storage()
    csv_text = read_uploaded_csv(form)
    targets = parse_targets(csv_text)
    max_profiles = _safe_int(form.get("max_profiles", "25"), 25)
    targets = targets[:max_profiles]
    delay_seconds = _safe_float(form.get("delay_seconds", "4"), 4.0)
    login_wait_seconds = _safe_int(form.get("login_wait_seconds", "900"), 900)
    max_scrolls = _safe_int(form.get("max_scrolls", "18"), 18)
    scroll_pause = _safe_float(form.get("scroll_pause_seconds", "2"), 2.0)
    capture_detail_pages = _truthy(form.get("capture_detail_pages", "1"))
    save_screenshots = _truthy(form.get("save_screenshots", "1"))
    headless = _truthy(form.get("headless", "0"))

    run_id = _run_id()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    rows: List[List[str]] = []

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO evidence_runs (run_id, created_at, source_label, target_count, notes) VALUES (?,?,?,?,?)",
            (run_id, _now(), "CSV/text input", len(targets), "LinkedIn Evidence Capture v1"),
        )

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=headless,
            viewport={"width": 1400, "height": 950},
            accept_downloads=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(20000)

        wait_for_login(context, page, login_wait_seconds)

        for idx, target in enumerate(targets, start=1):
            collected_at = _now()
            safe_name = _slug(target.input_name or _profile_slug_from_url(target.linkedin_url))
            target_dir = run_dir / f"{idx:03d}_{safe_name}"
            paths: Dict[str, Dict[str, str]] = {}
            errors: List[str] = []

            sections = [("main", "")]
            if capture_detail_pages:
                sections += [(name, slug) for name, slug in SECTION_SPECS if name != "main"]

            for section_name, section_slug in sections:
                url = detail_url(target.linkedin_url, section_slug)
                status, html_path, screenshot_path = capture_one_section(
                    page=page,
                    url=url,
                    target_dir=target_dir,
                    section=section_name,
                    max_scrolls=max_scrolls,
                    scroll_pause=scroll_pause,
                    delay_seconds=delay_seconds,
                    save_screenshot=save_screenshots,
                )
                paths[section_name] = {"html": html_path, "screenshot": screenshot_path}
                if status != "ok":
                    errors.append(f"{section_name}: capture failed")
                # Small pause between section pages.
                time.sleep(max(1.0, delay_seconds / 2.0))

            status = "ok" if not errors else "partial"
            row = build_row(idx, target, status, paths, "; ".join(errors), collected_at)
            rows.append(row)
            save_row_to_db(run_id, row)

    write_manifest(run_dir, rows)
    return HEADERS, rows


def export_rows(form: Dict[str, Any]) -> Iterable[List[str]]:
    # For export, rerun collection rather than exporting old DB rows, matching the app's existing plugin pattern.
    headers, rows = run(form)
    yield headers
    for row in rows:
        yield row
