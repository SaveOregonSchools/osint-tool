# Social OSINT Query Console

A small Flask app for public-source social-media and ad-library review. The app
is a plugin console: each module in `queries/` defines its own input form,
collection logic, preview rows, and CSV export.

The current integrations cover:

- Bluesky public profile, feed, and keyword-search review.
- Meta Ad Library public ad search for Facebook, Instagram, and Threads
  placements.
- Browser-assisted LinkedIn profile/evidence capture for pages visible to a
  logged-in browser session you control.

## Core App Capabilities

- Loads query modules dynamically from `queries/`.
- Provides a browser UI for selecting a module, entering query settings, running
  a preview, and exporting the full result to CSV.
- Supports text inputs and CSV uploads for file-aware plugins.
- Applies shared candidate-intervention and lobbying-review keyword/pattern
  flags where plugins use the common classifier.
- Keeps local data, evidence captures, browser sessions, exports, and caches out
  of Git.
- Exposes `/health` for a simple app/plugin smoke check.

## Integration Guides

- [Bluesky integration](README-Bluesky.md)
- [Instagram / Meta Ad Library integration](README-Instagram.md)
- [LinkedIn integration](README-LinkedIn.md)

## What This App Does Not Do

- It does not bypass platform access controls, rate limits, login challenges,
  CAPTCHA, 2FA, or privacy settings.
- It does not collect deleted, private, or restricted content.
- It does not determine whether conduct is illegal. Flags are leads for
  human/legal/compliance review.
- It does not replace screenshots, archives, or other evidence-preservation
  steps for high-value findings.

## Install

```bash
cd osint-tool
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

For development and tests, install the dev requirements instead:

```bash
pip install -r requirements-dev.txt
pytest
```

## Optional Environment Variables

```bash
# Default is https://public.api.bsky.app
export BSKY_APPVIEW_BASE="https://public.api.bsky.app"

# Default request timeout is 30 seconds
export OSINT_HTTP_TIMEOUT="30"

# Default delay between paged API calls is 0.2 seconds
export OSINT_REQUEST_DELAY="0.2"

# Optional custom user agent
export OSINT_USER_AGENT="your-org-social-osint-console/0.1"

# Required for Meta Ad Library unless pasted into the module form
export META_AD_LIBRARY_ACCESS_TOKEN="your_meta_ad_library_token"

# Default is v25.0
export META_GRAPH_API_VERSION="v25.0"
```

On Windows PowerShell, use `$env:NAME="value"` or put these values in `.env`.

## Plugin Contract

Create a new Python file in `queries/` with this shape:

```python
META = {
    "key": "unique_plugin_key",
    "name": "Human-readable query name",
    "description": "What the query does",
    "headers": ["col1", "col2"],
}

HEADERS = META["headers"]

def render_fields(form):
    return """<div class='row'><label>Input</label><textarea name='input'></textarea></div>"""

def run(form):
    rows = list(export_rows(form))
    return HEADERS, rows

def export_headers(form):
    return HEADERS

def export_rows(form):
    yield ["value1", "value2"]
```

The main app will load the module automatically after startup or after clicking
**Refresh Queries**.

Plugins can optionally define:

- `result_actions(form, headers, rows)` to render follow-up controls after a
  preview run.
- `handle_action(form)` to process those follow-up controls through
  `/plugin_action`.

## Development

Run checks before committing:

```bash
pytest
python -m compileall app.py common.py queries tests
```

The GitHub Actions workflow in `.github/workflows/ci.yml` runs pytest and a
Python compile check on pushes and pull requests.
