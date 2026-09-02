# Social Media Archive

The **Social Media Archive — Facebook, Instagram & X** module creates local,
high-fidelity WACZ archives with Browsertrix Crawler. It is browser-assisted:
you create a dedicated authenticated browser profile manually, and the module
uses only the session that profile can access. It never asks for or stores a
platform password.

## What It Archives

- Facebook profile, Page, and group timelines using the Historical posts preset.
- Instagram profile, post, reel, story, and highlight URLs.
- X account history through generated `from:account` searches.
- Custom X search expressions, including other supported X search operators.

Every Facebook and Instagram input URL gets a separate Browsertrix collection.
X searches are split into one collection per calendar quarter by default, with
monthly, yearly, and single-range options. Each collection
has independent page, elapsed-time, and size limits, preventing one target or
period from producing an unbounded archive.

Facebook and X use local custom Browsertrix behaviors optimized for posts and
attached images. They do not queue individual post pages, comments, reactions,
photo-grid pages, reels, or videos. Video CDN requests are blocked, autoplay is
disabled, and final screenshots are off by default. Facebook post text is
expanded when a visible **See more** control is present. Both behaviors track
unique post IDs and stop after repeated scrolls yield no new posts.

Facebook and Instagram do not expose an equivalent date-bounded feed URL. The
module reports the oldest and newest structured Facebook posts it observed, but
does not treat that range as proof that Facebook returned the complete history.

## Prerequisites

1. Install and start Docker Engine on Linux or Docker Desktop on Windows.
   The account running the Flask service must be able to use the Docker daemon
   and write under `data/social_media_archive/`.
2. Pull a tested Browsertrix Crawler release:

   ```powershell
   docker pull webrecorder/browsertrix-crawler:VERSION
   ```

   Browsertrix recommends using a published version rather than `latest` for
   production work. Enter that same tagged image name in the module. Browsertrix
   1.14.3 is the default for X capture. Version 1.13.2 is also supported as a
   compatibility option for diagnosing the false page-rate-limit behavior seen
   with some 1.14 crawls; older tagged releases are rejected.

3. Open the module and expand **Show local profile-creation instructions**.
4. Run the displayed host-appropriate command, then open
   `http://127.0.0.1:9223/`. The generated command binds Browsertrix's profile
   ports to loopback, not the server's public interfaces.
5. In the embedded Browsertrix browser, sign into the authorized Facebook,
   Instagram, and X accounts. Complete any 2FA or challenges manually and click
   **Create Profile**.

The resulting profile is stored under:

```text
data/social_media_archive/profiles/
```

This directory is ignored by Git.

On a remote Linux server such as `brad`, leave those ports bound to loopback and
tunnel them to the workstation before opening the Browsertrix interface:

```bash
ssh -N \
  -L 9223:127.0.0.1:9223 \
  -L 6080:127.0.0.1:6080 \
  <user>@brad
```

Do not expose ports 6080 or 9223 publicly. They provide interactive access to
the profile browser.

## X Crawler Tuning

The X-specific Browsertrix controls can be changed in `.env` without editing
application code:

```dotenv
OSINT_X_RATE_LIMIT_MAX_RETRIES=4
OSINT_X_RATE_LIMIT_INTERRUPT_COUNT=-1
OSINT_X_POST_LOAD_DELAY_SECONDS=10
```

For Browsertrix 1.14 or newer, the defaults allow four retries and disable the
immediate rate-limit-count interrupt. The application omits those unsupported
rate-limit options when using the 1.13.2 compatibility path. Both paths wait 10
seconds after the search page loads before running behaviors. The accepted
ranges are `-1` through `20` retries, `-1` through `1000` matches before
interruption, and `0` through `600` seconds of post-load delay. Restart the
application after changing `.env`. The effective values are copied into each
queued job, its crawl plan, and its manifest, so jobs already in the queue keep
the configuration with which they were submitted.

## X Authentication Preflight

Every X capture begins with a small, temporary Browsertrix preflight crawl against
`https://x.com/settings/account`. It does not open `SearchTimeline`, so it does
not consume the 50-request search window observed in the August runs. The
preflight:

- loads the selected saved profile read-only;
- requires X's authenticated profile-navigation and account-switcher elements;
- extracts only a safe account handle, never cookie or authorization values;
- optionally compares that handle with **Expected logged-in X account**;
- asks Browsertrix to save a refreshed candidate profile, validates the
  candidate tarball, and atomically promotes it only after authentication is
  positively verified;
- treats a missing, unsafe, or unpromotable refreshed profile as unverified;
- retries one indeterminate browser/network result, then fails closed; and
- performs a new positive browser check for every X job. Logged-out and other
  failed checks may be cached only while the selected profile file is unchanged.

