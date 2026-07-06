from __future__ import annotations

import csv
import importlib
import io
import json
import pkgutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, Response, redirect, render_template_string, request, url_for
from osint_common import enforce_source_access

load_dotenv()

app = Flask(__name__)

PLUGIN_PACKAGE = "queries"
PLUGIN_DIR = Path(__file__).parent / PLUGIN_PACKAGE
REGISTRY: dict[str, Any] = {}
PLUGIN_FINGERPRINT: tuple[tuple[str, int], ...] | None = None


def plugin_fingerprint() -> tuple[tuple[str, int], ...]:
    """Return a cheap signature of query plugin files for auto-reload."""
    return tuple(
        sorted(
            (path.name, path.stat().st_mtime_ns)
            for path in PLUGIN_DIR.glob("*.py")
            if not path.name.startswith("_")
        )
    )


def load_plugins() -> dict[str, Any]:
    """Load/reload query plugins from the local queries/ directory.

    A plugin is a Python module with:
    - META: dict with at least key/name/description
    - render_fields(form): returns HTML for input fields
    - run(form): returns (headers, rows)
    - export_rows(form): yields rows for CSV export

    This mirrors the plugin pattern in the IRS 990 console, but swaps the
    SQLite/local-database back end for remote OSINT/API queries.
    """
    loaded: dict[str, Any] = {}
    base_dir = str(Path(__file__).parent)
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)

    for info in pkgutil.iter_modules([str(PLUGIN_DIR)]):
        if info.name.startswith("_"):
            continue

        module_name = f"{PLUGIN_PACKAGE}.{info.name}"
        try:
            if module_name in sys.modules:
                mod = importlib.reload(sys.modules[module_name])
            else:
                mod = importlib.import_module(module_name)

            required = ["META", "render_fields", "run", "export_rows"]
            if all(hasattr(mod, name) for name in required):
                key = mod.META.get("key")
                if key:
                    loaded[key] = mod
        except Exception as exc:  # pragma: no cover - visible in app UI/logs
            print(f"Failed to load plugin {module_name}: {exc}", file=sys.stderr)
            traceback.print_exc()

    return loaded


def ensure_registry() -> None:
    global REGISTRY, PLUGIN_FINGERPRINT
    current_fingerprint = plugin_fingerprint()
    if not REGISTRY or current_fingerprint != PLUGIN_FINGERPRINT:
        REGISTRY = load_plugins()
        PLUGIN_FINGERPRINT = current_fingerprint


def request_payload() -> dict[str, Any]:
    """Return form fields plus uploaded files for plugins that support CSV upload.

    Existing text-only plugins can continue to use this like a normal dict.
    File-aware plugins can read form.get("_files") to access request.files.
    """
    form: dict[str, Any] = request.form.to_dict(flat=True)
    form["_files"] = request.files
    return form


