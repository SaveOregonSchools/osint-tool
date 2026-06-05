"""
LinkedIn Profile Collector v1.2

Drop-in query plugin for Jeff's Flask query console.

Purpose:
- Accept a CSV of already-identified LinkedIn profile URLs.
- Launch a visible Playwright browser using a persistent local browser profile.
- Let the user manually log in and complete any 2FA/challenges.
- Visit each supplied profile URL slowly.
- Save visible profile text, best-effort structured fields, screenshots, and HTML snapshots.

Important boundaries:
- This does not store your LinkedIn password.
- This does not bypass CAPTCHA, 2FA, login challenges, rate limits, or access controls.
- It only extracts information visible to the logged-in browser session you control.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except Exception:  # pragma: no cover - lets plugin load even before dependency installed
    sync_playwright = None
    PlaywrightTimeoutError = Exception


META = {
    "key": "linkedin_profile_collector_v1",
    "name": "LinkedIn Profile Collector v1.2",
    "description": (
        "Browser-assisted LinkedIn profile collector. Upload or paste a CSV containing "
        "person_name and linkedin_url columns. The module opens a visible browser so you can "
        "log in and complete any 2FA/challenge manually, then it collects visible profile fields."
    ),
}

HEADERS = [
    "target_id",
    "input_name",
    "linkedin_url",
    "status",
    "profile_name",
    "headline",
    "location",
    "current_title",
    "current_company",
    "experience_preview",
    "education_preview",
    "screenshot_path",
    "html_path",
    "error_message",
    "collected_at",
]

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "linkedin_profiles.db"
SNAPSHOT_DIR = DATA_DIR / "linkedin_snapshots"
EXPORT_DIR = DATA_DIR / "linkedin_exports"
USER_DATA_DIR = DATA_DIR / "linkedin_browser_profile"


@dataclass
class Target:
    input_name: str
    linkedin_url: str
    notes: str = ""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _slug(text: str, max_len: int = 80) -> str:
    text = re.sub(r"https?://", "", text or "")
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return (text[:max_len] or "profile")


def _safe_int(value: str, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS linkedin_profile_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source_label TEXT,
                status TEXT,
                notes TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS linkedin_profile_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                person_name_input TEXT,
                linkedin_url TEXT NOT NULL,
                notes TEXT,
                status TEXT,
                error_message TEXT,
                collected_at TEXT,
                FOREIGN KEY(job_id) REFERENCES linkedin_profile_jobs(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS linkedin_profile_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                profile_name_visible TEXT,
                headline TEXT,
                location TEXT,
                current_title TEXT,
                current_company TEXT,
                about_summary TEXT,
                experience_json TEXT,
                education_json TEXT,
                certifications_json TEXT,
                volunteer_json TEXT,
                full_text TEXT,
                screenshot_path TEXT,
                html_snapshot_path TEXT,
                collected_at TEXT,
                FOREIGN KEY(target_id) REFERENCES linkedin_profile_targets(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_linkedin_targets_job ON linkedin_profile_targets(job_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_linkedin_results_target ON linkedin_profile_results(target_id)")


def render_fields(form: Dict[str, str]) -> str:
    # The app.py patch included in this package enables multipart file upload.
    # CSV paste is kept as a fallback and is also useful for quick tests.
    return f"""
    <style>
      .linkedin-help {{ background:#f7f7f7; border:1px solid #ddd; border-radius:6px; padding:10px; margin:10px 0; }}
      .linkedin-grid {{ display:grid; grid-template-columns: 220px minmax(300px, 1fr); gap:8px 12px; align-items:center; }}
      .linkedin-grid input, .linkedin-grid textarea {{ width: 100%; }}
      .linkedin-note {{ color:#555; font-size: 13px; }}
    </style>
    <div class="linkedin-help">
      <b>CSV columns:</b> <code>person_name</code> and <code>linkedin_url</code> are preferred. Also accepted: <code>name</code>, <code>profile_url</code>, <code>url</code>, <code>notes</code>.<br>
      <b>Login/2FA:</b> a visible browser will open. Log in manually and complete any challenge in that browser. The collector will not start profiles until LinkedIn creates a normal logged-in session cookie.
    </div>

    <div class="linkedin-grid">
      <label><b>Upload CSV</b></label>
      <input type="file" name="linkedin_csv_file" accept=".csv,text/csv">

      <label><b>Or paste CSV</b></label>
      <textarea name="csv_text" rows="8" placeholder="person_name,linkedin_url,notes\nJane Smith,https://www.linkedin.com/in/example/,PFL lead">{html.escape(form.get('csv_text',''))}</textarea>

      <label>Source label</label>
      <input name="source_label" value="{html.escape(form.get('source_label','LinkedIn profile batch'))}">

      <label>Max profiles this run</label>
      <input type="number" name="max_profiles" value="{html.escape(form.get('max_profiles','25'))}" min="1" max="500">

      <label>Delay between profiles, seconds</label>
      <input type="number" name="delay_seconds" value="{html.escape(form.get('delay_seconds','8'))}" min="3" max="120">

      <label>Login wait, minutes</label>
      <input type="number" name="login_wait_minutes" value="{html.escape(form.get('login_wait_minutes','15'))}" min="1" max="60">

      <label>Headless browser?</label>
      <select name="headless">
        <option value="false" {'selected' if form.get('headless','false') == 'false' else ''}>No - visible browser</option>
        <option value="true" {'selected' if form.get('headless') == 'true' else ''}>Yes - not recommended</option>
      </select>

      <label>Save HTML snapshots?</label>
      <select name="save_html">
        <option value="true" {'selected' if form.get('save_html','true') == 'true' else ''}>Yes</option>
        <option value="false" {'selected' if form.get('save_html') == 'false' else ''}>No</option>
      </select>

      <label>Save screenshots?</label>
      <select name="save_screenshots">
        <option value="true" {'selected' if form.get('save_screenshots','true') == 'true' else ''}>Yes</option>
        <option value="false" {'selected' if form.get('save_screenshots') == 'false' else ''}>No</option>
      </select>
    </div>
    <p class="linkedin-note">
      First run may require: <code>pip install playwright</code> then <code>python -m playwright install chromium</code>.
      The persistent browser profile is stored under <code>data/linkedin_browser_profile</code> so you usually do not need to log in every time.
    </p>
    """


def _csv_text_from_form(form: Dict[str, str]) -> str:
    # Patched app.py adds uploaded CSV text here. Fallback to pasted CSV.
    uploaded = form.get("_uploaded_linkedin_csv_file_text", "") or form.get("_uploaded_file_text", "")
    return uploaded or form.get("csv_text", "") or ""


def parse_targets(csv_text: str) -> List[Target]:
    """Parse LinkedIn targets from either:
    1) a normal CSV with headers, e.g. person_name,linkedin_url,notes
    2) a simple two-column paste with no header, e.g. Jane Doe,https://www.linkedin.com/in/jane-doe/
    3) a one-column list of LinkedIn URLs
    """
    csv_text = (csv_text or "").strip("\ufeff\n\r ")
    if not csv_text:
        raise ValueError("No CSV data found. Upload a CSV or paste CSV rows.")

    sample = csv_text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except Exception:
        dialect = csv.excel

    def clean(value: object) -> str:
        return str(value or "").strip().strip('"').strip("'")

    def is_linkedin(value: str) -> bool:
        return "linkedin.com" in (value or "").lower()

    # First read all rows as lists so we can decide whether the first row is a header.
    raw_rows = [row for row in csv.reader(io.StringIO(csv_text), dialect=dialect) if any(clean(c) for c in row)]
    if not raw_rows:
        raise ValueError("CSV appears to contain no usable rows.")

    first = [clean(c) for c in raw_rows[0]]
    normalized_first = [re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_") for c in first]
    known_header_names = {
        "person_name", "name", "person", "full_name",
        "linkedin_url", "profile_url", "url", "link", "hyperlink",
        "notes", "note", "context",
    }
    first_row_has_linkedin = any(is_linkedin(c) for c in first)
    looks_like_header = (not first_row_has_linkedin) and any(c in known_header_names for c in normalized_first)

    targets: List[Target] = []
    seen = set()

    if looks_like_header:
        reader = csv.DictReader(io.StringIO(csv_text), dialect=dialect)
        field_map = {re.sub(r"[^a-z0-9]+", "_", f.lower()).strip("_"): f for f in (reader.fieldnames or [])}

        name_col = field_map.get("person_name") or field_map.get("name") or field_map.get("person") or field_map.get("full_name")
        url_col = field_map.get("linkedin_url") or field_map.get("profile_url") or field_map.get("url") or field_map.get("link") or field_map.get("hyperlink")
        notes_col = field_map.get("notes") or field_map.get("note") or field_map.get("context")

        if not url_col:
            raise ValueError(f"Could not find a LinkedIn URL column. Headers found: {reader.fieldnames}")

        for row in reader:
            url = clean(row.get(url_col))
            if not is_linkedin(url) or url in seen:
                continue
            seen.add(url)
            targets.append(Target(
                input_name=clean(row.get(name_col)) if name_col else "",
                linkedin_url=url,
                notes=clean(row.get(notes_col)) if notes_col else "",
            ))
    else:
        # Headerless mode. Accept rows like:
        #   Name, URL
        #   URL
        #   Name, URL, Notes
        for row in raw_rows:
            cells = [clean(c) for c in row]
            url_idx = next((i for i, c in enumerate(cells) if is_linkedin(c)), None)
            if url_idx is None:
                continue
            url = cells[url_idx]
            if url in seen:
                continue
            seen.add(url)
            name = cells[0] if url_idx != 0 and len(cells) > 0 else ""
            notes = ""
            if len(cells) > url_idx + 1:
                notes = ", ".join(c for c in cells[url_idx + 1:] if c)
            targets.append(Target(input_name=name, linkedin_url=url, notes=notes))

    if not targets:
        raise ValueError("CSV parsed successfully, but no LinkedIn URLs were found.")
    return targets

def _create_job(targets: List[Target], source_label: str) -> int:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO linkedin_profile_jobs(created_at, source_label, status, notes) VALUES (?,?,?,?)",
            (_now(), source_label, "running", f"{len(targets)} targets queued"),
        )
        job_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO linkedin_profile_targets(job_id, person_name_input, linkedin_url, notes, status)
            VALUES (?,?,?,?,?)
            """,
            [(job_id, t.input_name, t.linkedin_url, t.notes, "queued") for t in targets],
        )
    return job_id


