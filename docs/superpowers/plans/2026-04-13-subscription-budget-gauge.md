# Subscription Budget Gauge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pace-aware weekly budget gauge to the dashboard that shows subscription usage as a percentage of the weekly allowance, with color thresholds that adapt based on how far into the week you are.

**Architecture:** A new `subscription.json` config file defines the plan and weekly reset schedule. The dashboard server computes the current week's cost window and pace ratio via a new `/api/subscription` endpoint. The frontend renders an SVG arc gauge as the first summary card, colored by pace ratio (green/yellow/red).

**Tech Stack:** Python stdlib (json, datetime, zoneinfo, sqlite3), inline SVG in the dashboard HTML template, unittest for tests.

---

### Task 1: Create `subscription.json` config file

**Files:**
- Create: `subscription.json`

- [ ] **Step 1: Create the config file**

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

- [ ] **Step 2: Commit**

```bash
git add subscription.json
git commit -m "feat: add subscription.json config for weekly budget gauge"
```

---

### Task 2: Week boundary and pace ratio computation (TDD)

**Files:**
- Create: `subscription.py`
- Create: `tests/test_subscription.py`

This task builds the pure computation logic with no dashboard integration yet.

- [ ] **Step 1: Write failing tests for `load_subscription_config`**

Create `tests/test_subscription.py`:

```python
"""Tests for subscription.py — config loading, week boundaries, pace ratio."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class TestLoadSubscriptionConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data):
        p = self.tmp / "subscription.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_valid_config_loads(self):
        from subscription import load_subscription_config
        p = self._write({
            "plan": "max-20x",
            "monthly_price": 200,
            "weekly_budget_api_equivalent": 200,
            "reset": {"timezone": "America/Chicago", "day": "Tuesday", "time": "17:00"}
        })
        cfg = load_subscription_config(p)
        self.assertEqual(cfg["plan"], "max-20x")
        self.assertEqual(cfg["weekly_budget_api_equivalent"], 200)
        self.assertEqual(cfg["reset"]["day"], "Tuesday")

    def test_missing_file_returns_none(self):
        from subscription import load_subscription_config
        cfg = load_subscription_config(self.tmp / "nonexistent.json")
        self.assertIsNone(cfg)

    def test_malformed_json_returns_none(self):
        from subscription import load_subscription_config
        p = self.tmp / "subscription.json"
        p.write_text("{bad json", encoding="utf-8")
        self.assertIsNone(load_subscription_config(p))

    def test_missing_required_fields_returns_none(self):
        from subscription import load_subscription_config
        p = self._write({"plan": "max-20x"})  # missing reset, weekly_budget
        self.assertIsNone(load_subscription_config(p))

    def test_missing_reset_subfields_returns_none(self):
        from subscription import load_subscription_config
        p = self._write({
            "plan": "max-20x",
            "weekly_budget_api_equivalent": 200,
            "reset": {"timezone": "America/Chicago"}  # missing day, time
        })
        self.assertIsNone(load_subscription_config(p))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_subscription -v`
Expected: ModuleNotFoundError for `subscription`

- [ ] **Step 3: Write `load_subscription_config` implementation**

Create `subscription.py`:

