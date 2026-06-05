# LinkedIn Profile Collector v1

This is a first-version, browser-assisted LinkedIn profile collection module for the Flask query console pattern used in your IRS 990 / OSINT tools.

It is intentionally designed to avoid storing your LinkedIn password or bypassing LinkedIn security checks. A normal browser window opens, you log in directly on LinkedIn, and you complete any 2FA/challenge manually. The collector then visits the profile URLs from your CSV and saves visible profile information, screenshots, and HTML snapshots.

## What it collects

Best-effort fields:

- Input name
- LinkedIn URL
- Visible profile name
- Headline
- Location
- Current title/company, inferred from Experience
- Experience section as JSON
- Education section as JSON
- Licenses/certifications section as JSON
- Volunteer section as JSON
- Full visible page text
- Screenshot path
- HTML snapshot path
- Status/error message
- Collection timestamp

LinkedIn changes its page markup often, so the structured extraction is intentionally conservative. The saved screenshot, HTML, and full visible text are the evidentiary fallback.

## Files included

```text
queries/linkedin_profile_collector_v1.py   Drop-in query plugin
app_upload_patch.py                        Small patch needed for CSV upload support
requirements_linkedin.txt                  Additional dependency
sample_linkedin_targets.csv                Example input CSV
```

## Install

From the root of your existing Flask query-console project:

```powershell
pip install -r requirements_linkedin.txt
python -m playwright install chromium
```

Then copy:

```text
queries/linkedin_profile_collector_v1.py
```

into your existing app's `queries` folder.

Also apply the small `app.py` patch described in `app_upload_patch.py`. The key change is that the query form needs:

```html
<form method="post" action="/run" enctype="multipart/form-data" onsubmit="return showRunningMessage(event, this);">
```

and `/run` should use the included `form_with_uploads(request)` helper instead of only `request.form.to_dict(flat=True)`.

## CSV format

Preferred columns:

```csv
person_name,linkedin_url,notes
Jane Smith,https://www.linkedin.com/in/jane-smith-12345/,PFL steering committee
John Doe,https://www.linkedin.com/in/johndoe/,Board member
```

Also accepted column names:

- Name: `person_name`, `name`, `person`, `full_name`
- URL: `linkedin_url`, `profile_url`, `url`, `link`, `hyperlink`
- Notes: `notes`, `note`, `context`

## First run workflow

1. Start your Flask app.
2. Select **LinkedIn Profile Collector v1**.
3. Upload your CSV or paste CSV text.
4. Set a small Max Profiles number for the first test, such as 2 or 3.
5. Click **Run Query**.
6. A visible Chromium browser window opens.
7. Log into LinkedIn manually and complete any 2FA/challenge.
8. The module proceeds through the profile list.
9. Results are stored in:

```text
data/linkedin_profiles.db
data/linkedin_snapshots/
```

The browser session is stored in:

```text
data/linkedin_browser_profile/
```

That means you usually do not need to log in again every run unless LinkedIn expires the session.

## Recommended settings

Start with:

```text
Max profiles: 3
Delay between profiles: 8-15 seconds
Login wait: 10 minutes
Headless: No
Save HTML: Yes
Save screenshots: Yes
```

For larger batches, keep the delay conservative. Do not use this for aggressive automated scraping.

## Notes and limitations

- This does not bypass CAPTCHA, 2FA, login challenges, rate limits, or access controls.
- This does not store your LinkedIn credentials.
- If LinkedIn blocks, challenges, or restricts the session, the module pauses/fails rather than trying to evade it.
- Structured fields may need tuning after you test against real profile pages because LinkedIn markup varies by account, connection level, and page layout.