def _target_rows(job_id: int, limit: Optional[int] = None) -> List[Tuple]:
    sql = "SELECT id, person_name_input, linkedin_url, notes FROM linkedin_profile_targets WHERE job_id=? ORDER BY id"
    args: List[object] = [job_id]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(sql, args).fetchall()


def _extract_sections(page) -> Dict[str, str]:
    """Return best-effort text for LinkedIn profile sections."""
    sections = page.locator("section").all()
    out: Dict[str, str] = {}
    for sec in sections:
        try:
            txt = sec.inner_text(timeout=1500).strip()
        except Exception:
            continue
        low = txt.lower()
        # LinkedIn often includes the section title as the first line.
        for key in ["about", "experience", "education", "licenses", "certifications", "volunteering", "volunteer"]:
            if key in low[:200]:
                canonical = "certifications" if key in ("licenses", "certifications") else "volunteer" if key in ("volunteering", "volunteer") else key
                if canonical not in out or len(txt) > len(out[canonical]):
                    out[canonical] = txt
    return out


def _lines(text: str) -> List[str]:
    return [x.strip() for x in re.split(r"[\r\n]+", text or "") if x.strip()]


def _section_to_items(section_text: str, max_items: int = 12) -> List[Dict[str, str]]:
    """
    LinkedIn markup changes often. This parser intentionally keeps it conservative:
    it stores raw lines and makes a best-effort title/org/date grouping.
    """
    lines = _lines(section_text)
    if lines and lines[0].lower() in {"experience", "education", "about", "licenses & certifications", "volunteering"}:
        lines = lines[1:]
    cleaned: List[str] = []
    skip_patterns = [
        r"^show all ", r"^show less", r"^see more", r"^see all", r"^opens profile",
        r"^skills:", r"^endorsed", r"^\d+ endorsements?$",
    ]
    for line in lines:
        low = line.lower()
        if any(re.search(p, low) for p in skip_patterns):
            continue
        if line not in cleaned:
            cleaned.append(line)
    items: List[Dict[str, str]] = []
    i = 0
    while i < len(cleaned) and len(items) < max_items:
        title = cleaned[i]
        org = cleaned[i + 1] if i + 1 < len(cleaned) else ""
        dates = ""
        location = ""
        # Look ahead for date-looking and location-looking lines.
        for j in range(i + 2, min(i + 7, len(cleaned))):
            if not dates and re.search(r"\b(19|20)\d{2}\b|present|mos?|yrs?|years?", cleaned[j], flags=re.I):
                dates = cleaned[j]
            elif not location and re.search(r"area|united states|oregon|washington|california|remote|hybrid|on-site", cleaned[j], flags=re.I):
                location = cleaned[j]
        items.append({"title": title, "organization": org, "dates": dates, "location": location})
        i += 4 if dates else 3
    return items


