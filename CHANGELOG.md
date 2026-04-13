# Changelog

## 2026-04-10

- Add Windows desktop app session metadata enrichment — sessions now show titles like "Hourly checkin" instead of generic "Users/scott" when available
- Add two new columns to the `sessions` table: `title` and `original_cwd`
- Add time-of-day view in America/Chicago (DST-aware): averaged hourly histogram and precise hourly timeline
- Add custom date range picker (`From`/`To` inputs), mutually exclusive with preset buttons
- Add peak-hour visual overlay on hourly charts, driven by editable `peak-hours.json` config
- Dashboard API now ships `turns_by_hour_local`, `peak_bands`, and `viewer_timezone` fields
- CSV export of sessions now includes `Title` and `Project (cwd-derived)` as separate columns
- Windows users now need `pip install tzdata` for timezone support (README updated)
- Dashboard self-heals DB schema on first load if run against a database that predates the new columns

## 2026-04-09

- Fix token counts inflated ~2x by deduplicating streaming events that share the same message ID
- Fix session cost totals that were inflated when sessions spanned multiple JSONL files
- Fix pricing to match current Anthropic API rates (Opus $5/$25, Sonnet $3/$15, Haiku $1/$5)
- Add CI test suite (84 tests) and GitHub Actions workflow running on every PR
- Add sortable columns to Sessions, Cost by Model, and new Cost by Project tables
- Add CSV export for Sessions and Projects (all filtered data, not just top 20)
- Add Rescan button to dashboard for full database rebuild
- Add Xcode project directory support and `--projects-dir` CLI option
- Non-Anthropic models (gemma, glm, etc.) no longer incorrectly charged at Sonnet rates
- CLI and dashboard now both compute costs per-turn for consistent results
