from __future__ import annotations

import csv
import importlib
import io
import pkgutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, redirect, render_template_string, request, url_for


app = Flask(__name__)

PLUGIN_PACKAGE = "queries"
PLUGIN_DIR = Path(__file__).parent / PLUGIN_PACKAGE
REGISTRY: dict[str, Any] = {}


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
    global REGISTRY
    if not REGISTRY:
        REGISTRY = load_plugins()


def csv_row(values: list[Any] | tuple[Any, ...]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(values)
    return buf.getvalue()


HTML = """
<!doctype html>
<title>Social OSINT — Query Console</title>
<meta charset="utf-8">
<style>
  :root { --border:#ddd; --muted:#666; --bg:#f6f6f6; --warn:#fff8d6; --err:#ffecec; }
  body { font-family: system-ui, Segoe UI, Arial, sans-serif; max-width: 1320px; margin: 24px auto; padding: 0 16px; }
  h1 { margin-bottom: 0.2rem; }
  h2 { margin-top: 0.6rem; }
  .subtle { color: var(--muted); font-size: 0.95rem; line-height: 1.35; }
  .panel { border:1px solid var(--border); border-radius:8px; padding:12px; background:#fff; margin:12px 0; }
  .row { margin: 10px 0; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px 16px; }
  .toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  label { font-weight: 650; display:block; margin-bottom:3px; }
  input[type="text"], input[type="number"], input[type="date"], select, textarea {
    width:100%; box-sizing:border-box; padding:7px; border:1px solid #bbb; border-radius:6px;
    font-family: inherit; font-size: 14px;
  }
  textarea { min-height: 86px; resize: vertical; }
  button { padding: 8px 11px; border: 1px solid #aaa; border-radius: 6px; background: #f8f8f8; cursor:pointer; }
  button:hover { background: #eee; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 6px; border-bottom: 1px solid #eee; vertical-align: top; }
  thead th { position: sticky; top: 0; background: var(--bg); border-bottom: 1px solid var(--border); z-index: 1; }
  td { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td.wrap, td:nth-child(10), td:nth-child(11), td:nth-child(12) { white-space: pre-wrap; overflow: visible; text-overflow: clip; }
  .results { overflow:auto; max-height:64vh; border:1px solid var(--border); border-radius:8px; }
  .err { background:var(--err); border:1px solid #f5b5b5; padding:10px; white-space:pre-wrap; border-radius:6px; }
  .running-msg { display:none; margin: 10px 0; padding: 10px; background: var(--warn); border: 1px solid #e6d37a; border-radius: 6px; font-weight: 650; }
  body.is-running .running-msg { display: block; }
  body.is-running button { opacity: 0.6; cursor: not-allowed; }
  .pill { display:inline-block; border:1px solid #ccc; border-radius:999px; padding:2px 8px; margin-right:6px; color:#333; background:#fafafa; }
  .notice { background:#f9fbff; border:1px solid #c9d8ff; border-radius:8px; padding:10px; margin:10px 0; }
</style>
<body>
<h1>Social OSINT — Query Console</h1>
<p class="subtle">
  Public-source collection helper for nonprofit social-media review. This app does not log into accounts,
  bypass platform restrictions, or determine legality. Treat flags as leads for review and preserve screenshots/archives separately.
</p>

<div class="notice">
  <span class="pill">MVP target: Bluesky</span>
  <span class="pill">No credentials required</span>
  <span class="pill">CSV export</span>
  <span class="pill">Plugin-based</span>
</div>

<form method="post" action="/select" class="panel">
  <div class="toolbar">
    <label for="qkey" style="margin:0"><b>Query:</b></label>
    <select name="qkey" id="qkey" style="width:auto; min-width: 360px;"
            onchange="this.form.requestSubmit(document.getElementById('loadBtn'))">
      {% for key, mod in registry.items() %}
        <option value="{{ key }}" {% if key == qkey %}selected{% endif %}>{{ mod.META["name"] }}</option>
      {% endfor %}
    </select>
    <button id="loadBtn" type="submit">Load</button>
    <button formaction="/refresh" formmethod="post" type="submit">Refresh Queries</button>
  </div>
</form>

{% if not registry %}
  <div class="err"><b>No plugins loaded.</b> Make sure the queries/ directory exists and contains query modules.</div>
{% endif %}

{% if qkey and qkey in registry %}
  <div class="panel">
    <h2>{{ registry[qkey].META["name"] }}</h2>
    <p class="subtle">{{ registry[qkey].META.get("description","") }}</p>

    <form method="post" action="/run" onsubmit="return showRunningMessage(event, this);">
      <input type="hidden" name="qkey" value="{{ qkey }}">
      {{ registry[qkey].render_fields(form or {}) | safe }}
      <div class="toolbar" style="margin-top:12px;">
        <label style="margin:0">Preview row limit:</label>
        <input type="number" name="_limit" value="{{ (form or {}).get('_limit','500') }}" min="1" style="width:100px">
        <button type="submit">Run Query</button>
        <button formaction="/export" formmethod="post">Export CSV (full result)</button>
      </div>
      <div class="running-msg">Running query. Public APIs can be slow or rate-limited; the result will appear below.</div>
    </form>
  </div>

  {% if error %}
    <div class="err"><b>Error:</b>\n{{ error }}</div>
  {% endif %}

  {% if headers and rows is not none %}
    <p class="subtle">Showing up to <b>{{ (form or {}).get('_limit','500') }}</b> rows. Preview contains <b>{{ len(rows) }}</b> rows.</p>
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

<script>
  function showRunningMessage(event, form) {
    document.body.classList.add("is-running");
    const submitter = event.submitter;
    const isExport = submitter && submitter.getAttribute("formaction") === "/export";
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
</body>
"""


@app.route("/", methods=["GET"])
def home():
    ensure_registry()
    first_key = next(iter(REGISTRY.keys()), None)
    return render_template_string(HTML, registry=REGISTRY, qkey=first_key, form=None, headers=None, rows=None, error=None, len=len)


@app.route("/refresh", methods=["POST"])
def refresh():
    global REGISTRY
    REGISTRY = load_plugins()
    return redirect(url_for("home"))


@app.route("/select", methods=["POST"])
def select():
    ensure_registry()
    qkey = request.form.get("qkey")
    if qkey not in REGISTRY:
        qkey = next(iter(REGISTRY.keys()), None)
    return render_template_string(HTML, registry=REGISTRY, qkey=qkey, form={}, headers=None, rows=None, error=None, len=len)


@app.route("/run", methods=["GET", "POST"])
def run():
    if request.method == "GET":
        return redirect(url_for("home"))
    ensure_registry()
    qkey = request.form.get("qkey")
    form = request.form.to_dict(flat=True)
    if qkey not in REGISTRY:
        return "Unknown query key.", 400

    error = None
    headers, rows = None, None
    try:
        headers, rows = REGISTRY[qkey].run(form)
        try:
            lim = max(1, int(form.get("_limit", "500")))
        except Exception:
            lim = 500
        rows = rows[:lim]
    except Exception:
        error = traceback.format_exc()
    return render_template_string(HTML, registry=REGISTRY, qkey=qkey, form=form, headers=headers, rows=rows, error=error, len=len)


@app.route("/export", methods=["GET", "POST"])
def export():
    if request.method == "GET":
        return redirect(url_for("home"))
    ensure_registry()
    qkey = request.form.get("qkey")
    form = request.form.to_dict(flat=True)
    if qkey not in REGISTRY:
        return "Unknown query key.", 400

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
    return Response(generate(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/health", methods=["GET"])
def health():
    ensure_registry()
    return {"ok": True, "plugins": list(REGISTRY.keys())}


if __name__ == "__main__":
    app.run(debug=True)