def _extract_profile(page) -> Dict[str, object]:
    # Make lazy-loaded sections more likely to appear.
    for y in [600, 1200, 1800, 2400, 3200, 4200]:
        try:
            page.mouse.wheel(0, y)
            page.wait_for_timeout(500)
        except Exception:
            pass
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    page.wait_for_timeout(750)

    full_text = ""
    try:
        full_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        pass

    profile_name = ""
    headline = ""
    location = ""
    for selector in ["h1", "main h1"]:
        try:
            profile_name = page.locator(selector).first.inner_text(timeout=2500).strip()
            if profile_name:
                break
        except Exception:
            pass

    # Best effort top card extraction from visible lines.
    body_lines = _lines(full_text)
    if profile_name and profile_name in body_lines:
        idx = body_lines.index(profile_name)
        top_lines = body_lines[idx + 1: idx + 12]
        # First substantial line after the name is usually headline.
        for line in top_lines:
            if line.lower() in {"1st", "2nd", "3rd", "message", "connect", "follow", "more"}:
                continue
            if len(line) > 3 and not re.search(r"connections?|followers?", line, flags=re.I):
                headline = line
                break
        for line in top_lines:
            if re.search(r"area|oregon|washington|california|united states|remote|portland|seattle|new york|washington dc", line, flags=re.I):
                location = line
                break

    sections = _extract_sections(page)
    about_summary = "\n".join(_lines(sections.get("about", ""))[1:]).strip() if sections.get("about") else ""
    experience_items = _section_to_items(sections.get("experience", ""), max_items=20)
    education_items = _section_to_items(sections.get("education", ""), max_items=12)
    certification_items = _section_to_items(sections.get("certifications", ""), max_items=12)
    volunteer_items = _section_to_items(sections.get("volunteer", ""), max_items=12)

    current_title = ""
    current_company = ""
    if experience_items:
        current_title = experience_items[0].get("title", "")
        current_company = experience_items[0].get("organization", "")

    return {
        "profile_name_visible": profile_name,
        "headline": headline,
        "location": location,
        "current_title": current_title,
        "current_company": current_company,
        "about_summary": about_summary,
        "experience": experience_items,
        "education": education_items,
        "certifications": certification_items,
        "volunteer": volunteer_items,
        "full_text": full_text,
    }


