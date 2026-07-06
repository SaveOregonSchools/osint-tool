"""
LinkedIn Evidence Capture v1.9

Drop-in query plugin for the Flask Social OSINT query console.

Purpose:
- Accept a CSV/pasted list of already-identified LinkedIn profile URLs.
- Launch a visible Playwright browser using a persistent local browser profile.
- Let the user manually log in and complete any 2FA/challenges.
- For each supplied profile, capture HTML + screenshots for:
  - main profile page after expanding only the About-section “more” text, with optional scrolling
  - optional profile headshot image, when one exists on the main profile page
  - selected detail pages, classified from the detail page itself; empty sections are recorded as JSON-only metadata
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
import os
import platform
import re
import sqlite3
import subprocess
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
    "name": "LinkedIn Evidence Capture v1.9 - section-aware detail capture + optional profile photos",
    "description": (
        "Browser-assisted LinkedIn evidence capture. Upload or paste CSV rows with names and "
        "LinkedIn URLs. The module opens a visible browser so you can log in and complete any "
        "2FA/challenge manually. It then captures the main profile page and the detail "
        "pages for Experience, Education, Volunteering, and Licenses & certifications. Output is "
        "stored separately under data/linkedin_evidence_capture_v1/."
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
    "main_html",
    "main_screenshot",
    "profile_photo",
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


def _checkbox(form: Dict[str, Any], name: str, default: bool = True) -> bool:
    """Return a boolean option submitted by render_fields().

    v1.9 avoids relying on normal checkbox submission, because unchecked HTML
    checkboxes are omitted from request.form and the surrounding app flattens
    form values. Each visible checkbox is paired with a hidden field using the
    real option name. JavaScript updates that hidden value to 1 or 0, and run()
    reads only the hidden value. A fallback reads older checkbox names so older
    saved forms still work.
    """
    if name in form:
        return _truthy(form.get(name, ""))
    if f"{name}__checkbox" in form:
        return _truthy(form.get(f"{name}__checkbox", ""))
    if f"{name}__present" in form:
        return _truthy(form.get(name, ""))
    return default


def _checkbox_input(form: Dict[str, Any], name: str, label: str, default: bool = True) -> str:
    current = _checkbox(form, name, default)
    checked = "checked" if current else ""
    hidden_value = "1" if current else "0"
    safe_name = html.escape(name, quote=True)
    safe_id = html.escape(f"{name}_value", quote=True)
    safe_label = label
    return (
        f'<input type="hidden" id="{safe_id}" name="{safe_name}" value="{hidden_value}">'
        f'<label><input type="checkbox" name="{safe_name}__checkbox" value="1" {checked} '
        f'onchange="document.getElementById(\'{safe_id}\').value=this.checked?\'1\':\'0\'"> {safe_label}</label>'
    )


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
                profile_photo TEXT,
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
        # Existing local databases from earlier LEC builds may not have this column.
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(evidence_captures)")}
            if "profile_photo" not in cols:
                conn.execute("ALTER TABLE evidence_captures ADD COLUMN profile_photo TEXT")
        except Exception:
            pass


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

    <div class="row" style="border:1px solid #ddd; border-radius:8px; padding:10px; background:#fbfbfb;">
      <label style="margin-bottom:8px;"><b>Detailed Captures</b></label>
      <div class="grid">
        {_checkbox_input(form, 'capture_experience', 'Experience', True)}
        {_checkbox_input(form, 'capture_education', 'Education', True)}
        {_checkbox_input(form, 'capture_volunteering', 'Volunteering', True)}
        {_checkbox_input(form, 'capture_certifications', 'Licenses &amp; certifications', True)}
      </div>
      <div class="subtle">Main profile page is always captured. Checked detail pages are captured only when that section is actually listed on the profile page.</div>
    </div>

    <div class="row">
      {_checkbox_input(form, 'save_screenshots', 'Save screenshots in PNG? (HTML/JSON auto-saved)', True)}
      {_checkbox_input(form, 'save_profile_photos', 'Save profile headshots', True)}
      {_checkbox_input(form, 'scroll_main_page', 'Scroll main profile page to bottom before capture', True)}
      {_checkbox_input(form, 'scroll_detail_pages', 'Scroll selected detail pages to bottom before capture', True)}
      {_checkbox_input(form, 'headless', 'Run headless; not recommended because login/2FA usually requires a visible browser', False)}
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
    """Scroll the real LinkedIn content area, not just document.body.

    LinkedIn often renders profile content inside internal scrollable containers. A plain
    window.scrollTo() can report a tiny document height even when the profile content area
    still needs scrolling. This routine scrolls the document and the largest scrollable
    elements until heights stabilize.
    """
    last_max_height = 0
    stable_count = 0
    scrolls = 0
    last_stats: Dict[str, Any] = {}

    js = """
    () => {
      const docEl = document.scrollingElement || document.documentElement || document.body;
      const all = Array.from(document.querySelectorAll('*'));
      const scrollables = [];

      function isScrollable(el) {
        if (!el) return false;
        const sh = el.scrollHeight || 0;
        const ch = el.clientHeight || 0;
        if (sh <= ch + 25) return false;
        const style = window.getComputedStyle(el);
        const oy = style.overflowY || '';
        return /(auto|scroll|overlay|visible)/i.test(oy) || el === docEl || el.tagName === 'MAIN';
      }

      for (const el of [docEl, document.body, document.documentElement, document.querySelector('main'), document.querySelector('#workspace'), ...all]) {
        if (!el || scrollables.includes(el)) continue;
        if (isScrollable(el)) scrollables.push(el);
      }

      scrollables.sort((a, b) => (b.scrollHeight || 0) - (a.scrollHeight || 0));
      const chosen = scrollables.slice(0, 12);
      for (const el of chosen) {
        try { el.scrollTop = el.scrollHeight; } catch (e) {}
      }
      try { window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)); } catch (e) {}

      const heights = chosen.map((el) => ({
        tag: el.tagName,
        id: el.id || '',
        role: el.getAttribute && (el.getAttribute('role') || ''),
        testid: el.getAttribute && (el.getAttribute('data-testid') || ''),
        scrollHeight: el.scrollHeight || 0,
        clientHeight: el.clientHeight || 0,
        scrollTop: el.scrollTop || 0
      }));
      const maxHeight = Math.max(
        document.body ? document.body.scrollHeight || 0 : 0,
        document.documentElement ? document.documentElement.scrollHeight || 0 : 0,
        ...heights.map(h => h.scrollHeight || 0)
      );
      return {max_height: maxHeight, scrollable_count: chosen.length, scrollables: heights.slice(0, 5)};
    }
    """

    for i in range(max_scrolls):
        scrolls = i + 1
        try:
            stats = page.evaluate(js) or {}
            last_stats = stats
            max_height = int(stats.get("max_height") or 0)
            time.sleep(pause_seconds)
            if max_height <= int(last_max_height or 0):
                stable_count += 1
            else:
                stable_count = 0
            last_max_height = max_height
            if stable_count >= 2:
                break
        except Exception:
            time.sleep(pause_seconds)

    # Return to the top after loading, so screenshots and any subsequent routines start cleanly.
    try:
        page.evaluate("""
        () => {
          const docEl = document.scrollingElement || document.documentElement || document.body;
          try { docEl.scrollTop = 0; } catch (e) {}
          try { window.scrollTo(0, 0); } catch (e) {}
          for (const el of Array.from(document.querySelectorAll('*'))) {
            try {
              if ((el.scrollHeight || 0) > (el.clientHeight || 0) + 25) el.scrollTop = 0;
            } catch (e) {}
          }
        }
        """)
        time.sleep(0.8)
    except Exception:
        pass

    return {
        "scrolls": scrolls,
        "last_height": last_max_height,
        "scrollable_count": last_stats.get("scrollable_count", 0),
        "scrollables": last_stats.get("scrollables", []),
    }


def expand_about_more(page, max_rounds: int = 1) -> Dict[str, Any]:
    """Click only the About-card text expander on the main profile page.

    This function deliberately scopes to the closest section/card containing an
    exact h1/h2/h3 heading of "About". It clicks at most one expandable text
    button inside that specific card, so Activity/feed post expanders are not
    touched.
    """
    total_clicked = 0
    errors: List[str] = []
    attempts: List[Any] = []
    original_url = page.url

    js = r"""
    () => {
      function norm(s) { return (s || '').replace(/\s+/g, ' ').trim(); }
      const headings = Array.from(document.querySelectorAll('section h1, section h2, section h3'));
      const aboutHeading = headings.find(h => /^About$/i.test(norm(h.innerText || h.textContent)));
      if (!aboutHeading) return {clicked: 0, attempted: [], reason: 'about-heading-not-found'};

      const aboutSection = aboutHeading.closest('section');
      if (!aboutSection) return {clicked: 0, attempted: [], reason: 'about-section-not-found'};

      // Prefer the expander attached to the About text box itself.
      const textBox = aboutSection.querySelector('[data-testid="expandable-text-box"]');
      let button = null;
      if (textBox) {
        button = textBox.querySelector('[data-testid="expandable-text-button"]');
      }
      if (!button) {
        const buttons = Array.from(aboutSection.querySelectorAll('[data-testid="expandable-text-button"]'));
        button = buttons.find(b => /(?:…|\.\.\.)\s*more\b|\bsee more\b/i.test(norm(b.innerText || b.textContent || b.getAttribute('aria-label'))));
      }
      if (!button) return {clicked: 0, attempted: [], reason: 'about-more-not-found'};

      const label = norm(button.innerText || button.textContent || button.getAttribute('aria-label')).slice(0, 80);
      const attempted = [];
      function fireClick(el) {
        if (!el) return false;
        try { el.scrollIntoView({block: 'center', inline: 'nearest'}); } catch (e) {}
        try { el.click(); return true; } catch (e) {}
        try {
          el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
          el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
          el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
          return true;
        } catch (e) {}
        return false;
      }

      const clickableChild = button.querySelector('span[style*="pointer-events: auto"], span, div') || button.firstElementChild;
      const ok = fireClick(clickableChild) || fireClick(button);
      attempted.push({label, ok, testid: button.getAttribute('data-testid') || '', scoped_to: 'about'});
      return {clicked: ok ? 1 : 0, attempted, reason: ''};
    }
    """

    for _round in range(max_rounds):
        try:
            result = page.evaluate(js) or {}
            attempts.extend(result.get("attempted") or [])
            clicked = int(result.get("clicked") or 0)
            if clicked <= 0:
                if result.get("reason"):
                    attempts.append({"label": result.get("reason"), "ok": False, "testid": "", "scoped_to": "about"})
                break
            total_clicked += clicked
            time.sleep(1.0)
            if page.url != original_url:
                errors.append(f"Unsafe About expander navigation: {page.url}")
                try:
                    page.goto(original_url, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(1.0)
                except Exception as nav_exc:
                    errors.append(f"Failed to return after unsafe navigation: {nav_exc}")
                break
        except Exception as exc:
            errors.append(str(exc))
            break

    return {
        "more_expanders_clicked": total_clicked,
        "more_expander_errors": errors,
        "more_expander_attempts": attempts[:30],
        "about_more_expanders_clicked": total_clicked,
    }

def detect_profile_sections(page) -> Dict[str, Any]:
    """Detect whether detail sections are actually listed on the main profile page.

    This intentionally does not scan arbitrary body text for words like
    "volunteering". It looks for section headings and LinkedIn detail links
    associated with the known profile section URLs.
    """
    js = r"""
    () => {
      function norm(s) { return (s || '').replace(/\s+/g, ' ').trim(); }
      function lines(s) {
        return (s || '')
          .split(/[\n\r]+/)
          .map(norm)
          .filter(Boolean);
      }
      const specs = {
        experience: {slug: '/details/experience/', labels: ['Experience']},
        education: {slug: '/details/education/', labels: ['Education']},
        volunteering: {slug: '/details/volunteering-experiences/', labels: ['Volunteering', 'Volunteer experience', 'Volunteer Experience']},
        certifications: {slug: '/details/certifications/', labels: ['Licenses & certifications', 'Licenses and certifications', 'Certifications']}
      };

      const result = {};
      const headings = [];
      const detailLinks = [];

      for (const sec of Array.from(document.querySelectorAll('section'))) {
        const headingEls = Array.from(sec.querySelectorAll('h1,h2,h3'));
        const secHeadings = [];
        for (const h of headingEls) {
          const vals = lines(h.innerText || h.textContent);
          for (const v of vals) {
            if (!secHeadings.includes(v)) secHeadings.push(v);
            if (!headings.includes(v)) headings.push(v);
          }
        }
        for (const [key, spec] of Object.entries(specs)) {
          const headingMatch = secHeadings.some(h => spec.labels.some(label => h.toLowerCase() === label.toLowerCase()));
          if (headingMatch) {
            result[key] = result[key] || {listed: false, evidence: []};
            result[key].listed = true;
            result[key].evidence.push({type: 'section_heading', value: secHeadings.join(' | ')});
          }
        }
      }

      for (const a of Array.from(document.querySelectorAll('a[href]'))) {
        let href = a.getAttribute('href') || '';
        if (!href) continue;
        for (const [key, spec] of Object.entries(specs)) {
          if (href.includes(spec.slug)) {
            result[key] = result[key] || {listed: false, evidence: []};
            result[key].listed = true;
            result[key].evidence.push({type: 'detail_link', value: href});
            detailLinks.push({section: key, href});
          }
        }
      }

      for (const key of Object.keys(specs)) {
        result[key] = result[key] || {listed: false, evidence: []};
      }
      return {sections: result, headings, detailLinks};
    }
    """
    try:
        return page.evaluate(js) or {"sections": {}, "headings": [], "detailLinks": []}
    except Exception as exc:
        return {"sections": {}, "headings": [], "detailLinks": [], "error": str(exc)}


def classify_detail_page(page, section: str) -> Dict[str, Any]:
    """Classify a selected detail page as content, empty, or unknown.

    We no longer rely on the main profile page to decide whether a detail section
    exists. Instead, we open the direct detail URL and classify the actual page.
    Empty detail pages are recorded as JSON-only metadata.
    """
    labels = {
        "experience": ["Experience"],
        "education": ["Education"],
        "volunteering": ["Volunteering", "Volunteer experience", "Volunteer Experience"],
        "certifications": ["Licenses & certifications", "Licenses and certifications", "Certifications"],
    }
    js = r"""
    (sectionLabels) => {
      function norm(s) { return (s || '').replace(/\s+/g, ' ').trim(); }
      const bodyText = norm(document.body ? document.body.innerText : '');
      const headings = Array.from(document.querySelectorAll('h1,h2,h3')).map(h => norm(h.innerText || h.textContent)).filter(Boolean);
      const lowerBody = bodyText.toLowerCase();
      const hasEmptyMessage = /nothing to see for now/i.test(bodyText) || /no .* to show/i.test(bodyText);
      const hasSectionHeading = headings.some(h => sectionLabels.some(label => h.toLowerCase() === label.toLowerCase()));
      const primary = document.querySelector('main#workspace') || document.querySelector('main') || document.body;
      const primaryText = norm(primary ? primary.innerText : bodyText);
      const primaryLower = primaryText.toLowerCase();
      const hasProfileTopcard = /contact info/i.test(primaryText) || /connections/i.test(primaryText);

      // Heuristic: a real detail page usually has a matching section heading and
      // meaningful primary content, while an empty detail page displays the
      // standard LinkedIn empty-state text.
      let status = 'unknown';
      let reason = '';
      if (hasEmptyMessage) {
        status = 'empty_section';
        reason = 'LinkedIn empty-state message detected';
      } else if (hasSectionHeading) {
        status = 'content';
        reason = 'matching detail section heading detected';
      } else if (!hasSectionHeading && hasProfileTopcard && primaryText.length < 2500) {
        status = 'empty_section';
        reason = 'profile/topcard content only; no matching detail section heading';
      } else {
        status = 'unknown';
        reason = 'no empty-state message and no matching detail heading detected';
      }
      return {status, reason, headings, hasEmptyMessage, hasSectionHeading, primaryTextLength: primaryText.length};
    }
    """
    try:
        return page.evaluate(js, labels.get(section, [section])) or {"status": "unknown", "reason": "no result"}
    except Exception as exc:
        return {"status": "unknown", "reason": str(exc), "error": str(exc)}


def save_json_only_section_metadata(page, target_dir: Path, section: str, status: str, classification: Dict[str, Any], capture_stats: Optional[Dict[str, Any]] = None) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    meta_path = target_dir / f"{section}.json"
    page_title = ""
    try:
        page_title = page.title()
    except Exception:
        pass
    meta = {
        "section": section,
        "status": status,
        "url": page.url,
        "title": page_title,
        "captured_at": _now(),
        "html_path": "",
        "screenshot_path": "",
        "html_size_bytes": 0,
        "screenshot_size_bytes": 0,
        "capture_stats": capture_stats or {},
        "detail_classification": classification,
        "json_only_reason": "Detail section was empty or could not be confirmed as populated; HTML/PNG not saved.",
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta_path


def update_json_metadata(json_path: Path, updates: Dict[str, Any]) -> None:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else {}
        data.update(updates)
        json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        try:
            json_path.with_suffix('.json_update_error.txt').write_text(str(exc), encoding="utf-8", errors="replace")
        except Exception:
            pass


def save_capture(page, target_dir: Path, section: str, save_screenshot: bool, capture_stats: Optional[Dict[str, Any]] = None) -> Tuple[Path, Optional[Path], Dict[str, Any]]:
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

    page_title = ""
    try:
        page_title = page.title()
    except Exception:
        pass

    meta = {
        "section": section,
        "url": page.url,
        "title": page_title,
        "captured_at": _now(),
        "html_path": _short_path(html_path),
        "screenshot_path": _short_path(screenshot_path) if screenshot_path else "",
        "html_size_bytes": html_path.stat().st_size if html_path.exists() else 0,
        "screenshot_size_bytes": screenshot_path.stat().st_size if screenshot_path and screenshot_path.exists() else 0,
        "capture_stats": capture_stats or {},
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return html_path, screenshot_path, meta



def capture_profile_photo(page, target_dir: Path) -> Dict[str, Any]:
    """Save the profile headshot from the main profile page, when one exists.

    This captures only the profile-photo image element, not the whole profile page.
    It intentionally targets the top-card "Profile photo" container so it does not
    accidentally save the logged-in user's small nav avatar or unrelated images.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / "profile_photo.png"
    meta_path = target_dir / "profile_photo.json"
    result: Dict[str, Any] = {
        "status": "not_found",
        "profile_photo_path": "",
        "profile_photo_src": "",
        "profile_photo_srcset": "",
        "error": "",
        "captured_at": _now(),
    }

    try:
        # Prefer the explicit top-card profile-photo container. This avoids the
        # small "Me" avatar in the LinkedIn navigation bar.
        locator = page.locator('[aria-label="Profile photo"] img[src]').first
        try:
            count = page.locator('[aria-label="Profile photo"] img[src]').count()
        except Exception:
            count = 0

        if count <= 0:
            # Some LinkedIn layouts put the img under a topcard logo-image key.
            alt_locator = page.locator('a[componentkey="topcard-logo-image-referencekey"] img[src], a[href*="/in/"] [aria-label="Profile photo"] img[src]').first
            try:
                alt_count = page.locator('a[componentkey="topcard-logo-image-referencekey"] img[src], a[href*="/in/"] [aria-label="Profile photo"] img[src]').count()
            except Exception:
                alt_count = 0
            if alt_count > 0:
                locator = alt_locator
                count = alt_count

        if count <= 0:
            result["error"] = "No profile-photo img element was found. Profile may use a placeholder/SVG or image may be hidden."
            meta_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            return result

        try:
            locator.scroll_into_view_if_needed(timeout=10000)
        except Exception:
            pass
        time.sleep(0.5)

        src = locator.get_attribute("src", timeout=10000) or ""
        srcset = locator.get_attribute("srcset", timeout=10000) or ""
        result["profile_photo_src"] = src
        result["profile_photo_srcset"] = srcset

        # If LinkedIn shows only a placeholder SVG, there may be no useful img src.
        if not src and not srcset:
            result["error"] = "Profile-photo element was found, but no src/srcset was available."
            meta_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            return result

        locator.screenshot(path=str(out_path), timeout=30000)
        result["status"] = "ok"
        result["profile_photo_path"] = _short_path(out_path)
        result["profile_photo_size_bytes"] = out_path.stat().st_size if out_path.exists() else 0
        meta_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        return result
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        try:
            meta_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return result