The final WACZ is also checked for positive authenticated-request indicators as
well as guest or login-page evidence. A WACZ with missing, guest-only, or
otherwise ambiguous authentication evidence fails closed even if its preflight
passed. Only header and cookie *names* and boolean/count results are written to
JSON; credential values are never copied into the preflight result.

If X clearly reports a logged-out session or the wrong expected account, no
archive crawl starts. The failed job includes a loopback-only command that uses
Browsertrix's supported `create-login-profile --profile OLD --filename NEW`
flow. On a typical `/opt/osint-tool` Linux installation it is equivalent to:

```bash
mkdir -p /opt/osint-tool/data/social_media_archive/profiles
docker run --rm -it \
  -p 127.0.0.1:6080:6080 \
  -p 127.0.0.1:9223:9223 \
  -v /opt/osint-tool/data/social_media_archive/profiles:/crawls \
  -v /opt/osint-tool/data/social_media_archive/profiles/social-auth.tar.gz:/profile/old-profile.tar.gz:ro \
  webrecorder/browsertrix-crawler:1.14.3 create-login-profile \
  --url https://x.com/settings/account \
  --filename /crawls/social-auth-reauth.tar.gz \
  --profile /profile/old-profile.tar.gz
```

Tunnel the ports, open `http://127.0.0.1:9223/`, complete any login, CAPTCHA, or
2FA manually, verify the account, and click **Create Profile**. Then select the
new filename in the archive module and rerun. The background worker deliberately
does not launch an interactive login or accept X credentials: doing so could
hang on 2FA and could expose an unattended browser service.

## Recommended Workflow

1. Select **Plan and validate only**.
2. Enter Facebook/Instagram URLs and/or X account handles.
3. For X, select an inclusive start and end date. Keep **Quarterly** batching
   for the first run. Use **Monthly** for a busy account or to rerun only a
   quarter that was marked partial because of rate limiting.
4. Run the plan and review every generated target and date period.
5. Select the green **Run Archiving** button below a successful plan. It preserves
   the submitted settings and queues the plan without requiring another dropdown
   change. You can alternatively select **Queue WACZ archives** before submitting.
   Each platform/year batch becomes a persistent background job and the module
   returns immediately.
6. Open **Jobs** in the app header to follow queued, running, completed, and
   failed work. The page reports structured post count, oldest/newest observed
   dates, best-effort coverage status, and the verified X session state/account. When
   needed, expands to show exact reauthentication and SSH-tunnel instructions.
   It refreshes automatically while work is active. If X
   returns a rate limit before any timeline data, the job and other X jobs using
   the same saved profile are deferred until the recorded reset time and retried
   up to three times. Other queued platforms can continue while X is deferred.
7. Use **Open output folder** beside a finished job to open its run directory
   in Windows Explorer.
8. Replay every WACZ and verify that expected posts, text, and images were
   captured before treating it as evidence.

For an X range of January 1 through December 31, 2024, the module generates:

```text
from:example since:2024-01-01 until:2025-01-01
```

The module owns the `since:` and `until:` terms to keep all batches
non-overlapping. Do not add those two operators to custom X expressions.

The optimized defaults are 900 seconds of behavior time, 1,500 seconds total,
10 browser pages, a 512 MB WACZ limit, final text enabled, final screenshots
disabled, content-check failures enabled, and working-file retention disabled.
Because the Facebook and X presets scroll one search/timeline page instead of
queuing comments and media pages, the page limit is a safety ceiling rather
than a collection-depth target.

## Outputs

Capture attempts are stored under:

```text
data/social_media_archive/runs/<run-id>/
```

By default, every attempt that produced a structurally valid WACZ contains:

- `plan.json`, recording targets and crawl settings;
- `manifest.json`, recording per-batch status, validation, crawler command and
  return code, retry provenance, compaction details, and output metadata;
- one self-contained WACZ file;
- a SHA-256 hash for the WACZ in the manifest; and
- `content/content.json` plus `content/posts.csv` when structured posts were
  found, with attached post images copied under `content/media/`.

For X, structured extraction prefers the complete long-form Note Tweet text
over the shortened legacy text and filters account searches to the requested
`from:` handle. For Facebook, it extracts target-authored Story records returned
for the requested Page/profile timeline. The manifest and Jobs page record the
post count, oldest/newest observed timestamps, and a completeness status. A
status of `best_effort_no_errors_observed` means the crawler saw no explicit
error; it does not prove the platform exposed every historical post.

