# Social OSINT Query Console

A small Flask app for public-source social-media and ad-library review. The app
is a plugin console: each module in `queries/` defines its own input form,
collection logic, preview rows, and CSV export.

The current integrations cover:

- Bluesky public profile, feed, and keyword-search review.
- X recent public post search through the official X API.
- X full-archive, user lookup, user timeline, and conversation/reply searches
  where your X access tier allows them.
- YouTube Data API channel, keyword, and video-comment scans.
- Google Political Ads Transparency Report queries through BigQuery, plus a
  manual Google Ads Transparency Center link builder.
- Facebook Page post and top-level comment collection through the Meta Graph
  API, where your token has the required permissions.
- Meta Ad Library public ad search for Facebook, Instagram, and Threads
  placements, including an enhanced Page-ID/keyword watch module.
- Controlled-access TikTok Research API, TikTok Commercial Content API, and Meta
  Content Library adapter modules that are explicit about approval requirements.
- Public archive/enrichment modules for Wayback, GDELT, Common Crawl, Open
  Measures, and SpiderFoot.
- Optional Instaloader local runner support, disabled by default.
- Browser-assisted LinkedIn profile/evidence capture for pages visible to a
  logged-in browser session you control.

## Core App Capabilities

- Loads query modules dynamically from `queries/`.
- Provides a browser UI for selecting a module, entering query settings, running
  a preview, and exporting the full result to CSV.
- Supports text inputs and CSV uploads for file-aware plugins.
- Applies shared candidate-intervention and lobbying-review keyword/pattern
  flags where plugins use the common classifier.
- Stores normalized query-run/item metadata in a local SQLite cache when the
  expanded normalized plugins run.
- Shows source-type badges, per-plugin limitations, and a data-access mode so
  controlled or unofficial sources are not accidentally treated as ordinary
  official API modules.
- Exports CSV, JSONL, raw JSONL, and URL archive worklists.
- Keeps local data, evidence captures, browser sessions, exports, and caches out
  of Git.
- Exposes `/health` for a simple app/plugin smoke check.
- Exposes `/resources` for the evidence checklist and investigator tool links.

The header includes the Save Oregon Schools logo linking to
`https://www.saveoregonschools.com/`, and the footer includes the Save Oregon
Schools copyright, source code, and license links.

## Integration Guides

- [Bluesky integration](README-Bluesky.md)
- [Facebook Page content integration](README-Facebook.md)
- [Instagram / Meta Ad Library integration](README-Instagram.md)
- [LinkedIn integration](README-LinkedIn.md)
- [X integration](README-X.md)

## Current Expansion Notes

The most practical next modules fall into three buckets:

The expansion modules are intentionally grouped by source risk:

- Official or public APIs: YouTube Data API, Google Political Ads BigQuery, Meta
  Ad Library, X API, Bluesky, GDELT, Wayback, and Common Crawl.
- Approved research APIs: TikTok Research, TikTok Commercial Content, and Meta
  Content Library modules require project approval and credentials before use.
- Third-party enrichment: Open Measures and SpiderFoot are discovery sources,
  not substitutes for official platform APIs.
- Optional unofficial local tools: Instaloader is disabled unless
  `ALLOW_UNOFFICIAL_SCRAPERS=true` and should be used only for low-volume,
  authorized public capture.

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

On Windows PowerShell, the project virtual environment can run checks with:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m compileall app.py common.py osint_common.py providers queries tests
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

# Local SQLite cache/evidence database. The default is data/osint_cache.db.
export OSINT_DB_PATH="C:\Projects\osint-tool\data\osint_cache.db"

# official, approved, or unofficial
export OSINT_DATA_ACCESS_MODE="official"

# Google / YouTube
export GOOGLE_CLOUD_PROJECT="your_google_cloud_project"
export GOOGLE_APPLICATION_CREDENTIALS="C:\path\to\service-account.json"
export YOUTUBE_API_KEY="your_youtube_api_key"

# TikTok controlled-access APIs
export TIKTOK_RESEARCH_CLIENT_KEY="your_tiktok_research_client_key"
export TIKTOK_RESEARCH_CLIENT_SECRET="your_tiktok_research_client_secret"
export TIKTOK_COMMERCIAL_CLIENT_KEY="your_tiktok_commercial_client_key"
export TIKTOK_COMMERCIAL_CLIENT_SECRET="your_tiktok_commercial_client_secret"

# Required for Meta Ad Library unless pasted into the module form
export META_ACCESS_TOKEN="your_meta_access_token"
export META_AD_LIBRARY_ACCESS_TOKEN="your_meta_ad_library_token"

# Required for Facebook Page content unless pasted into the module form.
# Falls back to META_AD_LIBRARY_ACCESS_TOKEN if this is not set.
export META_GRAPH_ACCESS_TOKEN="your_meta_graph_token"

# Default is v25.0
export META_GRAPH_API_VERSION="v25.0"
export META_CONTENT_LIBRARY_ENABLED="false"

# Required for X recent search unless pasted into the module form
export X_BEARER_TOKEN="your_x_api_bearer_token"

# Default is https://api.x.com/2
export X_API_BASE="https://api.x.com/2"

# Third-party enrichment / local tools
export OPENMEASURES_API_KEY="your_openmeasures_api_key"
export SPIDERFOOT_BASE_URL="http://127.0.0.1:5001"
export SPIDERFOOT_API_KEY="your_spiderfoot_api_key"
export INSTALOADER_SESSION_DIR="C:\Projects\osint-tool\private_sessions"
export ALLOW_UNOFFICIAL_SCRAPERS="false"
```

On Windows PowerShell, use `$env:NAME="value"` or put these values in `.env`.

Google Political Ads BigQuery requires Google Cloud BigQuery client libraries in
the Python environment. The connector fails with a clear setup message if
`google-cloud-bigquery` is not installed.

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
python -m compileall app.py common.py osint_common.py providers queries tests
```

The GitHub Actions workflow in `.github/workflows/ci.yml` runs pytest and a
Python compile check on pushes and pull requests.

## License

The Social OSINT Query Console's software code is copyright (C) 2026 Save
Oregon Schools, LLC and is licensed under the GNU Affero General Public License
version 3. See `LICENSE` for the full license text.

The Social OSINT Query Console is distributed without any warranty; without even
the implied warranty of merchantability or fitness for a particular purpose.

The Save Oregon Schools name, logo, and related branding are not licensed for
reuse under the GNU Affero General Public License. See `TRADEMARKS.md` for the
project's trademark and branding notice.
