# Codex Project Notes

## Project Overview

This repository contains a small Flask-based Social OSINT Query Console. Query
modules live in `queries/` and are loaded dynamically by `app.py`.

## Local Setup

Use Python 3.11 or newer when possible.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python app.py
```

The app runs at `http://127.0.0.1:5000` by default.

## Validation

Before committing application changes, run:

```powershell
pytest
```

For dependency or packaging changes, also run:

```powershell
python -m compileall app.py common.py queries tests
```

## Development Notes

- Keep the plugin contract stable: each query module should expose `META`,
  `render_fields(form)`, `run(form)`, and `export_rows(form)`.
- Prefer small, focused changes that preserve the existing Flask plugin-console
  pattern.
- Avoid adding credentials, collected evidence, browser sessions, screenshots,
  exports, caches, or local databases to Git.
- Keep README setup instructions synchronized with the actual repository layout.
