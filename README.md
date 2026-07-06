# Social OSINT Query Console

A small Flask app for public-source social-media and ad-library review. The app
is a plugin console: each module in `queries/` defines its own input form,
collection logic, preview rows, and CSV export.

The current integrations cover:

- Bluesky public profile, feed, and keyword-search review.
- X recent public post search through the official X API.
- Facebook Page post and top-level comment collection through the Meta Graph
  API, where your token has the required permissions.
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
- [Facebook Page content integration](README-Facebook.md)
- [Instagram / Meta Ad Library integration](README-Instagram.md)
- [LinkedIn integration](README-LinkedIn.md)
- [X integration](README-X.md)

## Current Expansion Notes

The most practical next modules fall into three buckets:

- Official API collectors: X recent/all search, Facebook Page posts/comments,
  Meta Ad Library, Bluesky search/feeds, Reddit, YouTube Data API, Google
  Custom Search, and Common Crawl/GDELT-style web/news discovery.
- Browser-assisted collectors: LinkedIn-style workflows for content visible to
  a browser session you control, useful when a platform has no general public
  search API but normal manual review is allowed.
- Enrichment and evidence helpers: URL expansion, screenshot/archive workflows,
  duplicate clustering, account identity matching, saved-search watchlists, and
  export bundles for review packets.

Important platform caveats:

- Facebook organic posts/comments are not like the Ad Library. The new Page
  content module uses Graph API Page/post/comment endpoints, and access depends
  on Meta app review, token type, Page visibility, and permissions.
- Instagram arbitrary public organic post/comment search is limited through
  official APIs. Instagram Graph API workflows are strongest for owned or
  authorized Business/Creator accounts, media IDs, mentions, and comment
  management, not broad scraping of public Instagram.
- X search requires X developer access and a bearer token. Query operators and
  historical depth depend on the access tier.
- Browser-assisted modules should keep the same boundary as the LinkedIn
  modules: user-controlled login, visible pages only, no CAPTCHA/2FA bypass, and
  no stored credentials.

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

# Required for Facebook Page content unless pasted into the module form.
# Falls back to META_AD_LIBRARY_ACCESS_TOKEN if this is not set.
export META_GRAPH_ACCESS_TOKEN="your_meta_graph_token"

# Default is v25.0
export META_GRAPH_API_VERSION="v25.0"

# Required for X recent search unless pasted into the module form
export X_BEARER_TOKEN="your_x_api_bearer_token"

# Default is https://api.x.com/2
export X_API_BASE="https://api.x.com/2"
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
