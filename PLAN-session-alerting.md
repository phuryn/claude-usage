# PLAN: Session Alerting + Daily Pacing

## Status
Complete

## Tool
Claude Code

## Dates
Created: 2026-04-22

## Overview
Add real-time session monitoring, OS notifications, in-conversation alerts, and daily pacing to the claude-usage dashboard. Everything starts from `python cli.py dashboard`. Hook is one-time registration via `python cli.py install-hook`.

## Goals
- Alert user when active session is getting too long (context filling, cost, turns, duration)
- Show daily pacing (spend vs budget, projected EOD)
- Cross-platform OS notifications (macOS + Windows + Linux), zero new deps
- In-conversation hook warning that fires inside Claude regardless of dashboard state
- All configurable via browser settings UI — no hand-editing JSON

## Upstream PRs We Leverage
These are open PRs against phuryn/claude-usage that we port logic from. Our PR will credit them.

| PR | Author | What we take |
|----|--------|-------------|
| [#49](https://github.com/phuryn/claude-usage/pull/49) | tacobell101-101 | `subscription.py` pace ratio logic, SVG arc gauge UI, `subscription.json` config pattern |
| [#52](https://github.com/phuryn/claude-usage/pull/52) | gpechenik | `/api/session` endpoint + `get_session_detail()` — turn history, tool usage, branch info |
| [#57](https://github.com/phuryn/claude-usage/pull/57) | chphch | Usage Limits gauge widget UI — adapt for context fill % gauge |

We adapt, don't copy verbatim. Our config lives at `~/.claude/usage_alerts.json` (not `subscription.json`). Pace logic is daily not weekly. Session detail endpoint feeds our active session card.

## Key Design Decisions
- Single entrypoint: `python cli.py dashboard` starts server + background monitor thread
- Hook fires independently of dashboard (registered once, always active)
- Best "new session" signal: `input_tokens` on most recent turn = actual context window fill
- All Claude models = 200K context window limit
- Plan (Pro/Max/Team/Enterprise) set manually in config — used for daily budget presets
- No new Python dependencies — all stdlib + subprocess for OS notifications

## Context Window Tracking
- `input_tokens` on most recent turn = tokens currently in context
- Context fill % = last_turn_input_tokens / model_context_limit (200K)
- Alert threshold: configurable, default 80%
- This is the primary "time for new session" signal (context fill → degraded responses)

## Config Schema
Location: `~/.claude/usage_alerts.json`

```json
{
  "os_notifications": true,
  "plan": "max",
  "daily": {
    "budget_usd": 10.00,
    "warn_at_percent": 80
  },
  "session": {
    "cost_usd": 1.00,
    "turns": 50,
    "duration_minutes": 60,
    "context_fill_percent": 80
  },
  "notification_cooldown_minutes": 10
}
```

## Hook Warning Format (in-conversation)
```
⚠️  Session alert: 52 turns · $1.23 · 47min · context 78% full (156K/200K)
📊  Today: $4.20 / $10.00 budget (42%) — pacing ~$8.90 by EOD
```

## OS Notification Strategy
- `platform.system()` dispatch:
  - `Darwin` → `osascript -e 'display notification "..." with title "..."'`
  - `Windows` → PowerShell subprocess with toast API
  - `Linux` → `notify-send "title" "message"`
- Deduplication: don't re-fire same alert within cooldown window (configurable, default 10min)
- Respects `os_notifications: false`

## Tasks

### New files
- [x] `alert_config.py` — load/save/validate config with defaults
- [x] `notifier.py` — cross-platform OS notification dispatch + cooldown dedup
- [x] `session_alert_hook.py` — PostToolUse hook: reads usage.db, prints warning if thresholds crossed
- [x] `tests/test_alerts.py` — unit tests for config, notifier, hook logic (16 tests)

### `cli.py` changes
- [x] `install-hook` command — writes PostToolUse entry to `~/.claude/settings.json`, idempotent, shows diff

### `dashboard.py` changes
- [x] Port `get_session_detail()` + `/api/session` endpoint from PR #52
- [x] Port pace ratio logic from PR #49 `subscription.py`, adapt daily (not weekly)
- [x] Port SVG arc gauge UI from PR #49, use for daily budget %
- [x] Adapt usage limits gauge from PR #57 for context fill % display
- [x] Background monitor thread — polls active session every 15s, fires OS notifications
- [x] `/api/active` endpoint — live current session: session_id, project, cost, turns, duration_min, context_tokens, context_pct, model
- [x] `/api/config` GET + POST endpoints
- [x] Pacing card UI — today spend / budget bar (SVG arc), projected EOD, sessions today, avg $/session
- [x] Active session card UI — context fill % gauge, cost, turns, duration, session start time
- [x] `/settings` page — browser-editable thresholds, plan selector, OS notifications toggle

## Files Affected
| File | Change |
|------|--------|
| `alert_config.py` | new — config load/save/validate |
| `notifier.py` | new — cross-platform OS toast + cooldown |
| `session_alert_hook.py` | new — PostToolUse hook script |
| `dashboard.py` | monitor thread, pacing card, active session card, settings UI, new API endpoints, ported session detail |
| `cli.py` | add `install-hook` command |
| `tests/test_alerts.py` | new |

## API Endpoints Added
- `GET /api/active` — live current session: session_id, project, cost, turns, duration_min, context_tokens, context_pct, model
- `GET /api/session?session_id=<id>` — turn history + tool usage (ported from PR #52)
- `GET /api/config` — read alert config
- `POST /api/config` — write alert config
- `GET /settings` — settings page HTML

## Dashboard UI Changes
- **Pacing card** (new, top row): SVG arc gauge for daily budget %, projected EOD, sessions today, avg $/session
- **Active session card** (new, top row): context fill % gauge (green→yellow→red), cost, turns, duration, session start time
- **Settings page** (`/settings`): form to edit all thresholds, plan selector, OS notifications toggle

## Risks
- Hook reads usage.db on every tool call — must be fast (single indexed query, <5ms)
- Windows PowerShell toast API varies by Windows version — need fallback (MessageBox via mshta)
- Monitor thread must not block dashboard shutdown — use daemon thread
- Config file missing = use defaults, never crash
- PR #49/#52/#57 not merged upstream — we port logic, not git-merge, to stay clean

## Notes
- `install-hook` is idempotent — safe to run multiple times
- Hook path stored as absolute path in settings.json so it works from any cwd
- Context window limit hardcoded per model family (200K for all current Claude models) — update when new models release
- Plan field is cosmetic for now (budget presets) — future: could gate model expectations
- PR description will credit PR #49 (tacobell101-101), PR #52 (gpechenik), PR #57 (chphch)

## Completion Summary
All tasks complete. 100/100 tests passing (16 new in test_alerts.py). 

New files: `alert_config.py`, `notifier.py`, `session_alert_hook.py`, `tests/test_alerts.py`.

`cli.py`: added `install-hook` command (idempotent PostToolUse registration).

`dashboard.py`: added `get_active_session()`, `get_session_detail()`, `get_pace_data()`, background monitor thread, new routes `/api/active` `/api/session` `/api/pace` `/api/config` `/settings`, pacing card + active session card with SVG arc gauges (adapted from PR #49/#57 — not copied verbatim), settings page HTML. Logic from PR #52 adapted for `get_session_detail()`.

PR credits: PR #49 (tacobell101-101) pace ratio + SVG arc gauge pattern; PR #52 (gpechenik) session detail endpoint pattern; PR #57 (chphch) context fill gauge concept.
