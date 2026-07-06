# Instagram / Meta Ad Library Integration

This app reviews Instagram-related public advertising through the official Meta
Ad Library API. It does not scrape Instagram profiles, posts, stories, comments,
followers, or private/restricted content.

## Module

### Meta - Ad Library Search (Facebook/Instagram)

Plugin key: `meta_ad_library_search`

Searches Meta's public Ad Library API for ads delivered on Facebook, Instagram,
and optionally Threads. It is best suited for issue, election, political, and
page-based ad monitoring.

## Requirements

You need a Meta developer app token with Ad Library API access. Provide it in
one of two ways:

```bash
export META_AD_LIBRARY_ACCESS_TOKEN="your_meta_ad_library_token"
```

or paste the token into the module form for a single request. The module does
not echo the token back into the rendered page and redacts access tokens from
snapshot URLs before CSV export.

Optional:

```bash
export META_GRAPH_API_VERSION="v25.0"
export META_GRAPH_BASE="https://graph.facebook.com"
```

## Search Options

- Search terms, one per line.
- Optional Facebook Page IDs to limit results to known advertisers/pages.
- Reached country, such as `US`.
- Ad type: political and issue ads, all available ad types, housing,
  employment, or financial products/services.
- Active status: all, active, or inactive.
- Search type: unordered keyword or exact phrase.
- Delivery date minimum/maximum.
- Maximum results per query.
- Publisher platforms: Facebook, Instagram, and/or Threads.
- Candidate-intervention and lobbying-review pattern flags.
- Only flagged rows vs. all API results.
- Optional status rows for errors or empty result sets.

## Output

CSV exports include:

- Source query, flag categories, and matched terms.
- Ad Library ID and public Ad Library URL.
- Page ID, page name, byline, creative text fields, and publisher platforms.
- Creation and delivery dates.
- Spend/impression lower and upper bounds where available.
- Demographic and regional delivery JSON where available.
- Redacted ad snapshot URL, capture timestamp, source API, and notes.

## Suggested Workflow

1. Start with organization, PAC, candidate, issue, ballot-measure, or slogan
   search terms.
2. Use Page IDs when you want cleaner monitoring for known advertisers.
3. Select Instagram placements when you specifically need Instagram-delivered
   ads; keep Facebook enabled when cross-platform delivery matters.
4. Export CSV and preserve important public Ad Library pages separately.

## Boundaries

- This is an Ad Library integration, not an organic Instagram collection tool.
- It does not log into Instagram or Meta accounts.
- It does not bypass platform controls or collect private/restricted content.
- API availability, fields, and access requirements are controlled by Meta.