```python
"""
subscription.py — Weekly budget tracking for subscription plans.

Loads subscription.json, computes weekly billing windows, and calculates
pace ratios for the dashboard gauge.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SUBSCRIPTION_PATH = Path(__file__).parent / "subscription.json"

_REQUIRED_FIELDS = ("plan", "weekly_budget_api_equivalent", "reset")
_REQUIRED_RESET_FIELDS = ("timezone", "day", "time")
_VALID_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def load_subscription_config(path=SUBSCRIPTION_PATH):
    """Load and validate subscription.json. Returns dict or None."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as e:
        return None

    if not isinstance(data, dict):
        return None
    if any(data.get(k) is None for k in _REQUIRED_FIELDS):
        return None

    reset = data.get("reset")
    if not isinstance(reset, dict):
        return None
    if any(reset.get(k) is None for k in _REQUIRED_RESET_FIELDS):
        return None

    if reset["day"] not in _VALID_DAYS:
        return None

    try:
        ZoneInfo(reset["timezone"])
    except (KeyError, ValueError):
        return None

    return data
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_subscription -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Write failing tests for `get_week_window`**

Append to `tests/test_subscription.py`:

```python
class TestGetWeekWindow(unittest.TestCase):
    def _reset_cfg(self):
        return {"timezone": "America/Chicago", "day": "Tuesday", "time": "17:00"}

    def test_midweek_returns_previous_tuesday(self):
        from subscription import get_week_window
        # Thursday 2026-04-09 12:00 Chicago → week started Tue 2026-04-07 17:00
        chicago = ZoneInfo("America/Chicago")
        now = datetime(2026, 4, 9, 12, 0, tzinfo=chicago)
        start, end = get_week_window(self._reset_cfg(), now)
        self.assertEqual(start, datetime(2026, 4, 7, 17, 0, tzinfo=chicago))
        self.assertEqual(end, datetime(2026, 4, 14, 17, 0, tzinfo=chicago))

    def test_exactly_at_reset_starts_new_week(self):
        from subscription import get_week_window
        chicago = ZoneInfo("America/Chicago")
        now = datetime(2026, 4, 7, 17, 0, tzinfo=chicago)  # Tuesday 17:00 exactly
        start, end = get_week_window(self._reset_cfg(), now)
        self.assertEqual(start, datetime(2026, 4, 7, 17, 0, tzinfo=chicago))
        self.assertEqual(end, datetime(2026, 4, 14, 17, 0, tzinfo=chicago))

    def test_just_before_reset_is_previous_week(self):
        from subscription import get_week_window
        chicago = ZoneInfo("America/Chicago")
        now = datetime(2026, 4, 7, 16, 59, tzinfo=chicago)  # Tuesday 16:59
        start, end = get_week_window(self._reset_cfg(), now)
        self.assertEqual(start, datetime(2026, 3, 31, 17, 0, tzinfo=chicago))
        self.assertEqual(end, datetime(2026, 4, 7, 17, 0, tzinfo=chicago))

    def test_monday_is_still_previous_week(self):
        from subscription import get_week_window
        chicago = ZoneInfo("America/Chicago")
        now = datetime(2026, 4, 6, 10, 0, tzinfo=chicago)  # Monday 10:00
        start, end = get_week_window(self._reset_cfg(), now)
        self.assertEqual(start, datetime(2026, 3, 31, 17, 0, tzinfo=chicago))
        self.assertEqual(end, datetime(2026, 4, 7, 17, 0, tzinfo=chicago))

    def test_dst_spring_forward_week(self):
        """Week spanning DST spring-forward (March 8 2026). Window should still be 7 calendar days."""
        from subscription import get_week_window
        chicago = ZoneInfo("America/Chicago")
        # Tuesday March 3 17:00 CST → Tuesday March 10 17:00 CDT
        now = datetime(2026, 3, 5, 12, 0, tzinfo=chicago)  # Thursday in that week
        start, end = get_week_window(self._reset_cfg(), now)
        self.assertEqual(start, datetime(2026, 3, 3, 17, 0, tzinfo=chicago))
        self.assertEqual(end, datetime(2026, 3, 10, 17, 0, tzinfo=chicago))
        # 7 calendar days, but actual hours differ due to DST
        self.assertEqual((end.date() - start.date()).days, 7)
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `python -m unittest tests.test_subscription.TestGetWeekWindow -v`
Expected: FAIL — `get_week_window` not defined

- [ ] **Step 7: Implement `get_week_window`**

Add to `subscription.py`:

```python
def get_week_window(reset_cfg, now=None):
    """Return (start, end) datetimes for the current billing week.

    The week starts on reset_cfg['day'] at reset_cfg['time'] in the
    configured timezone. If `now` falls exactly on the reset moment,
    it's considered the start of a new week.
    """
    tz = ZoneInfo(reset_cfg["timezone"])
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    hour, minute = map(int, reset_cfg["time"].split(":"))
    target_weekday = _VALID_DAYS.index(reset_cfg["day"])  # Monday=0

    # Find the most recent reset_day at reset_time that is <= now
    # Start from today's date at the reset time
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Walk back to the correct weekday
    days_since = (candidate.weekday() - target_weekday) % 7
    candidate = candidate - timedelta(days=days_since)

    # If candidate is in the future (we haven't reached reset time today
    # and today is the reset day), go back one full week
    if candidate > now:
        candidate = candidate - timedelta(days=7)

    start = candidate
    end = start + timedelta(days=7)
    return start, end
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m unittest tests.test_subscription.TestGetWeekWindow -v`
Expected: All 5 tests PASS

