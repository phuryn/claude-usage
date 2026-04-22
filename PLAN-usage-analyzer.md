# PLAN: Usage Analyzer

## Status
In PR — https://github.com/phuryn/claude-usage/pull/66

## Tool
Claude Code

## Dates
- Created: 2026-04-22
- Target: TBD

## Overview

Add "Analyze Usage" feature that takes aggregated Claude Code usage data from the local SQLite DB and launches a `claude` CLI session to suggest token-saving optimizations grounded in the user's actual metrics.

Two modes:

| Mode | Trigger | Mechanism | Output |
|------|---------|-----------|--------|
| **Quick** | Dashboard "Analyze" button / `cli.py analyze` | `claude --print` subprocess, streamed via SSE | In-page panel with ranked suggestions |
| **Deep Dive** | Dashboard "Open Deep Dive" button | Spawns new terminal running interactive `claude` with pre-loaded prompt | Interactive chat — user asks follow-ups |

**Key constraint:** uses the user's existing local `claude` CLI installation. No new API key, no new dependency. Tokens billed to user's own Claude plan.

## Design

### Data snapshot

`analyzer.build_snapshot(conn) -> dict` queries SQLite and returns:

- Overall cache hit rate: `sum(cache_read_input_tokens) / sum(input_tokens + cache_read_input_tokens)`
- Per-project cache rate (top 10 by spend)
- Model distribution % (Opus / Sonnet / Haiku) by token volume and by spend
- Session patterns: avg turns, avg context fill %, avg cost, p95 cost
- Top 10 expensive sessions (last 30d) with tool-use counts
- Daily cost trend (last 30d) + 7d vs prior-7d delta
- Tool frequency: top 20 tools by invocation count

### $ waste quantification

Pure-function calc in `analyzer.py`:

- Current cache rate C, target cache rate T=80%
- Monthly input tokens I (from last 30d extrapolated)
- Savings estimate: `(T - C) * I * (input_price - cache_read_price)`
- Similar calc for Opus→Sonnet shifts on "simple" sessions (heuristic: short turn count, small context)

### Privacy scrubber

Before any data leaves the machine:

- Project paths → hashed (SHA1 first 8 chars)
- Git branch names → stripped
- Session IDs → hashed
- Preserve: model names, token counts, timestamps, tool names, cost numbers
- User-visible diff in preflight modal showing raw vs scrubbed
- Settings toggle `analyzer.scrub` (default `true`) in `~/.claude/usage_alerts.json`

### Prompt builder

`analyzer.build_prompt(snapshot) -> str` produces markdown with:

1. **Data section** — tables of scrubbed metrics
2. **Rules section** (hardcoded):
   - Every suggestion cites a specific metric from the snapshot
   - Rank by estimated $ saved/month
   - Max 5 suggestions
   - Output format per suggestion:
     ```
     ## [Title]
     Impact: ~$X/mo
     Metric: [cite from snapshot]
     Fix: [concrete, copy-paste-ready where possible]
     ```
   - No generic advice
3. **Research section** (optional): Claude may search web for current Claude Code optimization tips. If tools unavailable, skip — core suggestions must work from data alone.

### Preflight visibility (user must understand)

Feature wraps user's own `claude` CLI. Surface this clearly.

**Preflight check on dashboard load:**
- Run `claude --version` once on server start, cache result in memory
- If missing: button disabled, tooltip "Requires Claude Code CLI. Install: https://claude.com/claude-code"
- If present: button enabled, subtitle `using claude v<X.Y.Z>`

**Pre-run disclosure modal (first click, dismissible via localStorage):**
```
This runs your local `claude` CLI.
- Uses your existing Claude auth + plan
- Tokens count toward your own usage
- Estimated cost: ~$0.05–0.20 per analysis
- Data sent: scrubbed usage snapshot (preview below)

[Show snapshot preview]  [Don't show again]  [Run]  [Cancel]
```

**Live status during run:**
```
Running: claude --print (model: claude-sonnet-4-6)
Tokens: 1,240 in / 890 out · est cost $0.02
```

