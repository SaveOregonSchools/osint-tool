"""
LinkedIn Profile Collector v1.3

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
from urllib.parse import urlparse, urlunparse

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except Exception:  # pragma: no cover - lets plugin load even before dependency installed
    sync_playwright = None
    PlaywrightTimeoutError = Exception


META = {
    "key": "linkedin_profile_collector_v1",
    "name": "LinkedIn Profile Collector v1.3",
    "description": (
        "Browser-assisted LinkedIn profile collector. Upload or paste a CSV containing "
        "person_name and linkedin_url columns. The module opens a visible browser so you can "
        "log in and complete any 2FA/challenge manually, then it collects visible profile fields. "
        "v1.3 uses section-specific LinkedIn detail pages for cleaner experience/education output."
    ),
    "source_type": "manual_entry",
    "limitations": [
        "Browser-assisted workflow for pages visible to a user-controlled logged-in session.",
        "Does not bypass CAPTCHA, 2FA, login challenges, privacy settings, or access controls.",
    ],
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
    "about_text",
    "experience_1_title",
    "experience_1_organization",
    "experience_1_dates",
    "experience_1_location",
    "experience_1_description",
    "experience_2_title",
    "experience_2_organization",
    "experience_2_dates",
    "experience_2_location",
    "experience_2_description",
    "education_1_school",
    "education_1_degree",
    "education_1_dates",
    "volunteer_preview",
    "certifications_preview",
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



def _lines(text: str) -> List[str]:
    return [x.strip() for x in re.split(r"[\r\n]+", text or "") if x.strip()]


NOISE_LINE_RE = re.compile(
    r"^(skip to main content|linkedin|home|my network|jobs|messaging|notifications|me|for business|"
    r"try premium|search|show all|show less|see more|see all|opens profile|open profile|"
    r"profile photo|background image|message|connect|follow|more|contact info|"
    r"people also viewed|pages people also viewed|similar profiles|sign in|join now|"
    r"experience|education|about|licenses & certifications|volunteering|volunteer experience)$",
    re.I,
)


def _clean_lines(text: str) -> List[str]:
    out: List[str] = []
    for line in _lines(text):
        line = re.sub(r"\s+", " ", line).strip()
        if not line or NOISE_LINE_RE.search(line):
            continue
        low = line.lower()
        if low.startswith("show all ") or low.startswith("see all "):
            continue
        if line not in out:
            out.append(line)
    return out


def _profile_base_url(url: str) -> str:
    """Return https://www.linkedin.com/in/<slug>/ for common profile URLs."""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or "www.linkedin.com"
    parts = [p for p in parsed.path.split("/") if p]
    if "in" in parts:
        i = parts.index("in")
        if len(parts) > i + 1:
            path = f"/in/{parts[i + 1]}/"
            return urlunparse((scheme, netloc, path, "", "", ""))
    # Fallback: strip query/fragment and ensure trailing slash.
    path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
    return urlunparse((scheme, netloc, path, "", "", ""))


def _detail_url(profile_url: str, section: str) -> str:
    return _profile_base_url(profile_url) + f"details/{section}/"


def _scroll_page(page, steps: int = 6) -> None:
    for _ in range(steps):
        try:
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(450)
        except Exception:
            pass
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    page.wait_for_timeout(500)


def _body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def _main_text(page) -> str:
    try:
        return page.locator("main").inner_text(timeout=5000)
    except Exception:
        return _body_text(page)


def _extract_top_card(page) -> Dict[str, str]:
    full_text = _body_text(page)
    profile_name = ""
    headline = ""
    location = ""
    for selector in ["main h1", "h1"]:
        try:
            profile_name = page.locator(selector).first.inner_text(timeout=2500).strip()
            if profile_name:
                break
        except Exception:
            pass

    body_lines = _clean_lines(full_text)
    if profile_name and profile_name in body_lines:
        idx = body_lines.index(profile_name)
        top_lines = body_lines[idx + 1: idx + 14]
        for line in top_lines:
            if len(line) > 3 and not re.search(r"connections?|followers?|contact info|message|connect", line, flags=re.I):
                headline = line
                break
        for line in top_lines:
            if re.search(r"area|united states|oregon|washington|california|remote|portland|seattle|new york|washington dc|maine|texas|florida|illinois", line, flags=re.I):
                location = line
                break
    return {"profile_name_visible": profile_name, "headline": headline, "location": location, "full_text": full_text}


def _extract_main_section_text(page, section_title: str) -> str:
    """Fallback for main profile page sections when detail pages are unavailable."""
    try:
        sections = page.locator("section").all()
    except Exception:
        sections = []
    best = ""
    title_low = section_title.lower()
    for sec in sections:
        try:
            txt = sec.inner_text(timeout=1500).strip()
        except Exception:
            continue
        low = txt.lower()
        if title_low in low[:250] and len(txt) > len(best):
            best = txt
    return best