def capture_one_section(page, url: str, target_dir: Path, section: str, max_scrolls: int, scroll_pause: float, delay_seconds: float, save_screenshot: bool, do_scroll: bool = True) -> Tuple[str, str, str]:
    """Return status, html_path, screenshot_path for a single page/section."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(delay_seconds)
        dismiss_obvious_popups(page)
        if section == "main":
            # Main profile page: only expand the About-section "more" text. Do not click
            # Activity/feed post expanders, because those can navigate to post pages.
            expand_stats = expand_about_more(page)
            if do_scroll:
                first_scroll_stats = scroll_to_bottom(page, max_scrolls=max_scrolls, pause_seconds=scroll_pause)
                second_scroll_stats = scroll_to_bottom(page, max_scrolls=2, pause_seconds=scroll_pause)
            else:
                first_scroll_stats = {"skipped": True, "reason": "scroll_main_page unchecked"}
                second_scroll_stats = {"skipped": True, "reason": "scroll_main_page unchecked"}
            capture_stats = {
                "first_scroll": first_scroll_stats,
                "second_scroll": second_scroll_stats,
                "more_expanders_clicked": int(expand_stats.get("more_expanders_clicked", 0)),
                "more_expander_errors": expand_stats.get("more_expander_errors", []) or [],
                "more_expander_attempts": expand_stats.get("more_expander_attempts", []) or [],
                "about_more_expanders_clicked": int(expand_stats.get("about_more_expanders_clicked", 0)),
                "detail_page_expansion_skipped": False,
            }
        else:
            # Detail pages render entries fully expanded; optional scrolling can trigger lazy loading.
            if do_scroll:
                first_scroll_stats = scroll_to_bottom(page, max_scrolls=max_scrolls, pause_seconds=scroll_pause)
                second_scroll_stats = scroll_to_bottom(page, max_scrolls=2, pause_seconds=scroll_pause)
            else:
                first_scroll_stats = {"skipped": True, "reason": "scroll_detail_pages unchecked"}
                second_scroll_stats = {"skipped": True, "reason": "scroll_detail_pages unchecked"}
            capture_stats = {
                "first_scroll": first_scroll_stats,
                "second_scroll": second_scroll_stats,
                "more_expanders_clicked": 0,
                "more_expander_errors": [],
                "more_expander_attempts": [],
                "about_more_expanders_clicked": 0,
                "detail_page_expansion_skipped": True,
            }
            classification = classify_detail_page(page, section)
            capture_stats["detail_classification"] = classification
            if classification.get("status") == "empty_section":
                save_json_only_section_metadata(page, target_dir, section, "empty_section", classification, capture_stats=capture_stats)
                return "empty", "", ""

        html_path, screenshot_path, _meta = save_capture(page, target_dir, section, save_screenshot, capture_stats=capture_stats)
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
        paths.get("main", {}).get("profile_photo", ""),
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
                main_html, main_screenshot, profile_photo,
                experience_html, experience_screenshot,
                education_html, education_screenshot,
                volunteering_html, volunteering_screenshot,
                certifications_html, certifications_screenshot,
                error_message, collected_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
    selected_detail_sections = []
    if _checkbox(form, "capture_experience", True):
        selected_detail_sections.append(("experience", "experience"))
    if _checkbox(form, "capture_education", True):
        selected_detail_sections.append(("education", "education"))
    if _checkbox(form, "capture_volunteering", True):
        selected_detail_sections.append(("volunteering", "volunteering-experiences"))
    if _checkbox(form, "capture_certifications", True):
        selected_detail_sections.append(("certifications", "certifications"))
    save_screenshots = _checkbox(form, "save_screenshots", True)
    save_profile_photos = _checkbox(form, "save_profile_photos", True)
    scroll_main_page = _checkbox(form, "scroll_main_page", True)
    scroll_detail_pages = _checkbox(form, "scroll_detail_pages", True)
    headless = _checkbox(form, "headless", False)

    run_id = _run_id()
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    rows: List[List[str]] = []

    run_settings = {
        "selected_detail_sections": [name for name, _slug in selected_detail_sections],
        "save_screenshots": save_screenshots,
        "save_profile_photos": save_profile_photos,
        "scroll_main_page": scroll_main_page,
        "scroll_detail_pages": scroll_detail_pages,
        "headless": headless,
        "raw_form_option_values": {
            key: str(form.get(key, ""))
            for key in [
                "capture_experience", "capture_education", "capture_volunteering", "capture_certifications",
                "save_screenshots", "save_profile_photos", "scroll_main_page", "scroll_detail_pages", "headless"
            ]
        },
    }
    (run_dir / "run_settings.json").write_text(json.dumps(run_settings, indent=2, ensure_ascii=False), encoding="utf-8")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO evidence_runs (run_id, created_at, source_label, target_count, notes) VALUES (?,?,?,?,?)",
            (run_id, _now(), "CSV/text input", len(targets), "LinkedIn Evidence Capture v1.9 - section-aware detail capture + optional profile photos"),
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

            # Always capture the main profile page first. After the main page is loaded,
            # use its actual section headings/detail links to decide which detail pages
            # should be visited. This avoids capturing empty detail pages for sections
            # that are not listed on the profile.
            main_status, main_html_path, main_screenshot_path = capture_one_section(
                page=page,
                url=detail_url(target.linkedin_url, ""),
                target_dir=target_dir,
                section="main",
                max_scrolls=max_scrolls,
                scroll_pause=scroll_pause,
                delay_seconds=delay_seconds,
                save_screenshot=save_screenshots,
                do_scroll=scroll_main_page,
            )
            paths["main"] = {"html": main_html_path, "screenshot": main_screenshot_path, "profile_photo": ""}
            profile_photo_info = {"status": "skipped", "reason": "save_profile_photos unchecked"}
            if main_status == "ok" and save_profile_photos:
                profile_photo_info = capture_profile_photo(page, target_dir)
                if profile_photo_info.get("status") == "ok":
                    paths["main"]["profile_photo"] = profile_photo_info.get("profile_photo_path", "")
            if main_status == "ok":
                try:
                    update_json_metadata(target_dir / "main.json", {
                        "save_profile_photos": save_profile_photos,
                        "scroll_main_page": scroll_main_page,
                        "scroll_detail_pages": scroll_detail_pages,
                        "profile_photo_capture": profile_photo_info,
                        "profile_photo_path": profile_photo_info.get("profile_photo_path", ""),
                    })
                except Exception:
                    pass
            if main_status != "ok":
                errors.append("main: capture failed")

            # v1.6: Do not rely on the main page to decide whether detail sections
            # exist. LinkedIn often does not load the lower profile cards into the
            # main-page DOM. Instead, visit each selected detail URL and classify
            # that detail page as content vs empty_section.
            captured_detail_sections: List[str] = []
            empty_detail_sections: List[str] = []
            attempted_detail_sections: List[str] = [name for name, _slug in selected_detail_sections]

            try:
                main_json_path = target_dir / "main.json"
                update_json_metadata(main_json_path, {
                    "requested_detail_sections": attempted_detail_sections,
                    "detail_section_detection_method": "detail_page_classification",
                    "detail_section_detection_note": "Main profile cards are not used as the gate because LinkedIn may not load lower profile cards into the main-page DOM.",
                })
            except Exception:
                pass

            time.sleep(max(1.0, delay_seconds / 2.0))

            for section_name, section_slug in selected_detail_sections:
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
                    do_scroll=scroll_detail_pages,
                )
                paths[section_name] = {"html": html_path, "screenshot": screenshot_path}
                if status == "ok":
                    captured_detail_sections.append(section_name)
                elif status == "empty":
                    empty_detail_sections.append(section_name)
                else:
                    errors.append(f"{section_name}: capture failed")
                # Small pause between section pages.
                time.sleep(max(1.0, delay_seconds / 2.0))

            try:
                update_json_metadata(target_dir / "main.json", {
                    "captured_detail_sections": captured_detail_sections,
                    "empty_detail_sections": empty_detail_sections,
                    "skipped_detail_sections_not_selected": [
                        name for name, _slug in [("experience", "experience"), ("education", "education"), ("volunteering", "volunteering-experiences"), ("certifications", "certifications")]
                        if name not in attempted_detail_sections
                    ],
                })
            except Exception:
                pass

            status = "ok" if not errors else "partial"
            row = build_row(idx, target, status, paths, "; ".join(errors), collected_at)
            rows.append(row)
            save_row_to_db(run_id, row)

    manifest_path = write_manifest(run_dir, rows)
    # Mutate the form dict so patched app.py can render a post-run action button.
    form["_lec_last_run_dir"] = _short_path(run_dir)
    form["_lec_last_manifest"] = _short_path(manifest_path)
    return HEADERS, rows


def export_rows(form: Dict[str, Any]) -> Iterable[List[str]]:
    # For export, rerun collection rather than exporting old DB rows, matching the app's existing plugin pattern.
    headers, rows = run(form)
    yield headers
    for row in rows:
        yield row


def _latest_run_dir() -> Optional[Path]:
    try:
        if not RUNS_DIR.exists():
            return None
        dirs = [p for p in RUNS_DIR.iterdir() if p.is_dir()]
        if not dirs:
            return None
        return max(dirs, key=lambda p: p.stat().st_mtime)
    except Exception:
        return None


def _resolve_safe_output_path(raw_path: str) -> Path:
    """Resolve a user-supplied relative path, constrained to this module's data dir."""
    if raw_path:
        raw = Path(raw_path)
        candidate = raw.resolve() if raw.is_absolute() else (BASE_DIR / raw).resolve()
    else:
        latest = _latest_run_dir()
        candidate = latest.resolve() if latest else RUNS_DIR.resolve()
    allowed_root = MODULE_DATA_DIR.resolve()
    if candidate != allowed_root and allowed_root not in candidate.parents:
        raise ValueError(f"Refusing to open a path outside {allowed_root}: {candidate}")
    if not candidate.exists():
        raise FileNotFoundError(f"Output folder does not exist: {candidate}")
    if candidate.is_file():
        candidate = candidate.parent
    return candidate


