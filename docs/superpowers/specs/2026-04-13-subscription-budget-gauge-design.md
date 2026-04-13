# Subscription Budget Gauge

**Date:** 2026-04-13
**Status:** Approved

## Problem

The dashboard calculates and displays costs using API pricing, but the user is on a Max 20x subscription plan ($200/mo) with a weekly usage budget. There is no way to see usage as a percentage of the weekly allowance or whether spending is on pace.

## Solution

Add a pace-aware budget gauge to the web dashboard that shows weekly usage as a percentage of the subscription budget, with color thresholds that adapt based on how far into the week you are.

## 1. Config File (`subscription.json`)

New JSON file at the project root, following the `peak-hours.json` pattern:

```json
{
  "plan": "max-20x",
  "monthly_price": 200,
  "weekly_budget_api_equivalent": 200,
  "reset": {
    "timezone": "America/Chicago",
    "day": "Tuesday",
    "time": "17:00"
  }
}
```

- **`weekly_budget_api_equivalent`** is the 100% denominator for the gauge. Tunable if the actual cap feels different from $200.
- **Reset day/time** defines the weekly billing cycle: Tuesday 5:00 PM Chicago through the following Tuesday 4:59 PM.
- Dashboard reads this file on each page load (same as `peak-hours.json`) — no server restart needed.

## 2. Backend — New API Endpoint

**`GET /api/subscription`** computes the current week's usage server-side and returns:

```json
{
  "plan": "max-20x",
  "weekly_budget": 200,
  "current_week": {
    "start": "2026-04-07T17:00:00-05:00",
    "end": "2026-04-14T17:00:00-05:00",
    "cost_used": 47.23,
    "percent_used": 23.6,
    "elapsed_fraction": 0.21,
    "pace_ratio": 1.12,
    "days_remaining": 3.2
  }
}
```

- **Server-side computation** — reuses existing `calc_cost()` from `cli.py` and Chicago timezone logic from `dashboard.py`.
- **Week boundary logic** — find the most recent past Tuesday 17:00 Chicago as `start`, add 7 days for `end`. Sum all turn costs in that window.
- **Pace ratio** = `actual_cost / (weekly_budget * elapsed_fraction)`. Measures whether spending rate will exhaust the budget before reset. When `elapsed_fraction` < ~0.006 (first hour after reset), clamp pace ratio to 1.0 and show "just started" to avoid infinity/spike artifacts.
- **Graceful degradation** — if `subscription.json` is missing or malformed, returns `{"error": "not_configured"}`. The gauge simply doesn't render.

## 3. Dashboard UI — Pace-Aware Gauge Card

### Gauge card

- **Position:** First card in the summary row (most prominent).
- **SVG arc gauge** (no dependencies) showing absolute percent of weekly budget used.
- **Center text:** Large percentage (e.g., `24%`) with `$47.23 / $200` below.
- **Subtitle:** `Resets Tue 5:00 PM · 3.2 days left`
- **Pace indicator:** Small text like `1.2x pace` or `on track` beneath the percentage.
- **Pace marker:** Thin line on the arc showing where even spending would be at this point in the week.

### Pace-adjusted color thresholds

Instead of fixed thresholds, color reflects whether spending rate is sustainable:

```
elapsed_fraction = hours_since_reset / 168
expected_cost    = weekly_budget * elapsed_fraction
pace_ratio       = actual_cost / expected_cost
```

| Pace ratio | Color | Meaning |
|-----------|-------|---------|
| < 1.2 | Green (`#4ade80`) | On track or under |
| 1.2 - 1.5 | Yellow (`#facc15`) | Running hot |
| > 1.5 | Red (`#f87171`) | Burning through budget too fast |

**Examples:**
- Monday at 80% used -> pace ratio ~4.7 -> red
- Friday at 80% used -> pace ratio ~1.1 -> green
- Tuesday morning at 20% used -> pace ratio ~1.5 -> yellow

### Alert behavior

- At yellow (1.2x+): gauge border pulses gently (CSS animation).
- At red (1.5x+): card gets a subtle red glow.
- No popups, modals, or sound.

### When not configured

Gauge card does not render. Dashboard looks exactly as it does today.

## 4. Testing

Unit tests in `tests/` using `unittest` (no new dependencies):

- **Week boundary calculation** — given a "now" timestamp and reset config (Tuesday 17:00 Chicago), verify correct start/end window. Edge cases: exactly at reset time, just before reset, DST transition weeks.
- **Pace ratio calculation** — given elapsed fraction and actual cost, verify ratio and color bucket.
- **Graceful degradation** — missing file, malformed JSON, missing fields all return "not configured" without crashing.

## Files Changed

| File | Change |
|------|--------|
| `subscription.json` (new) | Config file with plan, budget, reset schedule |
| `dashboard.py` | New `/api/subscription` endpoint, read config, compute week window + pace |
| `dashboard.py` (HTML) | SVG gauge card, pace coloring, CSS animations |
| `tests/test_subscription.py` (new) | Week boundary, pace ratio, degradation tests |