def _has_linkedin_session_cookie(page) -> bool:
    """Return True only when the browser has a normal LinkedIn logged-in session cookie.

    This is intentionally stricter than looking at page text. LinkedIn login/authwall pages
    can contain profile URLs and generic navigation text that made earlier versions resume
    too soon while the user was still logging in.
    """
    try:
        cookies = page.context.cookies(["https://www.linkedin.com"])
    except Exception:
        return False
    return any(c.get("name") == "li_at" and c.get("value") for c in cookies)


def _page_looks_logged_in(page) -> bool:
    url = (page.url or "").lower()
    try:
        body = page.locator("body").inner_text(timeout=2500).lower()
    except Exception:
        body = ""
    login_wall_terms = [
        "sign in", "join linkedin", "authwall", "checkpoint", "security verification",
        "verification code", "two-step", "two factor", "2-step", "captcha",
    ]
    if any(term in url for term in ["/login", "checkpoint", "challenge", "authwall"]):
        return False
    if any(term in body[:5000] for term in login_wall_terms):
        return False
    return "start a post" in body or "messaging" in body or "notifications" in body or "/feed" in url


def _is_logged_in(page) -> bool:
    # Prefer the session cookie. Fall back to conservative page markers only because
    # some LinkedIn sessions may delay cookie visibility inside Playwright.
    return _has_linkedin_session_cookie(page) or _page_looks_logged_in(page)