An X attempt stopped by authentication preflight contains only `plan.json` and
`manifest.json`; no archive WACZ or redundant crawler workspace is created.

This compact layout also applies to partial and rate-limited attempts, avoiding
a fresh copied profile and raw-WARC tree for every retry. Before cleanup, the
module verifies the WACZ package manifest and its digest when present, every
declared member's size and SHA-256, the ZIP members, and the inner gzip streams.
It writes the
capture manifest before removing Browsertrix's external working copies of the
WARC, indexes, page records, crawler log, downloaded browser profile, and
expanded profile. Those records are packaged inside the WACZ where applicable,
and the saved login profile remains in the module's `profiles/` directory.

If the WACZ is missing or fails integrity validation, cleanup is refused and the
working files and wrapper log are retained for diagnosis. Select **Retain
Browsertrix working files** to keep them for every attempt. If filesystem locks
or permissions prevent cleanup, the job is marked failed rather than claiming
that the compact output contract was met.

If X returns a rate limit after some timeline pages were captured, the WACZ and
any extracted posts are preserved but marked partial/failed. The module does not
blindly restart a busy date range from the top because that can exhaust the same
quota repeatedly. Rerun only the affected period with **Monthly** batching after
the shared throttle clears.

Queue metadata is stored in `data/job_queue.sqlite`, which is also ignored by
Git. Jobs are run serially so multiple Browsertrix containers do not compete for
the authenticated browser profile or workstation resources. You may leave the
archive module, submit more work, or use other pages while the queue runs.

## n8n Profile Review API

Set a long random `OSINT_AUTOMATION_API_TOKEN` in the environment used to start
the app. n8n must send it as `Authorization: Bearer <token>`.

Submit one profile review with:

```http
POST /api/v1/social-profile-jobs
Content-Type: application/json
Authorization: Bearer <token>

{
  "platform": "x",
  "profile": "example_account",
  "lookback": {"value": 6, "unit": "weeks"},
  "profile_filename": "social-auth.tar.gz"
}
```

The response is HTTP 202 and includes `job_id`, `status_url`, and `content_url`.
Poll `GET <status_url>` with the same Bearer token until the status is
`completed` or `failed`. Once completed, `GET <content_url>` returns extracted
page text and a media manifest. Each media item has a token-protected
`download_url` for the captured image file.

These API submissions use the same persistent serial queue as jobs created in
the browser UI. They force screenshots off and text extraction on. Optional
limits can be supplied in an `options` object using the existing
`behavior_timeout_seconds`, `time_limit_seconds`, `page_limit`, and
`size_limit_mb` setting names. Supply `expected_x_session_handle` in `options`
for X automation when the job must verify a particular logged-in crawler
account before collection.
The workflow uses Browsertrix and the saved browser profile only. It does not
read `X_BEARER_TOKEN`, call the X API, or incur X API request charges.
Automation jobs use the same posts-and-attached-images behaviors as regular
Facebook and X archives and do not deliberately expand comment threads.

For X, the requested lookback is enforced in the generated search using
`since:` and `until:`. Facebook and Instagram profile URLs do not expose a
reliable publication-date cutoff, so their requested window is advisory: the
crawler scrolls the visible feed within its configured time/page limits, and a
later processing step must apply any date information present in the content.
Browsertrix page text remains a replay/QA fallback. When platform response
formats are recognized, `posts.csv` and the `posts` array in `content.json`
provide one deduplicated record per post and media copying is restricted to
image URLs associated with those records.

## Security and Collection Boundaries

- Use an account dedicated to authorized archiving when possible.
- Browser profiles and WACZ files may contain cookies, session tokens,
  personalized content, or identifying account information. Store them as
  sensitive evidence and do not share them casually.
- The module does not bypass CAPTCHA, 2FA, privacy controls, rate limits,
  deleted-content restrictions, or account permissions.
- Browsertrix behaviors can break when platforms change their interfaces.
  Successful process completion does not replace replay-based quality review.
- Profile-preflight working data is temporary and deleted after inspection. A
  verified refreshed profile replaces the selected saved profile atomically;
  logged-out, wrong-account, invalid, or interrupted candidates are discarded.
- Confirm that collection is permitted by applicable agreements, policies, and
  law before running an archive.

Browsertrix documentation:

- <https://crawler.docs.browsertrix.com/user-guide/>
- <https://crawler.docs.browsertrix.com/user-guide/browser-profiles/>
- <https://crawler.docs.browsertrix.com/user-guide/behaviors/>
- <https://crawler.docs.browsertrix.com/user-guide/cli-options/>
- <https://crawler.docs.browsertrix.com/user-guide/rate-limits/>