def _extract_about_from_main(page) -> str:
    txt = _extract_main_section_text(page, "about")
    lines = _clean_lines(txt)
    if lines and lines[0].lower() == "about":
        lines = lines[1:]
    # Stop if another section accidentally bled in.
    stop_words = {"activity", "experience", "education", "licenses & certifications", "volunteering"}
    kept = []
    for line in lines:
        if line.lower() in stop_words:
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _candidate_card_texts(page) -> List[str]:
    """Collect likely result cards on LinkedIn details pages."""
    texts: List[str] = []
    selectors = [
        "main li.pvs-list__paged-list-item",
        "main li.artdeco-list__item",
        "main section li",
        "main li",
    ]
    for selector in selectors:
        try:
            locs = page.locator(selector).all()
        except Exception:
            continue
        for loc in locs:
            try:
                txt = loc.inner_text(timeout=1000).strip()
            except Exception:
                continue
            lines = _clean_lines(txt)
            if len(lines) < 2:
                continue
            compact = "\n".join(lines)
            if compact not in texts and 20 <= len(compact) <= 5000:
                texts.append(compact)
        if texts:
            break
    return texts


def _parse_item_card(card_text: str, kind: str) -> Dict[str, str]:
    lines = _clean_lines(card_text)
    item = {"title": "", "organization": "", "dates": "", "location": "", "description": ""}
    if not lines:
        return item
    item["title"] = lines[0]
    if len(lines) > 1:
        item["organization"] = lines[1]

    used = {0, 1}
    for i, line in enumerate(lines[2:], start=2):
        if not item["dates"] and re.search(r"\b(19|20)\d{2}\b|present|mos?|yrs?|years?|months?", line, re.I):
            item["dates"] = line
            used.add(i)
            continue
        if not item["location"] and re.search(r"area|united states|oregon|washington|california|remote|hybrid|on-site|portland|seattle|new york|maine|texas|florida|illinois", line, re.I):
            item["location"] = line
            used.add(i)
            continue

    desc_lines = []
    for i, line in enumerate(lines):
        if i in used:
            continue
        if line == item["title"] or line == item["organization"]:
            continue
        # Avoid skill/endorsement noise unless it is the only detail.
        if re.match(r"^(skills|endorsements?):", line, re.I):
            continue
        desc_lines.append(line)
    item["description"] = "\n".join(desc_lines[:12]).strip()

    if kind == "education":
        # Keep the same JSON keys internally but map title=school, organization=degree/field.
        pass
    return item


def _parse_detail_page_items(page, kind: str, max_items: int = 25) -> List[Dict[str, str]]:
    cards = _candidate_card_texts(page)
    items: List[Dict[str, str]] = []
    for card in cards:
        item = _parse_item_card(card, kind)
        if not item.get("title"):
            continue
        # Filter obvious nav/sidebar cards.
        joined = " ".join(item.values()).lower()
        if any(term in joined for term in ["people also viewed", "you might like", "recommended for you"]):
            continue
        key = (item.get("title", ""), item.get("organization", ""), item.get("dates", ""))
        if key not in [(x.get("title"), x.get("organization"), x.get("dates")) for x in items]:
            items.append(item)
        if len(items) >= max_items:
            break
    return items


def _visit_detail_and_parse(page, profile_url: str, section: str, kind: str, max_items: int) -> List[Dict[str, str]]:
    url = _detail_url(profile_url, section)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        _scroll_page(page, steps=7)
        return _parse_detail_page_items(page, kind=kind, max_items=max_items)
    except Exception:
        return []