def _wait_for_manual_login(page, wait_minutes: int) -> None:
    # Start at the feed/login gate before touching target profiles. The user can log in
    # and complete 2FA/challenges in the visible browser window. Collection starts only
    # after a real session is detected.
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)
    deadline = time.time() + wait_minutes * 60
    while time.time() < deadline:
        if _is_logged_in(page):
            # Give LinkedIn a moment to finish redirects after 2FA before loading targets.
            page.wait_for_timeout(1500)
            return
        time.sleep(3)
    raise TimeoutError(
        f"Timed out after {wait_minutes} minutes waiting for LinkedIn login. "
        "Run again and complete the login/2FA challenge in the browser window, or increase Login wait minutes."
    )


def _mark_target(target_id: int, status: str, error: str = "") -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE linkedin_profile_targets SET status=?, error_message=?, collected_at=? WHERE id=?",
            (status, error, _now(), target_id),
        )


def _save_result(target_id: int, profile: Dict[str, object], screenshot_path: str, html_path: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO linkedin_profile_results(
                target_id, profile_name_visible, headline, location, current_title, current_company,
                about_summary, experience_json, education_json, certifications_json, volunteer_json,
                full_text, screenshot_path, html_snapshot_path, collected_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                target_id,
                profile.get("profile_name_visible", ""),
                profile.get("headline", ""),
                profile.get("location", ""),
                profile.get("current_title", ""),
                profile.get("current_company", ""),
                profile.get("about_summary", ""),
                json.dumps(profile.get("experience", []), ensure_ascii=False),
                json.dumps(profile.get("education", []), ensure_ascii=False),
                json.dumps(profile.get("certifications", []), ensure_ascii=False),
                json.dumps(profile.get("volunteer", []), ensure_ascii=False),
                profile.get("full_text", ""),
                screenshot_path,
                html_path,
                _now(),
            ),
        )


def collect_profiles(job_id: int, form: Dict[str, str]) -> None:
    if sync_playwright is None:
        raise RuntimeError(
            "Playwright is not installed. Install it with: pip install playwright && python -m playwright install chromium"
        )

    max_profiles = _safe_int(form.get("max_profiles", "25"), 25)
    delay_seconds = max(3, _safe_int(form.get("delay_seconds", "8"), 8))
    wait_minutes = max(1, _safe_int(form.get("login_wait_minutes", "15"), 15))
    headless = form.get("headless", "false") == "true"
    save_html = form.get("save_html", "true") == "true"
    save_screenshots = form.get("save_screenshots", "true") == "true"

    targets = _target_rows(job_id, max_profiles)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=headless,
            viewport={"width": 1400, "height": 950},
            slow_mo=75,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(15000)

        try:
            _wait_for_manual_login(page, wait_minutes)
            for target_id, input_name, linkedin_url, notes in targets:
                try:
                    _mark_target(target_id, "running")
                    page.goto(linkedin_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3500)

                    if not _is_logged_in(page):
                        _wait_for_manual_login(page, wait_minutes)
                        page.goto(linkedin_url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(3500)

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    stem = f"{target_id}_{timestamp}_{_slug(input_name or linkedin_url)}"
                    html_path = ""
                    screenshot_path = ""

                    profile = _extract_profile(page)

                    if save_html:
                        html_file = SNAPSHOT_DIR / f"{stem}.html"
                        html_file.write_text(page.content(), encoding="utf-8")
                        html_path = str(html_file)
                    if save_screenshots:
                        screenshot_file = SNAPSHOT_DIR / f"{stem}.png"
                        page.screenshot(path=str(screenshot_file), full_page=True)
                        screenshot_path = str(screenshot_file)

                    _save_result(target_id, profile, screenshot_path, html_path)
                    _mark_target(target_id, "collected")
                    time.sleep(delay_seconds)
                except Exception as e:
                    _mark_target(target_id, "error", str(e))
                    time.sleep(delay_seconds)
        finally:
            with sqlite3.connect(DB_PATH) as conn:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM linkedin_profile_targets WHERE job_id=? AND status IN ('queued','running')",
                    (job_id,),
                ).fetchone()[0]
                errors = conn.execute(
                    "SELECT COUNT(*) FROM linkedin_profile_targets WHERE job_id=? AND status='error'",
                    (job_id,),
                ).fetchone()[0]
                status = "complete_with_errors" if errors else "complete"
                if remaining:
                    status = "incomplete"
                conn.execute(
                    "UPDATE linkedin_profile_jobs SET status=?, notes=? WHERE id=?",
                    (status, f"remaining={remaining}; errors={errors}", job_id),
                )
            context.close()