**CLI parity** — `python cli.py analyze` prints same disclosure and prompts `[y/N]`.

**Deep-dive terminal banner** printed at top of spawned session:
```
=== Claude Usage Analyzer — Deep Dive ===
Session uses your claude CLI + auth.
Tokens billed to your plan.
```

**README addition** — "How it works: wraps your local `claude` CLI. Your auth, your billing."

### Snapshot cache

10-minute TTL in memory on dashboard server. Re-click within window reuses snapshot (not the LLM response — that always re-runs so user can re-ask).

### Tracking table

New SQLite table `analyses`:

| column | type |
|--------|------|
| id | INTEGER PK |
| timestamp | TEXT |
| snapshot_json | TEXT |
| suggestions_md | TEXT |
| cache_rate | REAL |
| est_monthly_waste | REAL |

Future runs compare current cache_rate vs prior entries → "cache rate improved 40% → 67% since last analysis (2 weeks ago)".

### Dashboard endpoints

- `GET /api/analyzer/preflight` → `{cli_available: bool, version: str|null}`
- `GET /api/analyzer/snapshot` → scrubbed snapshot JSON (for preview)
- `GET /api/analyzer/stream` → SSE, runs `claude --print`, streams chunks
- `POST /api/analyzer/launch-deep-dive` → writes launch script, spawns terminal
- `POST /api/analyzer/save` → persists completed suggestions to `analyses` table

### Deep-dive launcher

- Write prompt to `~/.claude/analyzer-context-<timestamp>.md`
- Write launch script next to it:
  - Windows: `launch-<timestamp>.bat`
    ```bat
    @echo off
    echo === Claude Usage Analyzer — Deep Dive ===
    echo Session uses your claude CLI + auth.
    echo Tokens billed to your plan.
    echo.
    type "%~dp0analyzer-context-<timestamp>.md" | claude
    ```
  - macOS: `launch-<timestamp>.command` with equivalent
  - Linux: `.sh` + best-effort terminal detection (`x-terminal-emulator`, fallback prompt user)
- Spawn via `os.startfile` (Windows) / `subprocess.Popen(['open', ...])` (mac) / terminal detect (linux)
- Script cleans up context file after session

### Streaming spike — RESOLVED

Tested 2026-04-22 on Windows (claude v2.1.118).

- **Default `claude --print`:** buffered. All output arrives after full completion.
- **`claude --print --output-format stream-json --include-partial-messages`:** streams. Events arrive progressively as line-delimited JSON. Content chunks via `stream_event/content_block_delta` events.

**Decision:** use `stream-json` mode. Parse events server-side, forward text deltas to SSE client.

Event types seen:
- `system/init`, `system/status` — setup (~1s in)
- `stream_event/message_start` — response begins
- `stream_event/content_block_delta` — text chunks (what UI renders)
- `stream_event/message_stop` — complete
- `result/success` — final metadata: `duration_ms`, token counts
- `rate_limit_event` — plan usage warnings (surface to user if present)

### Cross-platform portability

Quick mode (SSE streaming): fully portable.

| Component | Win / mac / Linux |
|-----------|-------------------|
| `claude` CLI flags | Identical across OSes (node/commander parsing) |
| `subprocess.Popen(text=True, bufsize=1)` | Stdlib, portable |
| `stream-json` line framing (`\n`-separated) | CLI emits `\n` everywhere; text-mode `readline` handles `\r\n` transparently |
| SSE endpoint (`http.server`) | Stdlib |
| DB path (`Path.home() / ".claude" / "usage.db"`) | Already used by scanner |

Deep-dive terminal spawn is the only OS-branched code:

| OS | Method |
|----|--------|
| Windows | `os.startfile("launch.bat")` — `.bat` pipes context into `claude` |
| macOS | `subprocess.Popen(["open", "-a", "Terminal", "launch.command"])` — executable shell script |
| Linux | Detect terminal: `x-terminal-emulator` → `gnome-terminal` → `konsole` → `xterm`. None found → print manual instructions, write `.sh`, user runs it |