- [ ] **Step 9: Write failing tests for `calc_pace_ratio`**

Append to `tests/test_subscription.py`:

```python
class TestCalcPaceRatio(unittest.TestCase):
    def test_on_track(self):
        from subscription import calc_pace_ratio
        # 50% through week, used 50% of budget → ratio 1.0
        ratio = calc_pace_ratio(cost_used=100, weekly_budget=200, elapsed_fraction=0.5)
        self.assertAlmostEqual(ratio, 1.0)

    def test_ahead_of_pace(self):
        from subscription import calc_pace_ratio
        # 25% through week, used 50% of budget → ratio 2.0
        ratio = calc_pace_ratio(cost_used=100, weekly_budget=200, elapsed_fraction=0.25)
        self.assertAlmostEqual(ratio, 2.0)

    def test_under_pace(self):
        from subscription import calc_pace_ratio
        # 75% through week, used 25% of budget → ratio 0.33
        ratio = calc_pace_ratio(cost_used=50, weekly_budget=200, elapsed_fraction=0.75)
        self.assertAlmostEqual(ratio, 50 / 150, places=2)

    def test_first_hour_clamps_to_1(self):
        from subscription import calc_pace_ratio
        # Less than 1 hour into week (elapsed < 0.006) → clamp to 1.0
        ratio = calc_pace_ratio(cost_used=50, weekly_budget=200, elapsed_fraction=0.003)
        self.assertAlmostEqual(ratio, 1.0)

    def test_zero_cost_returns_zero(self):
        from subscription import calc_pace_ratio
        ratio = calc_pace_ratio(cost_used=0, weekly_budget=200, elapsed_fraction=0.5)
        self.assertAlmostEqual(ratio, 0.0)

    def test_zero_budget_returns_zero(self):
        from subscription import calc_pace_ratio
        ratio = calc_pace_ratio(cost_used=50, weekly_budget=0, elapsed_fraction=0.5)
        self.assertAlmostEqual(ratio, 0.0)


class TestPaceColor(unittest.TestCase):
    def test_green(self):
        from subscription import pace_color
        self.assertEqual(pace_color(0.5), "green")
        self.assertEqual(pace_color(1.0), "green")
        self.assertEqual(pace_color(1.19), "green")

    def test_yellow(self):
        from subscription import pace_color
        self.assertEqual(pace_color(1.2), "yellow")
        self.assertEqual(pace_color(1.49), "yellow")

    def test_red(self):
        from subscription import pace_color
        self.assertEqual(pace_color(1.5), "red")
        self.assertEqual(pace_color(3.0), "red")
```

- [ ] **Step 10: Run tests to verify they fail**

Run: `python -m unittest tests.test_subscription.TestCalcPaceRatio tests.test_subscription.TestPaceColor -v`
Expected: FAIL — functions not defined

- [ ] **Step 11: Implement `calc_pace_ratio` and `pace_color`**

Add to `subscription.py`:

```python
# Elapsed fraction below this threshold (≈1 hour of 168-hour week)
# triggers "just started" clamping to avoid infinity/spike in pace ratio.
_JUST_STARTED_THRESHOLD = 1.0 / 168.0  # ~0.00595


def calc_pace_ratio(cost_used, weekly_budget, elapsed_fraction):
    """Return pace ratio: actual_cost / expected_cost at this point in the week.

    Returns 1.0 when elapsed_fraction < ~1 hour (just-started clamp).
    Returns 0.0 when cost_used or weekly_budget is 0.
    """
    if weekly_budget <= 0 or cost_used <= 0:
        return 0.0
    if elapsed_fraction < _JUST_STARTED_THRESHOLD:
        return 1.0
    expected = weekly_budget * elapsed_fraction
    return cost_used / expected


def pace_color(ratio):
    """Return color bucket based on pace ratio."""
    if ratio < 1.2:
        return "green"
    if ratio < 1.5:
        return "yellow"
    return "red"
```

