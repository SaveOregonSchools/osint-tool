# LinkedIn Integration

The LinkedIn modules are browser-assisted evidence tools. They open a visible
Playwright browser using a persistent local browser profile, let you log in and
complete any 2FA/challenge manually, and then collect only pages visible to that
browser session.

They do not store your LinkedIn password, bypass CAPTCHA/2FA/login challenges
or rate limits, or access private/restricted content.

## Modules

### LinkedIn Evidence Capture v1.9

Plugin key: `linkedin_evidence_capture_v1`

This is the preferred LinkedIn evidence-preservation module. It accepts uploaded
or pasted CSV rows with names and LinkedIn profile URLs, then captures evidence
under:

```text
data/linkedin_evidence_capture_v1/
```

For each target, it can capture:

- Main profile HTML and screenshot.
- Optional profile photo when one is visible on the main profile page.
- Detail pages for Experience, Education, Volunteering, and Licenses &
  certifications.
- JSON metadata for sections, including empty or unavailable sections.
- A run manifest and SQLite tracking database.

Outputs are separated from the older profile collector so evidence runs do not
mix with earlier experiments.

### LinkedIn Profile Collector v1.3

Plugin key: `linkedin_profile_collector_v1`

This older collector is still available. It visits supplied profile URLs and
saves best-effort structured profile fields, visible page text, screenshots, and
HTML snapshots under:

```text
data/linkedin_profiles.db
data/linkedin_snapshots/
data/linkedin_exports/
```

Use this module when you want structured fields such as profile name, headline,
location, current title/company, experience snippets, education snippets, and
full visible text. Use Evidence Capture v1.9 when preservation of screenshots,
HTML, and detail sections is the priority.

## CSV Input

Preferred columns:

```csv
person_name,linkedin_url,notes
Jane Smith,https://www.linkedin.com/in/jane-smith-12345/,board member
John Doe,https://www.linkedin.com/in/johndoe/,campaign contact
```

Accepted name columns include:

- `person_name`
- `name`
- `person`
- `full_name`

Accepted URL columns include:

- `linkedin_url`
- `profile_url`
- `url`
- `link`
- `hyperlink`

Accepted notes columns include:

- `notes`
- `note`
- `context`

## First Run Workflow

1. Install dependencies with `pip install -r requirements.txt`.
2. Install the browser runtime if needed:

   ```bash
   python -m playwright install chromium
   ```

3. Start the Flask app with `python app.py`.
4. Select a LinkedIn module.
5. Upload a CSV or paste CSV text.
6. Start with a small `Max profiles` value, such as 2 or 3.
7. Keep the browser visible for the first run.
8. Click **Run Query**.
9. Log into LinkedIn manually in the opened browser and complete any challenge.
10. Let the module proceed through the supplied profiles.

The browser session is stored locally under `data/`, so you usually do not need
to log in again until LinkedIn expires the session.

## Recommended Settings

Start conservatively:

```text
Max profiles: 2-3
Delay between profiles: 8-15 seconds
Login wait: 10-15 minutes
Headless: No
Save HTML: Yes
Save screenshots: Yes
```

For larger batches, keep delays conservative and be prepared for LinkedIn to
challenge, throttle, or restrict the session. The modules pause or fail rather
than trying to evade those controls.

## Boundaries And Limitations

- These modules collect only content visible to the logged-in browser session
  you control.
- They do not store credentials.
- They do not bypass CAPTCHA, 2FA, login challenges, rate limits, or access
  controls.
- Structured extraction is best effort because LinkedIn markup changes often.
- Screenshots, HTML snapshots, and visible text are the evidentiary fallback.