Cleanup: launch script + context file deleted on process exit (best-effort; leave in `~/.claude/analyzer-tmp/` so user can inspect on failure).

## Tasks

- [x] Spike `claude --print` streaming behavior (resolved 2026-04-22: use `--output-format stream-json --include-partial-messages`)
- [ ] Linux terminal-detection helper (try-list with manual fallback)
- [ ] Test SSE path on macOS (confirm portability on second OS before ship)
- [ ] `analyzer.py` — `build_snapshot(conn)` + tests with fixture DB
- [ ] `analyzer.py` — `scrub(snapshot)` + unit tests
- [ ] `analyzer.py` — `estimate_waste(snapshot)` + unit tests
- [ ] `analyzer.py` — `build_prompt(snapshot)` + golden-file test
- [ ] `scanner.py` — migration: create `analyses` table
- [ ] `cli.py` — `analyze` subcommand (prints preflight, confirms, runs, prints)
- [ ] `dashboard.py` — `/api/analyzer/preflight` endpoint
- [ ] `dashboard.py` — `/api/analyzer/snapshot` endpoint (scrubbed)
- [ ] `dashboard.py` — `/api/analyzer/stream` SSE endpoint
- [ ] `dashboard.py` — `/api/analyzer/launch-deep-dive` endpoint
- [ ] `dashboard.py` — `/api/analyzer/save` endpoint
- [ ] Dashboard UI — "Analyze Usage" button in header + preflight-based disable
- [ ] Dashboard UI — preflight disclosure modal + localStorage "don't show again"
- [ ] Dashboard UI — slide-in results panel, SSE consumer, live token counter
- [ ] Dashboard UI — "Open Deep Dive" button on results panel
- [ ] Deep-dive launch script writer (Windows `.bat` first, mac/linux later)
- [ ] Snapshot cache layer (10 min TTL)
- [ ] README section: "Analyze Usage — wraps your local claude CLI"
- [ ] CHANGELOG entry

## Files Affected

### New
- `analyzer.py` — snapshot, scrub, waste calc, prompt builder
- `tests/test_analyzer.py`
- Launch scripts written at runtime to `~/.claude/` (not in repo)

### Modified
- `cli.py` — `analyze` subcommand
- `dashboard.py` — endpoints + embedded HTML/JS additions
- `scanner.py` — `analyses` table migration
- `README.md` — feature section
- `CHANGELOG.md` — entry

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| `claude --print` buffers instead of streams | Resolved | Use `--output-format stream-json --include-partial-messages` (verified 2026-04-22) |
| Linux user has no detectable terminal emulator | Medium | Fallback path: write `.sh`, print instructions, user runs manually |
| `stream-json` schema changes in future `claude` release | Low | Version-lock reading: parse defensively, skip unknown event types, log version from preflight |
| User has no `claude` in PATH | High | Preflight check, disabled button with install link |
| Hallucinated generic advice instead of data-grounded | Medium | Prompt rule: every suggestion must cite snapshot metric. Golden-file prompt test |
| Subprocess blocks dashboard server | Medium | Run in thread, stream via queue to SSE |
| Windows terminal spawn quoting | Medium | Write `.bat` file, invoke via `os.startfile` — no inline shell |
| Privacy: project paths leak client names | High | Scrubber default-on, preview modal shows what leaves the box |
| Meta-cost: analysis tokens count against user | Low | Disclose estimated cost upfront, small prompt <5k tokens |
| Subprocess hangs if `claude` waits for interactive input | Medium | Use `--print` mode only, timeout 120s, kill on abort |
| Multiple simultaneous analyses on same dashboard | Low | Single in-flight lock per server |

## Notes

- Quick mode uses `claude --print`; deep dive pipes prompt into interactive `claude`
- User's existing `claude` auth and billing — no API key added
- Tracking table enables before/after comparison over time
- Drop "peer comparison" framing — no peer data locally, honest label is "best-practice research"
- Research section in prompt is optional — Claude may not have web tools in subprocess context; core suggestions must work from data alone

## Completion Summary

(Fill in when done.)