- [ ] **Step 12: Run all subscription tests**

Run: `python -m unittest tests.test_subscription -v`
Expected: All 16 tests PASS

- [ ] **Step 13: Run full test suite to check for regressions**

Run: `python -m unittest discover -s tests -v`
Expected: All existing tests PASS

- [ ] **Step 14: Commit**

```bash
git add subscription.py tests/test_subscription.py
git commit -m "feat: add subscription module with week boundary and pace ratio logic"
```

---

### Task 3: Backend `/api/subscription` endpoint

**Files:**
- Modify: `dashboard.py:1832-1866` (DashboardHandler.do_GET)
- Modify: `dashboard.py:1-13` (imports)
- Modify: `tests/test_dashboard.py` (add integration tests)

- [ ] **Step 1: Write failing tests for the endpoint**

Append to `tests/test_dashboard.py`, inside the existing `TestDashboardHTTP` class:

```python
    def test_api_subscription_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/subscription"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            # Either configured with budget data, or not_configured error
            self.assertTrue(
                "current_week" in data or data.get("error") == "not_configured"
            )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest tests.test_dashboard.TestDashboardHTTP.test_api_subscription_returns_json -v`
Expected: FAIL — 404 for /api/subscription

- [ ] **Step 3: Implement the `/api/subscription` endpoint**

In `dashboard.py`, add import at the top (after the existing imports around line 13):

```python
from subscription import load_subscription_config, get_week_window, calc_pace_ratio, pace_color, SUBSCRIPTION_PATH
```

Add a new function before the `DashboardHandler` class:

```python
def get_subscription_data(config_path=SUBSCRIPTION_PATH, db_path=DB_PATH):
    """Compute current-week subscription usage. Returns dict for JSON response."""
    cfg = load_subscription_config(config_path)
    if cfg is None:
        return {"error": "not_configured"}

    reset = cfg["reset"]
    tz = ZoneInfo(reset["timezone"])
    now = datetime.now(tz)
    start, end = get_week_window(reset, now)
    budget = cfg["weekly_budget_api_equivalent"]

    # Sum costs for all turns in the current week window
    from cli import calc_cost
    if not db_path.exists():
        return {"error": "not_configured"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model,
               input_tokens, output_tokens,
               cache_read_tokens, cache_creation_tokens
        FROM turns
        WHERE timestamp >= ? AND timestamp < ?
    """, (start.astimezone(ZoneInfo("UTC")).isoformat(), end.astimezone(ZoneInfo("UTC")).isoformat())).fetchall()
    conn.close()

    cost_used = sum(
        calc_cost(r["model"], r["input_tokens"] or 0, r["output_tokens"] or 0,
                  r["cache_read_tokens"] or 0, r["cache_creation_tokens"] or 0)
        for r in rows
    )

    elapsed = (now - start).total_seconds()
    total = (end - start).total_seconds()
    elapsed_fraction = max(0, min(1, elapsed / total)) if total > 0 else 0

    percent_used = (cost_used / budget * 100) if budget > 0 else 0
    ratio = calc_pace_ratio(cost_used, budget, elapsed_fraction)
    days_remaining = max(0, (end - now).total_seconds() / 86400)

    return {
        "plan": cfg["plan"],
        "weekly_budget": budget,
        "current_week": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "cost_used": round(cost_used, 4),
            "percent_used": round(percent_used, 1),
            "elapsed_fraction": round(elapsed_fraction, 4),
            "pace_ratio": round(ratio, 2),
            "pace_color": pace_color(ratio),
            "days_remaining": round(days_remaining, 1),
        }
    }
```

Add the route in `DashboardHandler.do_GET`, between the `/api/session/` block and the `else: 404` block (around line 1862):

```python
        elif self.path == "/api/subscription":
            data = get_subscription_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest tests.test_dashboard.TestDashboardHTTP.test_api_subscription_returns_json -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `python -m unittest discover -s tests -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: add /api/subscription endpoint for weekly budget data"