def csv_row(values: list[Any] | tuple[Any, ...]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(values)
    return buf.getvalue()


BASE_CSS = """
  :root {
    --border: #d8dde6;
    --ink: #202733;
    --muted: #647084;
    --panel: #f7f9fc;
    --primary: #1c78a6;
    --primary-dark: #125f85;
    --warn: #fff8d6;
    --err: #ffecec;
  }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, Segoe UI, Arial, sans-serif;
    color: var(--ink);
    max-width: 1320px;
    min-height: 100vh;
    margin: 0 auto;
    padding: 18px 24px 0;
    display: flex;
    flex-direction: column;
  }
  main { flex: 1; }
  a { color: var(--primary); }
  h1 { margin: 0; font-size: 26px; line-height: 1.15; }
  h2 { margin: 24px 0 6px; }
  h3 { margin: 0 0 8px; font-size: 18px; }
  .site-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--border);
  }
  .title-wrap { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .home-link {
    width: 34px;
    height: 34px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--primary-dark);
    background: #fff;
    flex: 0 0 auto;
  }
  .home-link:hover { background: var(--panel); }
  .home-link svg { width: 20px; height: 20px; }
  .brand-link { display: inline-flex; flex: 0 0 auto; }
  .brand-logo { width: 118px; height: auto; flex: 0 0 auto; }
  .footer {
    margin-top: 32px;
    padding: 18px 0;
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 13px;
    text-align: center;
  }
  .subtle, .note, .description { color: var(--muted); line-height: 1.35; }
  .subtle { font-size: 0.95rem; }
  .home-title-row {
    display: flex;
    align-items: baseline;
    gap: 14px;
    flex-wrap: wrap;
    margin-top: 24px;
  }
  .home-title-row h2, .home-title-row .note { margin: 0; }
  .notice {
    background: #f9fbff;
    border: 1px solid #c9d8ff;
    border-radius: 8px;
    padding: 10px;
    margin: 14px 0 4px;
  }
  .pill {
    display: inline-block;
    border: 1px solid #ccd4de;
    border-radius: 999px;
    padding: 2px 8px;
    margin: 2px 6px 2px 0;
    color: #333;
    background: #fff;
    font-size: 13px;
  }
  .nav-links { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
  .button-link {
    border: 1px solid var(--border);
    background: #fff;
    color: var(--primary-dark);
    border-radius: 6px;
    padding: 8px 12px;
    min-height: 36px;
    font: inherit;
    font-weight: 650;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
  }
  .button-link:hover { background: var(--panel); }
  .source-badge {
    display: inline-flex;
    align-items: center;
    border: 1px solid #ccd4de;
    border-radius: 999px;
    padding: 2px 8px;
    background: var(--panel);
    color: #334155;
    font-size: 12px;
    font-weight: 700;
  }
  .limitations {
    margin: 10px 0 0;
    padding: 10px 12px;
    border: 1px solid #e6d37a;
    border-radius: 8px;
    background: var(--warn);
  }
  .limitations ul { margin: 6px 0 0 18px; padding: 0; }
  .module-sections { display: grid; gap: 26px; max-width: 980px; margin-top: 18px; }
  .module-list { display: grid; gap: 10px; }
  .module-row {
    display: grid;
    grid-template-columns: minmax(230px, 310px) 1fr;
    gap: 14px;
    align-items: center;
    padding: 10px 0;
    border-bottom: 1px solid #eef1f5;
  }
  .module-button,
  button {
    border: 1px solid var(--primary-dark);
    background: var(--primary);
    color: #fff;
    border-radius: 6px;
    padding: 8px 12px;
    font: inherit;
    font-weight: 650;
    cursor: pointer;
    text-decoration: none;
    text-align: center;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 36px;
  }
  .module-button:hover,
  button:hover { background: var(--primary-dark); }
  button.secondary {
    color: var(--primary-dark);
    background: #fff;
    border-color: var(--border);
  }
  button.secondary:hover { background: var(--panel); }
  .panel { border: 1px solid var(--border); border-radius: 8px; padding: 12px; background: #fff; margin: 12px 0; }
  .row { margin: 10px 0; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px 16px; }
  .toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  label { font-weight: 650; display: block; margin-bottom: 3px; }
  input[type="text"], input[type="number"], input[type="date"], input[type="file"], input[type="password"], select, textarea {
    width: 100%;
    padding: 7px;
    border: 1px solid #b7c0cc;
    border-radius: 6px;
    font-family: inherit;
    font-size: 14px;
  }
  textarea { min-height: 86px; resize: vertical; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 6px; border-bottom: 1px solid #eee; vertical-align: top; }
  thead th { position: sticky; top: 0; background: var(--panel); border-bottom: 1px solid var(--border); z-index: 1; }
  td { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td.wrap, td:nth-child(10), td:nth-child(11), td:nth-child(12) { white-space: pre-wrap; overflow: visible; text-overflow: clip; }
  .results { overflow: auto; max-height: 64vh; border: 1px solid var(--border); border-radius: 8px; }
  .err { background: var(--err); border: 1px solid #f5b5b5; padding: 10px; white-space: pre-wrap; border-radius: 6px; }
  .running-msg {
    display: none;
    margin: 10px 0;
    padding: 10px;
    background: var(--warn);
    border: 1px solid #e6d37a;
    border-radius: 6px;
    font-weight: 650;
  }
  body.is-running .running-msg { display: block; }
  body.is-running button { opacity: 0.6; cursor: not-allowed; }
  @media (max-width: 700px) {
    body { padding: 14px 14px 0; }
    .site-header { align-items: flex-start; }
    .brand-logo { width: 88px; }
    h1 { font-size: 22px; }
    .home-title-row { gap: 6px 12px; }
    .module-row { grid-template-columns: 1fr; gap: 6px; }
    .module-button { justify-content: center; }
  }
"""

HOME_ICON = """
<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor"
     stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M3 11.5 12 4l9 7.5"></path>
  <path d="M5 10.5V20h5v-5h4v5h5v-9.5"></path>
</svg>
"""

LAYOUT_START = """
<!doctype html>
<title>{{ title or "Social OSINT - Query Console" }}</title>
<meta charset="utf-8">
<style>{{ css | safe }}</style>

<body>
<header class="site-header">
  <div class="title-wrap">
    <a class="home-link" href="{{ url_for('home') }}" aria-label="Home">{{ home_icon | safe }}</a>
    <h1>Social OSINT - Query Console</h1>
  </div>
  <div class="nav-links">
    <a class="button-link" href="{{ url_for('resources') }}">Resources</a>
    <a class="brand-link" href="https://www.saveoregonschools.com" aria-label="Save Oregon Schools website">
      <img class="brand-logo" src="{{ url_for('static', filename='save-oregon-schools-logo.png') }}" alt="Save Oregon Schools">
    </a>
  </div>
</header>
<main>
"""

LAYOUT_END = """
</main>
<footer class="footer">
  Copyright &copy; {{ year }} Save Oregon Schools, LLC.
  <a href="https://www.saveoregonschools.com">www.saveoregonschools.com</a>
  |
  <a href="https://github.com/SaveOregonSchools">Check out all our apps on GitHub</a>
</footer>
</body>
"""

HOME_MENU = [
    (
        "Platform APIs",
        [
            (
                "x_recent_search",
                "X Recent Search",
                "Search recent public X posts with an API bearer token, including optional reply/conversation filters.",
            ),
            (
                "meta_facebook_page_content_search",
                "Facebook Page Posts & Comments",
                "Collect visible Page posts and optional top-level comments through Meta Graph API access.",
            ),
            (
                "meta_ad_library_search",
                "Meta Ad Library",
                "Search public Meta ads delivered on Facebook, Instagram, and Threads.",
            ),
        ],
    ),
    (
        "Bluesky",
        [
            (
                "bluesky_keyword_search",
                "Keyword Search",
                "Search public Bluesky posts globally or within selected accounts.",
            ),
            (
                "bluesky_author_feed_scan",
                "Author Feed Scan",
                "Scan public account feeds for candidate-intervention, lobbying, or custom terms.",
            ),
            (
                "bluesky_profile_lookup",
                "Profile Lookup",
                "Resolve and export public Bluesky profile metadata.",
            ),
        ],
    ),
    (
        "Browser-Assisted",
        [
            (
                "linkedin_profile_collector_v1",
                "LinkedIn Profile Collector",
                "Open a browser you control, then collect fields visible to your logged-in session.",
            ),
            (
                "linkedin_evidence_capture_v1",
                "LinkedIn Evidence Capture",
                "Capture visible LinkedIn pages as evidence snapshots from a user-controlled browser session.",
            ),
        ],
    ),
]

HOME_HTML = LAYOUT_START + """
<div class="home-title-row">
  <h2>Home</h2>
  <p class="note">Select a research module from the list below</p>
</div>

<p class="subtle">
  Public-source collection helper for nonprofit social-media review. This app does not bypass platform restrictions
  or determine legality. Treat flags as leads for review and preserve screenshots/archives separately.
</p>

<div class="notice">
  <span class="pill">API modules</span>
  <span class="pill">Browser-assisted modules</span>
  <span class="pill">CSV input/export</span>
  <span class="pill">Plugin-based</span>
</div>

{% if not registry %}
  <div class="err"><b>No plugins loaded.</b> Make sure the queries/ directory exists and contains query modules.</div>
{% endif %}

<div class="module-sections">
  {% for section in home_sections %}
    <section class="module-section">
      <h3>{{ section.title }}</h3>
      <div class="module-list">
        {% for item in section.entries %}
          <div class="module-row">
            <a class="module-button" href="{{ item.href }}">{{ item.label }}</a>
            <div class="description">
              <span class="source-badge">{{ item.source_type }}</span>
              {{ item.description }}
            </div>
          </div>
        {% endfor %}
      </div>
    </section>
  {% endfor %}
</div>
""" + LAYOUT_END

QUERY_HTML = LAYOUT_START + """
<form method="post" action="{{ url_for('select') }}" class="panel">
  <div class="toolbar">
    <label for="qkey" style="margin:0"><b>Query:</b></label>
    <select name="qkey" id="qkey" style="width:auto; min-width: 360px;" onchange="this.form.submit()">
      {% for key, mod in query_options %}
        <option value="{{ key }}" {% if key == qkey %}selected{% endif %}>{{ mod.META["name"] }}</option>
      {% endfor %}
    </select>
    <button class="secondary" formaction="{{ url_for('refresh') }}" formmethod="post" type="submit">Refresh Queries</button>
  </div>
</form>

{% if qkey and qkey in registry %}
  <div class="panel">
    <h2>{{ registry[qkey].META["name"] }}</h2>
    <div class="toolbar">
      <span class="source-badge">{{ registry[qkey].META.get("source_type", "official_api") }}</span>
      {% if registry[qkey].META.get("coverage") %}
        <span class="subtle">{{ registry[qkey].META.get("coverage") }}</span>
      {% endif %}
    </div>
    <p class="subtle">{{ registry[qkey].META.get("description","") }}</p>
    {% if registry[qkey].META.get("limitations") %}
      <div class="limitations">
        <b>Limitations</b>
        <ul>
          {% for limitation in registry[qkey].META.get("limitations", []) %}
            <li>{{ limitation }}</li>
          {% endfor %}
        </ul>
      </div>
    {% endif %}

    <form method="post" action="{{ url_for('run') }}" enctype="multipart/form-data" onsubmit="return showRunningMessage(event, this);">
      <input type="hidden" name="qkey" value="{{ qkey }}">
      <div class="row">
        <label>Data access mode</label>
        <select name="data_access_mode">
          {% for value, label in data_access_modes %}
            <option value="{{ value }}" {% if (form or {}).get("data_access_mode", "official") == value %}selected{% endif %}>{{ label }}</option>
          {% endfor %}
        </select>
      </div>
      {{ registry[qkey].render_fields(form or {}) | safe }}

      <div class="toolbar" style="margin-top:12px;">
        {% if not hide_preview_limit %}
          <label style="margin:0">Preview row limit:</label>
          <input type="number" name="_limit" value="{{ (form or {}).get('_limit','500') }}" min="1" style="width:100px">
        {% endif %}
        <button type="submit">{{ run_button_label }}</button>
        {% if not hide_csv_export %}
          <button formaction="{{ url_for('export') }}" formmethod="post" formenctype="multipart/form-data">Export CSV (full result)</button>
          <button formaction="{{ url_for('export_jsonl') }}" formmethod="post" formenctype="multipart/form-data">Export JSONL</button>
          <button formaction="{{ url_for('raw_json_export') }}" formmethod="post" formenctype="multipart/form-data">Save Raw JSON</button>
          <button formaction="{{ url_for('archive_urls') }}" formmethod="post" formenctype="multipart/form-data">Archive URLs</button>
        {% endif %}
        <a class="button-link" href="{{ url_for('evidence_checklist') }}">Open Evidence Checklist</a>
      </div>
      <div class="running-msg">Running query. Public APIs and browser-assisted modules can be slow; the result will appear below.</div>
    </form>
  </div>

  {% if error %}
    <div class="err"><b>Error:</b>\n{{ error }}</div>
  {% endif %}

  {% if headers and rows is not none %}
    {% if custom_results_html %}
      {{ custom_results_html | safe }}
    {% else %}
      <p class="subtle">Showing up to <b>{{ (form or {}).get('_limit','500') }}</b> rows. Preview contains <b>{{ len(rows) }}</b> rows.</p>
      {% if result_actions %}
        {{ result_actions | safe }}
      {% endif %}
      <div class="results">
        <table>
          <thead><tr>{% for h in headers %}<th>{{ h }}</th>{% endfor %}</tr></thead>
          <tbody>
          {% for r in rows %}
            <tr>{% for v in r %}<td title="{{ v|e }}">{{ v }}</td>{% endfor %}</tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    {% endif %}
  {% endif %}
{% endif %}

<script>
  function showRunningMessage(event, form) {
    document.body.classList.add("is-running");
    const submitter = event.submitter;
    const submitAction = submitter ? submitter.getAttribute("formaction") : "";
    const exportActions = new Set([
      "{{ url_for('export') }}",
      "{{ url_for('export_jsonl') }}",
      "{{ url_for('raw_json_export') }}",
      "{{ url_for('archive_urls') }}"
    ]);
    const isExport = exportActions.has(submitAction);
    const buttons = form.querySelectorAll("button");
    buttons.forEach(function(btn) { btn.disabled = true; });
    if (isExport) {
      setTimeout(function() {
        document.body.classList.remove("is-running");
        buttons.forEach(function(btn) { btn.disabled = false; });
      }, 1600);
    }
    return true;
  }
</script>
""" + LAYOUT_END


def _template_context(**extra: Any) -> dict[str, Any]:
    ctx = {
        "css": BASE_CSS,
        "home_icon": HOME_ICON,
        "year": datetime.now().year,
    }
    ctx.update(extra)
    return ctx


DATA_ACCESS_MODE_OPTIONS = [
    ("official", "Official APIs only"),
    ("approved", "Official + approved research APIs"),
    ("unofficial", "Include unofficial local tools"),
]


def _build_home_sections() -> list[dict[str, Any]]:
    seen_query_keys: set[str] = set()
    sections: list[dict[str, Any]] = []
    for title, entries in HOME_MENU:
        items: list[dict[str, str]] = []
        for key, label, description in entries:
            mod = REGISTRY.get(key)
            if not mod:
                continue
            seen_query_keys.add(key)
            items.append(
                {
                    "label": label,
                    "href": url_for("query_page", qkey=key),
                    "description": description or mod.META.get("description", ""),
                    "source_type": mod.META.get("source_type", "official_api"),
                }
            )
        if items:
            sections.append({"title": title, "entries": items})

    extra_items = []
    for key, mod in _query_options():
        if key in seen_query_keys:
            continue
        extra_items.append(
            {
                "label": mod.META["name"],
                "href": url_for("query_page", qkey=key),
                "description": mod.META.get("description", ""),
                "source_type": mod.META.get("source_type", "official_api"),
            }
        )
    if extra_items:
        sections.append({"title": "Other Modules", "entries": extra_items})
    return sections


def _query_options() -> list[tuple[str, Any]]:
    return sorted(
        REGISTRY.items(),
        key=lambda item: item[1].META.get("name", item[0]).casefold(),
    )


def _module_flag(qkey: str | None, name: str) -> bool:
    return bool(qkey in REGISTRY and getattr(REGISTRY[qkey], name, False))


def _render_home() -> str:
    ensure_registry()
    return render_template_string(
        HOME_HTML,
        **_template_context(title="Social OSINT - Home", qkey=None, registry=REGISTRY, home_sections=_build_home_sections()),
    )


def _render_query(
    qkey: str,
    form: dict[str, Any] | None = None,
    headers: list[str] | None = None,
    rows: list[list[Any]] | None = None,
    error: str | None = None,
    result_actions: str = "",
) -> str:
    ensure_registry()
    custom_results_html = None
    if headers and rows is not None and qkey in REGISTRY and hasattr(REGISTRY[qkey], "render_results"):
        custom_results_html = REGISTRY[qkey].render_results(form or {}, headers, rows)
    return render_template_string(
        QUERY_HTML,
        **_template_context(
            title="Social OSINT - Query Console",
            registry=REGISTRY,
            query_options=_query_options(),
            qkey=qkey,
            form=form,
            headers=headers,
            rows=rows,
            error=error,
            result_actions=result_actions,
            custom_results_html=custom_results_html,
            hide_preview_limit=_module_flag(qkey, "HIDE_PREVIEW_LIMIT"),
            hide_csv_export=_module_flag(qkey, "HIDE_CSV_EXPORT"),
            run_button_label=getattr(REGISTRY[qkey], "RUN_BUTTON_LABEL", "Run Preview") if qkey in REGISTRY else "Run Preview",
            data_access_modes=DATA_ACCESS_MODE_OPTIONS,
            len=len,
        ),
    )


@app.route("/", methods=["GET"])
def home():
    return _render_home()


RESOURCES_HTML = LAYOUT_START + """
<div class="home-title-row">
  <h2>Resources</h2>
  <p class="note">Investigator notes and external public-source tools</p>
</div>

<section class="panel" id="evidence-checklist">
  <h3>Evidence Checklist</h3>
  <p class="subtle">Use the console as triage. For high-value matches, preserve source context before drawing conclusions.</p>
  <ol>
    <li>Save raw API JSON and the normalized CSV/JSONL row.</li>
    <li>Open and save the canonical URL.</li>
    <li>Capture a screenshot with URL bar and timestamp visible where allowed.</li>
    <li>Attempt Wayback or other public archive capture for public web pages.</li>
    <li>Preserve context: profile metadata, surrounding thread/comments, dates, and media.</li>
    <li>Use review labels such as candidate_intervention_review, lobbying_review, and needs_manual_review. Flags are not legal conclusions.</li>
  </ol>
</section>

<section class="panel">
  <h3>Tool Registry</h3>
  <p class="subtle">These links are starting points for manual investigation and corroboration. The app does not scrape tool registries.</p>
  <p>
    <a href="https://bellingcat.gitbook.io/toolkit" target="_blank" rel="noreferrer">Bellingcat Online Investigation Toolkit</a><br>
    <a href="https://adstransparency.google.com/" target="_blank" rel="noreferrer">Google Ads Transparency Center</a><br>
    <a href="https://transparency.meta.com/researchtools/ad-library/" target="_blank" rel="noreferrer">Meta Ad Library</a><br>
    <a href="https://archive.org/web/" target="_blank" rel="noreferrer">Wayback Machine</a><br>
    <a href="https://commoncrawl.org/" target="_blank" rel="noreferrer">Common Crawl</a><br>
    <a href="https://www.gdeltproject.org/" target="_blank" rel="noreferrer">GDELT</a>
  </p>
</section>
""" + LAYOUT_END


@app.route("/resources", methods=["GET"])
def resources():
    return render_template_string(RESOURCES_HTML, **_template_context(title="Social OSINT - Resources"))


@app.route("/evidence-checklist", methods=["GET"])
def evidence_checklist():
    return redirect(url_for("resources") + "#evidence-checklist")


@app.route("/query/<qkey>", methods=["GET"])
def query_page(qkey: str):
    ensure_registry()
    if qkey not in REGISTRY:
        return redirect(url_for("home"))
    return _render_query(qkey, form={}, headers=None, rows=None, error=None)


@app.route("/refresh", methods=["POST"])
def refresh():
    global REGISTRY, PLUGIN_FINGERPRINT
    REGISTRY = load_plugins()
    PLUGIN_FINGERPRINT = plugin_fingerprint()
    return redirect(url_for("home"))


@app.route("/select", methods=["POST"])
def select():
    ensure_registry()
    qkey = request.form.get("qkey")
    if qkey not in REGISTRY:
        return redirect(url_for("home"))
    return redirect(url_for("query_page", qkey=qkey))


@app.route("/run", methods=["GET", "POST"])
def run():
    if request.method == "GET":
        return redirect(url_for("home"))

    ensure_registry()
    qkey = request.form.get("qkey")
    form = request_payload()

    if qkey not in REGISTRY:
        return redirect(url_for("home"))

    error = None
    headers, rows = None, None
    result_actions_html = ""

    try:
        enforce_source_access(REGISTRY[qkey].META, form)
        headers, rows = REGISTRY[qkey].run(form)
        if not _module_flag(qkey, "DISABLE_ROW_LIMIT"):
            try:
                lim = max(1, int(form.get("_limit", "500")))
            except Exception:
                lim = 500
            rows = rows[:lim]
        if hasattr(REGISTRY[qkey], "result_actions"):
            try:
                result_actions_html = REGISTRY[qkey].result_actions(form, headers, rows)
            except Exception:
                result_actions_html = ""
    except Exception:
        error = traceback.format_exc()

    return _render_query(qkey, form=form, headers=headers, rows=rows, error=error, result_actions=result_actions_html)


@app.route("/plugin_action", methods=["POST"])
def plugin_action():
    ensure_registry()
    qkey = request.form.get("qkey")
    if qkey not in REGISTRY:
        return "Unknown query key.", 400
    mod = REGISTRY[qkey]
    if not hasattr(mod, "handle_action"):
        return "This plugin does not support actions.", 400
    try:
        message = mod.handle_action(request.form.to_dict(flat=True))
        return (
            '<!doctype html><title>Action complete</title>'
            '<body style="font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;">'
            '<h2>Action complete</h2><p>' + str(message) + '</p>'
            '<p><a href="/">Return to query console</a></p></body>'
        )
    except Exception:
        return "<pre>" + traceback.format_exc() + "</pre>", 500


@app.route("/export", methods=["GET", "POST"])
def export():
    if request.method == "GET":
        return redirect(url_for("home"))

    ensure_registry()
    qkey = request.form.get("qkey")
    form = request_payload()

    if qkey not in REGISTRY:
        return "Unknown query key.", 400
    enforce_source_access(REGISTRY[qkey].META, form)

    def generate():
        if hasattr(REGISTRY[qkey], "export_headers"):
            headers = REGISTRY[qkey].export_headers(form)
        else:
            headers = getattr(REGISTRY[qkey], "HEADERS", REGISTRY[qkey].META.get("headers", []))

        yield csv_row(headers)
        for row in REGISTRY[qkey].export_rows(form):
            yield csv_row(row)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%MZ")
    base = REGISTRY[qkey].META.get("key", qkey)
    filename = f"{base}_{ts}.csv"
    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _headers_for_export(qkey: str, form: dict[str, Any]) -> list[str]:
    if hasattr(REGISTRY[qkey], "export_headers"):
        return REGISTRY[qkey].export_headers(form)
    return getattr(REGISTRY[qkey], "HEADERS", REGISTRY[qkey].META.get("headers", []))


@app.route("/export_jsonl", methods=["GET", "POST"])
def export_jsonl():
    if request.method == "GET":
        return redirect(url_for("home"))

    ensure_registry()
    qkey = request.form.get("qkey")
    form = request_payload()

    if qkey not in REGISTRY:
        return "Unknown query key.", 400
    enforce_source_access(REGISTRY[qkey].META, form)

    def generate():
        headers = _headers_for_export(qkey, form)
        for row in REGISTRY[qkey].export_rows(form):
            yield json.dumps(dict(zip(headers, row)), ensure_ascii=False) + "\n"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%MZ")
    base = REGISTRY[qkey].META.get("key", qkey)
    filename = f"{base}_{ts}.jsonl"
    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/raw_json_export", methods=["GET", "POST"])
def raw_json_export():
    if request.method == "GET":
        return redirect(url_for("home"))

    ensure_registry()
    qkey = request.form.get("qkey")
    form = request_payload()

    if qkey not in REGISTRY:
        return "Unknown query key.", 400
    enforce_source_access(REGISTRY[qkey].META, form)

    def generate():
        headers = _headers_for_export(qkey, form)
        raw_idx = headers.index("raw_json") if "raw_json" in headers else -1
        for row in REGISTRY[qkey].export_rows(form):
            if raw_idx >= 0 and raw_idx < len(row) and row[raw_idx]:
                raw = str(row[raw_idx])
                yield raw if raw.endswith("\n") else raw + "\n"
            else:
                yield json.dumps(dict(zip(headers, row)), ensure_ascii=False) + "\n"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%MZ")
    base = REGISTRY[qkey].META.get("key", qkey)
    filename = f"{base}_raw_{ts}.jsonl"
    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/archive_urls", methods=["GET", "POST"])
def archive_urls():
    if request.method == "GET":
        return redirect(url_for("home"))

    ensure_registry()
    qkey = request.form.get("qkey")
    form = request_payload()

    if qkey not in REGISTRY:
        return "Unknown query key.", 400
    enforce_source_access(REGISTRY[qkey].META, form)

    url_columns = {
        "canonical_url",
        "tweet_url",
        "post_url",
        "permalink_url",
        "ad_library_public_url",
        "ad_detail_url",
        "video_url",
        "comment_url",
        "url",
        "link",
    }

    def generate():
        headers = _headers_for_export(qkey, form)
        yield csv_row(["plugin_key", "url_column", "url", "notes"])
        for row in REGISTRY[qkey].export_rows(form):
            row_map = dict(zip(headers, row))
            for column, value in row_map.items():
                if column in url_columns and value:
                    yield csv_row([qkey, column, value, "Manual archive/screenshot review recommended."])

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%MZ")
    base = REGISTRY[qkey].META.get("key", qkey)
    filename = f"{base}_archive_urls_{ts}.csv"
    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/health", methods=["GET"])
def health():
    ensure_registry()
    return {"ok": True, "plugins": list(REGISTRY.keys())}


if __name__ == "__main__":
    app.run(debug=True)