def _preview_rows(job_id: int) -> List[List[str]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT
                t.id,
                t.person_name_input,
                t.linkedin_url,
                t.status,
                COALESCE(r.profile_name_visible, ''),
                COALESCE(r.headline, ''),
                COALESCE(r.location, ''),
                COALESCE(r.current_title, ''),
                COALESCE(r.current_company, ''),
                COALESCE(r.experience_json, ''),
                COALESCE(r.education_json, ''),
                COALESCE(r.screenshot_path, ''),
                COALESCE(r.html_snapshot_path, ''),
                COALESCE(t.error_message, ''),
                COALESCE(r.collected_at, t.collected_at, '')
            FROM linkedin_profile_targets t
            LEFT JOIN linkedin_profile_results r ON r.target_id = t.id
            WHERE t.job_id=?
            ORDER BY t.id
            """,
            (job_id,),
        ).fetchall()

    out: List[List[str]] = []
    for r in rows:
        r = list(r)
        # Compact JSON previews for table readability.
        for idx in [9, 10]:
            try:
                items = json.loads(r[idx] or "[]")
                r[idx] = "; ".join(
                    " - ".join([x for x in [it.get("title", ""), it.get("organization", ""), it.get("dates", "")] if x])
                    for it in items[:3]
                )
            except Exception:
                pass
        out.append([str(x or "") for x in r])
    return out


def run(form: Dict[str, str]):
    csv_text = _csv_text_from_form(form)
    targets = parse_targets(csv_text)
    max_profiles = _safe_int(form.get("max_profiles", "25"), 25)
    if max_profiles < len(targets):
        targets = targets[:max_profiles]

    source_label = form.get("source_label", "LinkedIn profile batch")
    job_id = _create_job(targets, source_label)
    collect_profiles(job_id, form)
    return HEADERS, _preview_rows(job_id)


def export_headers(form: Dict[str, str]):
    return HEADERS + ["about_summary", "experience_json", "education_json", "certifications_json", "volunteer_json", "full_text"]


def export_rows(form: Dict[str, str]) -> Iterable[List[str]]:
    init_db()
    # Export latest job unless a job_id is provided manually.
    job_id = form.get("job_id")
    with sqlite3.connect(DB_PATH) as conn:
        if not job_id:
            row = conn.execute("SELECT id FROM linkedin_profile_jobs ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return
            job_id = row[0]
        rows = conn.execute(
            """
            SELECT
                t.id,
                t.person_name_input,
                t.linkedin_url,
                t.status,
                COALESCE(r.profile_name_visible, ''),
                COALESCE(r.headline, ''),
                COALESCE(r.location, ''),
                COALESCE(r.current_title, ''),
                COALESCE(r.current_company, ''),
                COALESCE(r.experience_json, ''),
                COALESCE(r.education_json, ''),
                COALESCE(r.screenshot_path, ''),
                COALESCE(r.html_snapshot_path, ''),
                COALESCE(t.error_message, ''),
                COALESCE(r.collected_at, t.collected_at, ''),
                COALESCE(r.about_summary, ''),
                COALESCE(r.experience_json, ''),
                COALESCE(r.education_json, ''),
                COALESCE(r.certifications_json, ''),
                COALESCE(r.volunteer_json, ''),
                COALESCE(r.full_text, '')
            FROM linkedin_profile_targets t
            LEFT JOIN linkedin_profile_results r ON r.target_id = t.id
            WHERE t.job_id=?
            ORDER BY t.id
            """,
            (job_id,),
        ).fetchall()
    for row in rows:
        yield [str(x or "") for x in row]