def _open_in_file_manager(path: Path) -> None:
    system = platform.system().lower()
    if system == "windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif system == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def result_actions(form: Dict[str, Any], headers: List[str], rows: List[List[str]]) -> str:
    """Optional hook used by patched app.py to show a post-run action button."""
    if not rows:
        return ""
    run_dir = str(form.get("_lec_last_run_dir") or "")
    manifest = str(form.get("_lec_last_manifest") or "")
    folder_label = html.escape(run_dir or "latest LEC run folder")
    manifest_html = ""
    if manifest:
        manifest_html = f'<span class="subtle" style="margin-left:8px;">Manifest: <code>{html.escape(manifest)}</code></span>'
    return f'''
    <div class="notice">
      <b>LEC output:</b> <code>{folder_label}</code><br>
      <form method="post" action="/plugin_action" style="display:inline-block; margin-top:8px;">
        <input type="hidden" name="qkey" value="{META['key']}">
        <input type="hidden" name="action" value="open_output_folder">
        <input type="hidden" name="path" value="{html.escape(run_dir, quote=True)}">
        <button type="submit">Open output folder in Explorer</button>
      </form>
      {manifest_html}
    </div>
    '''


def handle_action(form: Dict[str, Any]) -> str:
    action = str(form.get("action") or "")
    if action != "open_output_folder":
        raise ValueError(f"Unknown LEC action: {action}")
    path = _resolve_safe_output_path(str(form.get("path") or ""))
    _open_in_file_manager(path)
    return f"Opened output folder: {path}"
