# Bluesky Integration

The Bluesky modules use public AT Protocol AppView reads. They do not require a
login for the endpoints currently used by this app.

## Modules

### Bluesky - Profile Lookup / Account Validation

Plugin key: `bluesky_profile_lookup`

Use this first to normalize and validate handles, DIDs, `@handle` values, or
`bsky.app/profile/...` URLs. The export includes public profile metadata such as
handle, DID, display name, description, follower/following counts, post count,
profile URL, capture timestamp, source API, and status notes.

### Bluesky - Scan Account Feeds For Lobbying/Candidate Flags

Plugin key: `bluesky_author_feed_scan`

Fetches public author-feed posts/reposts/replies for one or more accounts, then
applies local review patterns and custom terms. Useful when you already know the
accounts to review.

Options include:

- Since/until dates.
- Maximum posts per actor.
- Feed filter: posts with replies, posts without replies, or posts with media.
- Candidate-intervention review patterns.
- Lobbying review patterns.
- Custom terms, names, bill numbers, slogans, ballot measures, or hashtags.
- Optional status rows for accounts with errors or no matches.
- Only flagged rows vs. all fetched rows.

### Bluesky - API Keyword Search By Account

Plugin key: `bluesky_keyword_search`

Runs `app.bsky.feed.searchPosts` searches for user-entered terms. Searches can
be broad across Bluesky or limited to one or more handles/DIDs. The module
deduplicates posts by URI and applies the same local flagging patterns used by
the feed scanner.

## Suggested Workflow

1. Run profile lookup to confirm account identifiers.
2. Run account feed scans for known organizations, leaders, or related accounts.
3. Run keyword searches for candidate names, bill numbers, campaign slogans,
   ballot measures, hashtags, or issue terms.
4. Export CSV rows and preserve screenshots/archives separately for important
   evidence.

## Output

Feed and keyword-search exports use the shared post evidence columns, including:

- Platform, actor input, query, flag categories, and matched terms.
- Created/indexed timestamps.
- Author handle, DID, display name, and post URL.
- Post text, reply/repost indicators, engagement counts, and embed summaries.
- AT Protocol URI/CID, root/parent URIs, labels, capture timestamp, source API,
  and notes.

## Endpoints Used

- `app.bsky.actor.getProfile`
- `app.bsky.feed.getAuthorFeed`
- `app.bsky.feed.searchPosts`

## Configuration

```bash
export BSKY_APPVIEW_BASE="https://public.api.bsky.app"
export OSINT_HTTP_TIMEOUT="30"
export OSINT_REQUEST_DELAY="0.2"
export OSINT_USER_AGENT="your-org-social-osint-console/0.1"
```

If an endpoint returns a 401, 403, 429, or other error, the plugin either
surfaces it in the UI or adds a status row when that option is enabled.
