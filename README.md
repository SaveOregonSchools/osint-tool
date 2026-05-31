# Social OSINT Query Console — Bluesky MVP

A small Flask app for public-source social-media review of nonprofits and leaders. It follows the same plugin-console pattern as the IRS 990 query app: each module in `queries/` exposes `META`, `render_fields()`, `run()`, and `export_rows()`.

The first target platform is **Bluesky** because its public AT Protocol reads are straightforward and do not require credentials for the endpoints used here.

## What this MVP does

- Validates Bluesky handles/DIDs with public profile lookups.
- Scans one or more public Bluesky author feeds for posts/reposts/replies.
- Runs targeted keyword searches, optionally limited to specific accounts.
- Flags posts for review using local keyword/regex patterns for:
  - candidate-intervention review terms, such as `vote for`, `endorse`, `defeat`, `campaign`, `donate to candidate`;
  - lobbying review terms, such as `contact lawmakers`, `support/oppose bill`, `HB 1234`, `ballot measure`, `vote yes/no on`;
  - custom terms, names, handles, slogans, bills, or ballot measures you enter.
- Exports CSV evidence logs with post URLs, text, timestamps, matched terms, basic engagement counts, embed summaries, and source API endpoint.

## What it does not do

- It does not log into social accounts.
- It does not bypass platform access controls, rate limits, or privacy settings.
- It does not collect deleted, private, or restricted content.
- It does not determine whether conduct is illegal. Flags are leads for legal/compliance review.
- It does not replace screenshots or web archives. For serious evidence preservation, save screenshots with URL/date/context and archive pages where possible.

## Install

```bash
cd social_osint_console
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

## Suggested workflow

1. Use **Bluesky — profile lookup / account validation** to confirm handles and DIDs.
2. Use **Bluesky — scan account feeds for lobbying/candidate flags** to scan recent posts/reposts/replies for selected accounts.
3. Use **Bluesky — API keyword search by account** for specific candidate names, bill numbers, hashtags, slogans, or ballot measures.
4. Export CSV, then manually preserve screenshots/archives for high-value rows.

## Optional environment variables

```bash
# Default is https://public.api.bsky.app
export BSKY_APPVIEW_BASE="https://public.api.bsky.app"

# Default request timeout is 30 seconds
export OSINT_HTTP_TIMEOUT="30"

# Default delay between paged API calls is 0.2 seconds
export OSINT_REQUEST_DELAY="0.2"

# Optional custom user agent
export OSINT_USER_AGENT="your-org-social-osint-console/0.1"
```

## Adding another platform/API plugin

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

The main app will load it automatically after you click **Refresh Queries**.

## Notes on Bluesky endpoints used

- `app.bsky.actor.getProfile`
- `app.bsky.feed.getAuthorFeed`
- `app.bsky.feed.searchPosts`

The app uses public AppView reads. If an endpoint returns a 401/403/429/other error, the plugin either surfaces it in the UI or adds a status row when that option is enabled.
