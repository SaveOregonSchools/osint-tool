# Facebook Page Content Integration

This app can collect visible Facebook Page posts and optional top-level comments
through Meta Graph API access.

## Module

### Meta - Facebook Page Posts/Comments

Plugin key: `meta_facebook_page_content_search`

Use this module when you have Page IDs or resolvable Page handles and a Meta
Graph API token with the permissions Meta requires for the target content.

## Requirements

Provide a Meta Graph API access token in one of two ways:

```bash
export META_GRAPH_ACCESS_TOKEN="your_meta_graph_token"
```

or paste the token into the module form for a single request. If
`META_GRAPH_ACCESS_TOKEN` is not set, the module falls back to
`META_AD_LIBRARY_ACCESS_TOKEN`.

Optional:

```bash
export META_GRAPH_API_VERSION="v25.0"
export META_GRAPH_BASE="https://graph.facebook.com"
```

## Search Options

- Facebook Page IDs or handles, one per line.
- Optional local filter terms.
- Since/until dates.
- Maximum posts per Page.
- Maximum top-level comments per post.
- Candidate-intervention, lobbying, and custom term flags.
- Optional status rows for permission/API errors or empty result sets.

## Boundaries

- Organic Facebook Page posts/comments are not collected through the Meta Ad
  Library API.
- Graph API access depends on token type, app review, permissions, Page
  visibility, and the content Meta makes available to the requesting app/user.
- The module does not log into Facebook, scrape private content, bypass access
  controls, or collect hidden/deleted/restricted material.
