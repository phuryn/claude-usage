# Claude Code Usage Dashboard

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![claude-code](https://img.shields.io/badge/claude--code-black?style=flat-square)](https://claude.ai/code)
[![Companion: burnstop](https://img.shields.io/badge/companion-burnstop-blue?style=flat-square)](https://github.com/phuryn/burnstop)

**About:** Local dashboard tracking LLM token usage, costs, and session history. Originally built by [phuryn](https://github.com/phuryn/claude-usage).

Claude Code writes detailed usage logs locally — token counts, models, sessions, projects — regardless of your plan. This dashboard reads those logs and turns them into charts and cost estimates. Works on API, Pro, and Max plans.

![Claude Usage Dashboard](docs/screenshot.png)

Run it as a local web app with `python cli.py dashboard`.

---

## What this tracks

Works on **API, Pro, and Max plans** — Claude Code writes local usage logs regardless of subscription type. This tool reads those logs and gives you visibility that Anthropic's UI doesn't provide.

Captures usage from local Claude Code transcript files, including:
- **Claude Code CLI** (`claude` command in terminal)
- **Dispatched Code sessions** (sessions routed through Claude Code)
- **Xcode Claude integration sessions** when the local Xcode transcript directory exists

**Not captured:**
- **Cowork sessions** — these run server-side and do not write local JSONL transcripts

---

## Requirements

- Python 3.8+
- No third-party packages — uses only the standard library (`sqlite3`, `http.server`, `json`, `pathlib`)

> Anyone running Claude Code already has Python installed.

## Quick Start

No `pip install`, no virtual environment, no build step.

### macOS / Linux
```
git clone https://github.com/krnv9h68j4-max/claude-usage-jdt.git
cd claude-usage-jdt
python3 cli.py dashboard
```

Opens the dashboard at **http://localhost:8080** after scanning local transcripts.

### Windows
```
git clone https://github.com/krnv9h68j4-max/claude-usage-jdt.git
cd claude-usage-jdt
python cli.py dashboard
```

### Docker
```
git clone https://github.com/krnv9h68j4-max/claude-usage-jdt.git
cd claude-usage-jdt
bash scripts/run-docker.sh
```

Opens the containerized dashboard at **http://localhost:9898**. The script pulls the latest code, builds the image, and runs the container with:
- `~/.claude` mounted **read-only** — the container can read transcripts but cannot modify them
- A named Docker volume (`claude-usage-data`) for the SQLite database — persisted across restarts, isolated from your home directory

### Common commands

```
python cli.py scan                  # Scan JSONL files into ~/.claude/usage.db
python cli.py today                 # Today's usage by model
python cli.py week                  # Last 7 days, per-day and by-model
python cli.py stats                 # All-time usage statistics
python cli.py dashboard             # Scan + serve http://localhost:8080
HOST=0.0.0.0 PORT=9000 python cli.py dashboard
python cli.py scan --projects-dir /path/to/transcripts
```

The scanner is incremental — re-running `scan` is fast and only processes new or changed files.

---

## Usage

> On macOS/Linux, use `python3` instead of `python` in all commands below. If you installed via Homebrew, replace `python cli.py` with `claude-usage`.

```
# Scan JSONL files and populate the database (~/.claude/usage.db)
python cli.py scan

# Show today's usage summary by model (in terminal)
python cli.py today

# Show the last 7 days (per-day breakdown + by-model totals)
python cli.py week

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

`dashboard.py` serves a single-page dashboard on `localhost:8080` with Chart.js charts (loaded from CDN). It auto-refreshes every 30 seconds and supports model filtering and a date-range dropdown with bookmarkable URLs. A sticky section nav jumps between sections, and every chart/table can be collapsed (remembered across reloads). The bind address and port can be overridden with `HOST` and `PORT` environment variables (defaults: `localhost`, `8080`).

---

## Cost estimates

Costs shown in the CLI and dashboard are calculated with the Anthropic pricing table embedded in this fork's `cli.py` and `dashboard.py`, updated against **Anthropic API pricing as of June 2026** ([claude.com/pricing#api](https://claude.com/pricing#api)).

**Only models whose name contains `fable`, `mythos`, `opus`, `sonnet`, or `haiku` are included in cost calculations.** Local models, unknown models, and any other model names are excluded (shown as `n/a`).

| Model | Input | Output | Cache Write | Cache Read |
|-------|-------|--------|------------|-----------|
| claude-fable-5 | $10.00/MTok | $50.00/MTok | $12.50/MTok | $1.00/MTok |
| claude-mythos-5 | $10.00/MTok | $50.00/MTok | $12.50/MTok | $1.00/MTok |
| claude-opus-4-8 | $5.00/MTok | $25.00/MTok | $6.25/MTok | $0.50/MTok |
| claude-opus-4-7 | $5.00/MTok | $25.00/MTok | $6.25/MTok | $0.50/MTok |
| claude-opus-4-6 | $5.00/MTok | $25.00/MTok | $6.25/MTok | $0.50/MTok |
| claude-opus-4-5 | $5.00/MTok | $25.00/MTok | $6.25/MTok | $0.50/MTok |
| claude-sonnet-4-7 | $3.00/MTok | $15.00/MTok | $3.75/MTok | $0.30/MTok |
| claude-sonnet-4-6 | $3.00/MTok | $15.00/MTok | $3.75/MTok | $0.30/MTok |
| claude-sonnet-4-5 | $3.00/MTok | $15.00/MTok | $3.75/MTok | $0.30/MTok |
| claude-haiku-4-7 | $1.00/MTok | $5.00/MTok | $1.25/MTok | $0.10/MTok |
| claude-haiku-4-6 | $1.00/MTok | $5.00/MTok | $1.25/MTok | $0.10/MTok |
| claude-haiku-4-5 | $1.00/MTok | $5.00/MTok | $1.25/MTok | $0.10/MTok |

> **Note:** These are API prices. If you use Claude Code via a Max or Pro subscription, your actual cost structure is different (subscription-based, not per-token).

For context, these are comparable API prices from other major LLM providers as of June 2026. They are **not** used by this dashboard's cost calculation unless the code is extended to include those model families.

| Provider | Model | Input | Cached / Cache Read | Output | Source |
|----------|-------|-------|---------------------|--------|--------|
| OpenAI | gpt-5.5 | $2.50/MTok short context; $5.00/MTok long context | $0.25/MTok short context; $0.50/MTok long context | $15.00/MTok short context; $22.50/MTok long context | [OpenAI API pricing](https://developers.openai.com/api/docs/pricing) |
| OpenAI | gpt-5.4 | $1.25/MTok short context; $2.50/MTok long context | $0.13/MTok short context; $0.25/MTok long context | $7.50/MTok short context; $11.25/MTok long context | [OpenAI API pricing](https://developers.openai.com/api/docs/pricing) |
| Google | gemini-3.5-flash | $2.70/MTok | $0.27/MTok context caching | $16.20/MTok | [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| Google | gemini-3.1-flash-lite | $0.25/MTok text/image/video | $0.025/MTok context caching | $1.50/MTok | [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| xAI | grok-4.3 | $1.25/MTok | n/a | $2.50/MTok | [xAI model pricing](https://docs.x.ai/developers/models) |

Always check the linked provider pages before making billing decisions; API pricing can change without a code change here.

---

## Files

| File | Purpose |
|------|---------|
| `scanner.py` | Parses JSONL transcripts, writes to `~/.claude/usage.db` |
| `dashboard.py` | HTTP server + single-page HTML/JS dashboard |
| `cli.py` | `scan`, `today`, `stats`, `dashboard` commands |
| `Formula/claude-usage.rb` | Homebrew formula — install with `brew install --formula <raw-url>` |
| `Dockerfile` | Container image definition |
| `scripts/run-docker.sh` | Build and run the dashboard in Docker with a read-only `~/.claude` mount |