def _extract_profile(page, linkedin_url: str) -> Dict[str, object]:
    """Extract a profile using the main page plus LinkedIn detail section pages.

    v1.3 intentionally ignores connection degree and follower/connection counts. Those are
    noisy for this project and not worth collecting.
    """
    # Main profile page for identity/top-card/about/evidence.
    page.goto(linkedin_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    _scroll_page(page, steps=6)
    top = _extract_top_card(page)
    about_summary = _extract_about_from_main(page)
    main_full_text = top.get("full_text", "")

    # Detail pages are usually much cleaner than the main profile page.
    experience_items = _visit_detail_and_parse(page, linkedin_url, "experience", "experience", 30)
    education_items = _visit_detail_and_parse(page, linkedin_url, "education", "education", 20)
    certification_items = _visit_detail_and_parse(page, linkedin_url, "certifications", "certifications", 20)
    volunteer_items = _visit_detail_and_parse(page, linkedin_url, "volunteering-experiences", "volunteer", 20)

    # Fallback to main-page section parsing if detail pages produced nothing.
    if not experience_items:
        page.goto(linkedin_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        _scroll_page(page, steps=6)
        exp_text = _extract_main_section_text(page, "experience")
        experience_items = [_parse_item_card("\n".join(_clean_lines(exp_text)[i:i+6]), "experience") for i in range(1, len(_clean_lines(exp_text)), 6)][:10]
        experience_items = [x for x in experience_items if x.get("title")]
    if not education_items:
        edu_text = _extract_main_section_text(page, "education")
        education_items = [_parse_item_card("\n".join(_clean_lines(edu_text)[i:i+5]), "education") for i in range(1, len(_clean_lines(edu_text)), 5)][:10]
        education_items = [x for x in education_items if x.get("title")]

    current_title = ""
    current_company = ""
    if experience_items:
        current_title = experience_items[0].get("title", "")
        current_company = experience_items[0].get("organization", "")

    return {
        "profile_name_visible": top.get("profile_name_visible", ""),
        "headline": top.get("headline", ""),
        "location": top.get("location", ""),
        "current_title": current_title,
        "current_company": current_company,
        "about_summary": about_summary,
        "experience": experience_items,
        "education": education_items,
        "certifications": certification_items,
        "volunteer": volunteer_items,
        "full_text": main_full_text,
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

                    profile = _extract_profile(page, linkedin_url)

                    # Return to the main profile page before saving evidence files.
                    # The parser may have visited /details/experience/, /details/education/, etc.
                    page.goto(linkedin_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2000)

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



def _item_value(items: List[Dict[str, str]], index: int, key: str) -> str:
    try:
        return str(items[index].get(key, "") or "")
    except Exception:
        return ""


def _items_preview(json_text: str, limit: int = 2, education: bool = False) -> str:
    try:
        items = json.loads(json_text or "[]")
    except Exception:
        return ""
    parts = []
    for it in items[:limit]:
        if education:
            pieces = [it.get("title", ""), it.get("organization", ""), it.get("dates", "")]
        else:
            pieces = [it.get("title", ""), it.get("organization", ""), it.get("dates", "")]
        parts.append(" - ".join([x for x in pieces if x]))
    return "; ".join(parts)


def _flatten_result_row(row: Tuple) -> List[str]:
    (
        target_id, input_name, linkedin_url, status, profile_name, headline, location,
        current_title, current_company, about_summary, experience_json, education_json,
        certifications_json, volunteer_json, screenshot_path, html_path, error_message, collected_at
    ) = row
    try:
        exp = json.loads(experience_json or "[]")
    except Exception:
        exp = []
    try:
        edu = json.loads(education_json or "[]")
    except Exception:
        edu = []
    return [
        str(target_id or ""), str(input_name or ""), str(linkedin_url or ""), str(status or ""),
        str(profile_name or ""), str(headline or ""), str(location or ""),
        str(current_title or ""), str(current_company or ""), str(about_summary or ""),
        _item_value(exp, 0, "title"), _item_value(exp, 0, "organization"), _item_value(exp, 0, "dates"), _item_value(exp, 0, "location"), _item_value(exp, 0, "description"),
        _item_value(exp, 1, "title"), _item_value(exp, 1, "organization"), _item_value(exp, 1, "dates"), _item_value(exp, 1, "location"), _item_value(exp, 1, "description"),
        _item_value(edu, 0, "title"), _item_value(edu, 0, "organization"), _item_value(edu, 0, "dates"),
        _items_preview(volunteer_json, limit=2), _items_preview(certifications_json, limit=2),
        str(screenshot_path or ""), str(html_path or ""), str(error_message or ""), str(collected_at or ""),
    ]

def _result_select_sql() -> str:
    return """
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
                COALESCE(r.about_summary, ''),
                COALESCE(r.experience_json, ''),
                COALESCE(r.education_json, ''),
                COALESCE(r.certifications_json, ''),
                COALESCE(r.volunteer_json, ''),
                COALESCE(r.screenshot_path, ''),
                COALESCE(r.html_snapshot_path, ''),
                COALESCE(t.error_message, ''),
                COALESCE(r.collected_at, t.collected_at, '')
            FROM linkedin_profile_targets t
            LEFT JOIN linkedin_profile_results r ON r.target_id = t.id
            WHERE t.job_id=?
            ORDER BY t.id
            """


def _preview_rows(job_id: int) -> List[List[str]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(_result_select_sql(), (job_id,)).fetchall()
    return [_flatten_result_row(tuple(r)) for r in rows]


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
    return HEADERS + [
        "experience_json",
        "education_json",
        "certifications_json",
        "volunteer_json",
        "raw_profile_text",
    ]


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
                COALESCE(r.about_summary, ''),
                COALESCE(r.experience_json, ''),
                COALESCE(r.education_json, ''),
                COALESCE(r.certifications_json, ''),
                COALESCE(r.volunteer_json, ''),
                COALESCE(r.screenshot_path, ''),
                COALESCE(r.html_snapshot_path, ''),
                COALESCE(t.error_message, ''),
                COALESCE(r.collected_at, t.collected_at, ''),
                COALESCE(r.full_text, '')
            FROM linkedin_profile_targets t
            LEFT JOIN linkedin_profile_results r ON r.target_id = t.id
            WHERE t.job_id=?
            ORDER BY t.id
            """,
            (job_id,),
        ).fetchall()
    for row in rows:
        base_row = _flatten_result_row(tuple(row[:18]))
        experience_json = row[10] or ""
        education_json = row[11] or ""
        certifications_json = row[12] or ""
        volunteer_json = row[13] or ""
        raw_profile_text = row[18] or ""
        yield base_row + [
            str(experience_json),
            str(education_json),
            str(certifications_json),
            str(volunteer_json),
            str(raw_profile_text),
        ]
