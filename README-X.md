# X Integration

This app can search recent public X posts through the official X API v2 recent
search endpoint.

## Module

### X - Recent Public Post Search

Plugin key: `x_recent_search`

Use this module for keyword searches, account-limited searches, reply/thread
collection by conversation ID, and exports of public post metadata returned by
your X developer access tier.

## Requirements

Provide an X API bearer token in one of two ways:

```bash
export X_BEARER_TOKEN="your_x_api_bearer_token"
```

or paste the token into the module form for a single request. The module does
not echo the token back into the rendered page.

Optional:

```bash
export X_API_BASE="https://api.x.com/2"
```

## Search Options

- Raw X search queries, one per line.
- Generated searches from terms, usernames, and/or a conversation ID.
- Optional language filter.
- Optional start/end timestamps in API format, such as
  `2026-07-01T00:00:00Z`.
- Include or exclude replies.
- Include or exclude reposts/retweets.
- Candidate-intervention, lobbying, and custom term flags.
- Optional status rows for errors or empty result sets.

## Boundaries

- X controls API availability, query operators, rate limits, and historical
  depth by developer account/tier.
- This module does not log into X, scrape pages, bypass platform controls, or
  collect private/restricted content.
- Conversation/reply collection depends on the `conversation_id` search
  operator being available to your account.