```

---

### Task 4: Dashboard SVG gauge card and pace coloring

**Files:**
- Modify: `dashboard.py` (HTML_TEMPLATE — CSS, HTML container, JavaScript)

This task adds the frontend gauge. It fetches `/api/subscription` on page load and renders an SVG arc gauge as the first stat card.

- [ ] **Step 1: Add CSS for the gauge card**

In `dashboard.py`, add these styles after the existing `.stat-card .sub` rule (around line 337):

```css
  /* Budget gauge */
  .gauge-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; min-width: 180px; text-align: center; }
  .gauge-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }
  .gauge-card .gauge-pct { font-size: 28px; font-weight: 700; margin: 4px 0; }
  .gauge-card .gauge-cost { font-size: 13px; color: var(--fg); font-family: monospace; }
  .gauge-card .gauge-pace { font-size: 11px; margin-top: 4px; }
  .gauge-card .gauge-reset { color: var(--muted); font-size: 10px; margin-top: 6px; }

  @keyframes pulse-border { 0%, 100% { border-color: var(--border); } 50% { border-color: var(--gauge-alert); } }
  .gauge-card.pace-yellow { --gauge-alert: #facc15; animation: pulse-border 2s ease-in-out infinite; }
  .gauge-card.pace-red { --gauge-alert: #f87171; animation: pulse-border 1.5s ease-in-out infinite; box-shadow: 0 0 12px rgba(248,113,113,0.25); }
```

- [ ] **Step 2: Add the gauge container div**

In the HTML, add a `div` with id `gauge-container` just before the `stats-row` div (around line 445):

```html
<div id="gauge-container"></div>
```

- [ ] **Step 3: Add JavaScript to fetch and render the gauge**

Add this function in the `<script>` section, after the `renderStats` function (around line 1119):

```javascript
// ── Subscription gauge ────────────────────────────────────────────────────
const PACE_COLORS = { green: '#4ade80', yellow: '#facc15', red: '#f87171' };

async function fetchAndRenderGauge() {
  try {
    const resp = await fetch('/api/subscription');
    const data = await resp.json();
    if (data.error) { document.getElementById('gauge-container').innerHTML = ''; return; }

    const w = data.current_week;
    const pct = Math.min(w.percent_used, 100);
    const color = PACE_COLORS[w.pace_color] || PACE_COLORS.green;
    const paceClass = w.pace_color === 'red' ? 'pace-red' : w.pace_color === 'yellow' ? 'pace-yellow' : '';

    // SVG arc gauge (semicircle, 180 degrees)
    const R = 50, CX = 60, CY = 55, SW = 8;
    const startAngle = Math.PI;
    const fullArc = Math.PI;
    const usedAngle = startAngle + fullArc * (pct / 100);
    const paceAngle = startAngle + fullArc * Math.min(w.elapsed_fraction, 1);

    function arcXY(angle) { return [CX + R * Math.cos(angle), CY + R * Math.sin(angle)]; }
    const [ex, ey] = arcXY(usedAngle);
    const largeArc = pct > 50 ? 1 : 0;
    const [sx, sy] = arcXY(startAngle);

    // Background arc (full semicircle)
    const [bex, bey] = arcXY(startAngle + fullArc);
    const bgArc = `M ${sx} ${sy} A ${R} ${R} 0 1 1 ${bex} ${bey}`;

    // Used arc
    const usedArc = pct > 0
      ? `M ${sx} ${sy} A ${R} ${R} 0 ${largeArc} 1 ${ex} ${ey}`
      : '';

    // Pace marker (thin line showing expected position)
    const [px, py] = arcXY(paceAngle);
    const [pix, piy] = [CX + (R - 12) * Math.cos(paceAngle), CY + (R - 12) * Math.sin(paceAngle)];
    const [pox, poy] = [CX + (R + 4) * Math.cos(paceAngle), CY + (R + 4) * Math.sin(paceAngle)];

    const paceLabel = w.pace_ratio <= 0 ? 'no usage yet'
      : w.elapsed_fraction < 0.006 ? 'just started'
      : w.pace_ratio <= 1.05 ? 'on track'
      : w.pace_ratio.toFixed(1) + '\u00d7 pace';

    const resetDay = w.end ? new Date(w.end).toLocaleDateString('en-US', { weekday: 'short' }) : '';
    const resetSuffix = data.plan ? ` \u00b7 ${w.days_remaining.toFixed(1)}d left` : '';

    document.getElementById('gauge-container').innerHTML = `
      <div class="gauge-card ${esc(paceClass)}" style="display:inline-block; vertical-align:top; margin-right:12px; margin-bottom:12px;">
        <div class="label">Weekly Budget</div>
        <svg viewBox="0 0 120 65" width="140" height="76" style="display:block; margin:0 auto;">
          <path d="${bgArc}" fill="none" stroke="#2a2d3a" stroke-width="${SW}" stroke-linecap="round"/>
          ${usedArc ? `<path d="${usedArc}" fill="none" stroke="${color}" stroke-width="${SW}" stroke-linecap="round"/>` : ''}
          <line x1="${pix}" y1="${piy}" x2="${pox}" y2="${poy}" stroke="#8892a4" stroke-width="1.5" stroke-linecap="round" opacity="0.6"/>
        </svg>
        <div class="gauge-pct" style="color:${color}">${pct.toFixed(1)}%</div>
        <div class="gauge-cost">$${w.cost_used.toFixed(2)} / $${data.weekly_budget}</div>
        <div class="gauge-pace" style="color:${color}">${esc(paceLabel)}</div>
        <div class="gauge-reset">Resets ${esc(resetDay)} 5:00 PM${esc(resetSuffix)}</div>
      </div>
    `;
  } catch (e) {
    document.getElementById('gauge-container').innerHTML = '';
  }
}
```

- [ ] **Step 4: Call `fetchAndRenderGauge()` on page load**

In the existing `loadData()` or `init()` function that runs on page load, add a call to `fetchAndRenderGauge()`. Find the line that calls `renderStats(t)` (around line 1101 in `renderAll`) and add after it:

```javascript
  fetchAndRenderGauge();
```

- [ ] **Step 5: Run the full test suite**

Run: `python -m unittest discover -s tests -v`
Expected: All tests PASS (including the HTML template tests)

- [ ] **Step 6: Manual test — start the dashboard and verify the gauge**

Run: `python dashboard.py`
Open `http://localhost:8080` in a browser.

Verify:
1. Gauge card appears as the first element above the stats row
2. Shows current percentage, dollar amounts, pace label
3. SVG arc is colored based on pace (green if on track)
4. Pace marker line shows expected position
5. Reset info shows correct day and days remaining
6. If `subscription.json` is deleted, gauge disappears on refresh

- [ ] **Step 7: Commit**

```bash
git add dashboard.py
git commit -m "feat: add SVG budget gauge card with pace-aware coloring to dashboard"
```

---

### Task 5: Final integration test and cleanup

**Files:**
- Modify: `tests/test_dashboard.py` (add gauge HTML test)
- Modify: `CLAUDE.md` (document new config file)

- [ ] **Step 1: Add template test for gauge container**

Append to the `TestHTMLTemplate` class in `tests/test_dashboard.py`:

```python
    def test_template_has_gauge_container(self):
        self.assertIn('id="gauge-container"', HTML_TEMPLATE)

    def test_template_has_gauge_fetch(self):
        self.assertIn("fetchAndRenderGauge", HTML_TEMPLATE)
```

- [ ] **Step 2: Run the full test suite**

Run: `python -m unittest discover -s tests -v`
Expected: All tests PASS

- [ ] **Step 3: Update CLAUDE.md**

Add to the `CLAUDE.md` file, in the Architecture section:

```markdown
- **`subscription.py`** — Weekly budget tracking: config loading, week boundary calculation, pace ratio
- **`subscription.json`** — User config for subscription plan, weekly budget, and reset schedule
```

And add to the Running section:

```markdown
## Configuration

- `peak-hours.json` — Peak hour overlay bands for the hourly chart
- `subscription.json` — Subscription plan and weekly budget settings (optional; gauge hidden if absent)
```

- [ ] **Step 4: Run full test suite one final time**

Run: `python -m unittest discover -s tests -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_dashboard.py CLAUDE.md
git commit -m "test: add gauge template tests and update CLAUDE.md docs"
```
