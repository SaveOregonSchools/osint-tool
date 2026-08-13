# Social Media Archive

The **Social Media Archive — Facebook, Instagram & X** module creates local,
high-fidelity WACZ archives with Browsertrix Crawler. It is browser-assisted:
you create a dedicated authenticated browser profile manually, and the module
uses only the session that profile can access. It never asks for or stores a
platform password.

## What It Archives

- Facebook profile, Page, group, post, photo, and reel URLs.
- Instagram profile, post, reel, story, and highlight URLs.
- X account history through generated `from:account` searches.
- Custom X search expressions, including other supported X search operators.

Every Facebook and Instagram input URL gets a separate Browsertrix collection.
X searches can be split into one collection per calendar year. Each collection
has independent page, elapsed-time, and size limits, preventing one target or
year from producing an unbounded archive.

Facebook and Instagram do not expose an equivalent date-bounded feed URL, so
the initial module does not claim to separate those feeds by publication year.
Use individual post URLs or shorter target lists when narrower captures are
required.

## Prerequisites

1. Install and start Docker Desktop.
2. Pull a tested Browsertrix Crawler release:

   ```powershell
   docker pull webrecorder/browsertrix-crawler:VERSION
   ```

   Browsertrix recommends using a published version rather than `latest` for
   production work. Enter that same tagged image name in the module.

3. Open the module and expand **Show local profile-creation instructions**.
4. Run the displayed command, then open `http://localhost:9223/`.
5. In the embedded Browsertrix browser, sign into the authorized Facebook,
   Instagram, and X accounts. Complete any 2FA or challenges manually and click
   **Create Profile**.

The resulting profile is stored under:

```text
data/social_media_archive/profiles/
```

This directory is ignored by Git.

## Recommended Workflow

1. Select **Plan and validate only**.
2. Enter Facebook/Instagram URLs and/or X account handles.
3. For X, select an inclusive start and end date. Keep yearly batching enabled
   for long periods.
4. Run the plan and review every generated target and date period.
5. Select the green **Run Archiving** button below a successful plan. It preserves
   the submitted settings and queues the plan without requiring another dropdown
   change. You can alternatively select **Queue WACZ archives** before submitting.
   Each platform/year batch becomes a persistent background job and the module
   returns immediately.
6. Open **Jobs** in the app header to follow queued, running, completed, and
   failed work. The page refreshes automatically while work is active.
7. Use **Open output folder** beside a finished job to open its run directory
   in Windows Explorer.
8. Replay every WACZ and verify that expected posts, comments, and media were
   captured before treating it as evidence.

For an X range of January 1 through December 31, 2024, the module generates:

```text
from:example since:2024-01-01 until:2025-01-01
```

The module owns the `since:` and `until:` terms to keep yearly batches
non-overlapping. Do not add those two operators to custom X expressions.

## Outputs

Completed runs are stored under:

```text
data/social_media_archive/runs/<run-id>/
```

Each run contains:

- `plan.json`, recording targets and crawl settings;
- `manifest.json`, recording per-batch status and output metadata;
- a log for each Browsertrix batch;
- Browsertrix collection output and WACZ files;
- a SHA-256 hash for every completed WACZ.

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
`size_limit_mb` setting names.
The workflow uses Browsertrix and the saved browser profile only. It does not
read `X_BEARER_TOKEN`, call the X API, or incur X API request charges.
Automation jobs use scrolling and media fetching without the regular archive
module's social-site behavior, so they do not deliberately expand comment
threads. Comments already visible in a platform feed may still appear in the
extracted page text.

For X, the requested lookback is enforced in the generated search using
`since:` and `until:`. Facebook and Instagram profile URLs do not expose a
reliable publication-date cutoff, so their requested window is advisory: the
crawler scrolls the visible feed within its configured time/page limits, and a
later processing step must apply any date information present in the content.
Browsertrix page text is not guaranteed to be one record per post, and the
image bundle can include avatars or interface images along with post graphics.

## Security and Collection Boundaries

- Use an account dedicated to authorized archiving when possible.
- Browser profiles and WACZ files may contain cookies, session tokens,
  personalized content, or identifying account information. Store them as
  sensitive evidence and do not share them casually.
- The module does not bypass CAPTCHA, 2FA, privacy controls, rate limits,
  deleted-content restrictions, or account permissions.
- Browsertrix behaviors can break when platforms change their interfaces.
  Successful process completion does not replace replay-based quality review.
- Confirm that collection is permitted by applicable agreements, policies, and
  law before running an archive.

Browsertrix documentation:

- <https://crawler.docs.browsertrix.com/user-guide/>
- <https://crawler.docs.browsertrix.com/user-guide/browser-profiles/>
- <https://crawler.docs.browsertrix.com/user-guide/behaviors/>
