# Claude Code Usage Dashboard

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![claude-code](https://img.shields.io/badge/claude--code-black?style=flat-square)](https://claude.ai/code)

**Pro and Max subscribers get a progress bar. This gives you the full picture.**

Claude Code writes detailed usage logs locally — token counts, models, sessions, projects — regardless of your plan. This dashboard reads those logs and turns them into charts and cost estimates. Works on API, Pro, and Max plans.

![Claude Usage Dashboard](docs/screenshot.png)

**Created by:** [The Product Compass Newsletter](https://www.productcompass.pm)

---

## What this tracks

Works on **API, Pro, and Max plans** — Claude Code writes local usage logs regardless of subscription type. This tool reads those logs and gives you visibility that Anthropic's UI doesn't provide.

Captures usage from:
- **Claude Code CLI** (`claude` command in terminal)
- **VS Code extension** (Claude Code sidebar)
- **Dispatched Code sessions** (sessions routed through Claude Code)

**Not captured:**
- **Cowork sessions** — these run server-side and do not write local JSONL transcripts

### Windows: Desktop app session metadata

On Windows, the scanner additionally reads session metadata from `%APPDATA%/Claude/claude-code-sessions/` (written by the Claude Desktop app). This enriches sessions with:

- **Title** — human-readable names like "Hourly checkin" or "Kb refresh" that replace the generic cwd-derived project name in the dashboard's Project column
- **Original cwd** — the working directory the desktop app recorded explicitly, shown as a hover tooltip

This feature is Windows-only. On macOS/Linux, the enrichment silently no-ops and sessions show their cwd-derived project name as before.

Token counts are not affected by enrichment — they come entirely from the JSONL files under `~/.claude/projects/`. Desktop metadata only adds labels.

---

## Requirements

- Python 3.9+
- No third-party packages on macOS/Linux — uses only the standard library (`sqlite3`, `http.server`, `json`, `pathlib`, `zoneinfo`)
- **Windows only:** install `tzdata` for timezone support: `pip install tzdata` (Python's `zoneinfo` needs IANA timezone data, which Windows does not ship natively)

> Anyone running Claude Code already has Python installed.

## Quick Start

No `pip install`, no virtual environment, no build step.

### Windows
```
git clone https://github.com/phuryn/claude-usage
cd claude-usage
python cli.py dashboard
```

### macOS / Linux
```
git clone https://github.com/phuryn/claude-usage
cd claude-usage
python3 cli.py dashboard
```

---

## Usage

> On macOS/Linux, use `python3` instead of `python` in all commands below.

```
# Scan JSONL files and populate the database (~/.claude/usage.db)
python cli.py scan

# Show today's usage summary by model (in terminal)
python cli.py today

# Show all-time statistics (in terminal)
python cli.py stats

# Scan + open browser dashboard at http://localhost:8080
python cli.py dashboard

# Custom host and port via environment variables
HOST=0.0.0.0 PORT=9000 python cli.py dashboard

# Scan a custom projects directory
python cli.py scan --projects-dir /path/to/transcripts
```

The scanner is incremental — it tracks each file's path and modification time, so re-running `scan` is fast and only processes new or changed files.

By default, the scanner checks both `~/.claude/projects/` and the Xcode Claude integration directory (`~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig/projects/`), skipping any that don't exist. Use `--projects-dir` to scan a custom location instead.

---

## How it works

Claude Code writes one JSONL file per session to `~/.claude/projects/`. Each line is a JSON record; `assistant`-type records contain:
- `message.usage.input_tokens` — raw prompt tokens
- `message.usage.output_tokens` — generated tokens
- `message.usage.cache_creation_input_tokens` — tokens written to prompt cache
- `message.usage.cache_read_input_tokens` — tokens served from prompt cache
- `message.model` — the model used (e.g. `claude-sonnet-4-6`)

`scanner.py` parses those files and stores the data in a SQLite database at `~/.claude/usage.db`.

`dashboard.py` serves a single-page dashboard on `localhost:8080` with Chart.js charts (loaded from CDN). It auto-refreshes every 30 seconds and supports model filtering with bookmarkable URLs. The bind address and port can be overridden with `HOST` and `PORT` environment variables (defaults: `localhost`, `8080`).

---

## Cost estimates

Costs are calculated using **Anthropic API pricing as of April 2026** ([claude.com/pricing#api](https://claude.com/pricing#api)).

**Only models whose name contains `opus`, `sonnet`, or `haiku` are included in cost calculations.** Local models, unknown models, and any other model names are excluded (shown as `n/a`).

| Model | Input | Output | Cache Write | Cache Read |
|-------|-------|--------|------------|-----------|
| claude-opus-4-6 | $5.00/MTok | $25.00/MTok | $6.25/MTok | $0.50/MTok |
| claude-sonnet-4-6 | $3.00/MTok | $15.00/MTok | $3.75/MTok | $0.30/MTok |
| claude-haiku-4-5 | $1.00/MTok | $5.00/MTok | $1.25/MTok | $0.10/MTok |

> **Note:** These are API prices. If you use Claude Code via a Max or Pro subscription, your actual cost structure is different (subscription-based, not per-token).

---

## Time-of-day view

The dashboard includes two charts that break usage down by hour of day in your local time (currently hardcoded to America/Chicago, DST-aware):

- **Usage by Hour of Day** — 24 stacked bars averaged across the days in your selected range. Useful for spotting patterns like "I always burn tokens at 9am."
- **Hourly Timeline** — one stacked bar per (day, hour) in the selected range, sorted chronologically. Wider ranges scroll horizontally. Useful for forensic investigation like "what happened yesterday at 3pm?"

Both charts show a translucent peak-hour overlay based on `peak-hours.json` in the repo root. The default reflects Anthropic's reported peak window (Mon–Fri 05:00–11:00 Pacific, March 2026 source). Edit the file and refresh the dashboard to change it.

## Custom date range

In addition to the preset 7d/30d/90d/All buttons, you can pick a custom From/To range using the date inputs at the top of the dashboard. Using the custom range deactivates the preset buttons; clicking any preset clears the custom dates. Range is persisted in the URL as `?from=YYYY-MM-DD&to=YYYY-MM-DD`.

---

## Files

| File | Purpose |
|------|---------|
| `scanner.py` | Parses JSONL transcripts, writes to `~/.claude/usage.db` |
| `dashboard.py` | HTTP server + single-page HTML/JS dashboard |
| `cli.py` | `scan`, `today`, `stats`, `dashboard` commands |
