# HN Tech Trend Radar

HN Tech Trend Radar is a public, read-only Streamlit dashboard for tracking technical trends on Hacker News. The public app reads from a committed SQLite snapshot, so Streamlit Community Cloud can run without an external database service.

## Features

- Overview of fast-moving stories, notable stories, and themes appearing across multiple sources.
- Article search and filtering by time window, category, post type, HN score, comments, and sort order.
- Per-article analysis with Japanese summaries, snippets, keywords, 24-hour score/comment movement, HN comments, and related articles.
- Trend charts for notable terms, term history, category movement, and rising/cooling terms.
- Data freshness view that explains coverage for keywords, categories, snapshots, and comments.

## Public App Data Flow

```text
Local MySQL + batch jobs
  -> scripts/export_sqlite.py
  -> data/hn_dashboard.sqlite
  -> GitHub
  -> Streamlit Community Cloud
```

The dashboard itself is read-only. Data collection runs locally or on another machine, then a SQLite snapshot is committed to the repository.

## Repository Layout

```text
app/
  main.py              Streamlit dashboard entrypoint
  db.py                SQLite read/query helpers for the public app
  analysis.py          Keyword and text-analysis helpers
  requirements.txt     Streamlit Community Cloud dependencies
data/
  hn_dashboard.sqlite  Public SQLite data snapshot
batch/
  scheduler.py         Local/external batch scheduler for MySQL ingestion
scripts/
  export_sqlite.py     MySQL-to-SQLite public data export
  migrate.py           MySQL migration runner
  migrate.sql          Additional MySQL table migrations
tests/
  test_*.py            Unit tests for scoring and enrichment logic
```

## Local Setup

```powershell
python -m venv venv
venv\Scripts\pip.exe install -r requirements.txt
venv\Scripts\python.exe scripts\migrate.py
venv\Scripts\python.exe scripts\export_sqlite.py
venv\Scripts\streamlit.exe run app\main.py
```

For local MySQL batch/export work, set database connection values through environment variables or a local `.env` file:

- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME`

The public Streamlit app does not need these values when `data/hn_dashboard.sqlite` is present.

## Streamlit Community Cloud

Use `app/main.py` as the app entrypoint. The app dependencies are listed in `app/requirements.txt`.

Secrets are not required for the SQLite public app. Make sure `data/hn_dashboard.sqlite` is committed to GitHub and kept below GitHub's file size limits.

## Updating the Public Data Snapshot

Run the local batch pipeline, export a fresh SQLite snapshot, then commit and push the updated database.

```powershell
venv\Scripts\python.exe batch\fetch_articles.py
venv\Scripts\python.exe batch\record_snapshots.py
venv\Scripts\python.exe batch\enrich_articles.py
venv\Scripts\python.exe batch\fetch_comments.py
venv\Scripts\python.exe batch\extract_keywords.py
venv\Scripts\python.exe batch\summarize_articles.py
venv\Scripts\python.exe scripts\export_sqlite.py
```

`scripts/export_sqlite.py` exports the last 30 days by default. Use `--days` to change the window:

```powershell
venv\Scripts\python.exe scripts\export_sqlite.py --days 14
```

## Tests

```powershell
venv\Scripts\python.exe -m unittest discover -s tests -v
```
