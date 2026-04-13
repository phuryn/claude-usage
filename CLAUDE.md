# Claude Usage Dashboard

A local CLI + web dashboard that tracks Claude Code token usage and estimated costs.

## Architecture

- **`cli.py`** — CLI entry point (`scan`, `today`, `stats`, `dashboard` commands)
- **`scanner.py`** — Parses Claude Code JSONL transcripts from `~/.claude/projects/` into a SQLite DB (`~/.claude/usage.db`)
- **`dashboard.py`** — Serves a local web dashboard on `localhost:8080` (threaded HTTP server, no framework)
- **`subscription.py`** — Weekly budget tracking: config loading, week boundary calculation, pace ratio
- **`peak-hours.json`** — Configurable peak-hour overlay bands for the dashboard chart
- **`subscription.json`** — User config for subscription plan, weekly budget, and reset schedule

## Key conventions

- **Python 3.9+** — no dependencies beyond the standard library (plus `tzdata` on Windows)
- **No framework** — raw `http.server` for the dashboard, `unittest` for tests
- **SQLite** — single DB at `~/.claude/usage.db`; schema lives in `scanner.py:init_db()`
- **Timezone** — dashboard uses `America/Chicago` for local-day bucketing; peak hours stored in `America/Los_Angeles`
- **Cost calculation** — per-model pricing in `cli.py:PRICING`; cache read = 10% of input price, cache creation = 125%

## Running

```bash
python cli.py scan        # ingest JSONL transcripts
python cli.py dashboard   # scan + open browser + serve on :8080
python cli.py today       # terminal summary
python cli.py stats       # all-time terminal summary
```

## Configuration

- `peak-hours.json` — Peak hour overlay bands for the hourly chart
- `subscription.json` — Subscription plan and weekly budget settings (optional; gauge hidden if absent)

## Testing

```bash
python -m unittest discover -s tests -v
```

CI runs on Python 3.9, 3.11, 3.12 via GitHub Actions.
