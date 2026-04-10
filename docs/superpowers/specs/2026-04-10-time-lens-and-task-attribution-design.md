# Time-Lens and Task Attribution — Design

**Status:** Approved
**Date:** 2026-04-10
**Target codebase:** `claude-usage` (main branch, HEAD = `af507cd`)
**Scope:** One implementation cycle. Follow-up cycles may add per-turn drill-down.

## 1. Problem statement

The existing `claude-usage` dashboard groups Claude Code usage by UTC calendar day, shows only preset date ranges (7d/30d/90d/all), and labels sessions with a project name derived from the last two path segments of the working directory. Three pain points follow:

1. **Time granularity is too coarse.** The dashboard cannot show usage at hourly resolution or in the viewer's local time, so it cannot answer "at what hour of day am I burning tokens?" or "how does my usage correlate with Anthropic's peak-demand windows?"
2. **Date range is inflexible.** The viewer cannot isolate an arbitrary day or pick a custom range — only the four presets are available.
3. **Attribution collapses to the home directory.** Scheduled tasks and ad-hoc sessions that run from `C:\users\scott` (or similar) all show up as the same project. With six recurring scheduled tasks accounting for ~90 session runs, the "Project" column is effectively a black box for a large slice of usage.

## 2. Investigation findings

### 2.1 Desktop app writes additional session metadata we weren't reading

The Claude Desktop app (Windows build at `%APPDATA%/Claude/`) embeds Claude Code and writes per-session metadata files at:

```
%APPDATA%/Claude/claude-code-sessions/<accountId>/<installId>/local_<uuid>.json
```

Each file is a single JSON object with fields including:

- `cliSessionId` — joins to the JSONL file in `~/.claude/projects/` (and to `sessions.session_id` in `usage.db`)
- `title` — human-readable, e.g. `"Hourly checkin"`, `"Kb refresh"`, `"Disable Windows briefing task"`
- `cwd` — the working directory the desktop app recorded explicitly (may differ from the path-slug used in `~/.claude/projects/`)
- `model`, `effort`, `enabledMcpTools`, `remoteMcpServersConfig`
- `createdAt`, `lastActivityAt` — millisecond epoch timestamps
- `completedTurns` — integer count

As of this writing the filesystem has 126 such metadata files. **Every single one has a matching JSONL** in `~/.claude/projects/`, so token counts are already captured by the existing scanner. What's missing is the attribution context.

Token attribution impact from these desktop-titled sessions:

- ~22% of input tokens in `usage.db`
- ~34% of output tokens
- Include all six recurring scheduled tasks (`commute-check`, `eod-recap`, `hourly-checkin`, `kb-refresh`, `morning-brief`, `sunday-prep`) discovered at `~/.claude/scheduled-tasks/`

These sessions currently collapse to a project name of `Users/scott` (or similar home-dir slug), rendering them indistinguishable in the dashboard.

### 2.2 Claude Desktop (the chat app / claude.ai web)

The standalone Claude Desktop chat app does not write local usage transcripts. Usage from claude.ai in a browser or the standalone Claude chat app is not available locally and is out of scope for this tool.

### 2.3 Timestamps are UTC ISO strings

The `turns` table stores `timestamp` as ISO 8601 strings with a `Z` suffix. Existing queries use `substr(timestamp, 1, 10)` to group by UTC date, which is wrong for any view that should respect the viewer's local time. Chicago time is the agreed-upon viewer timezone for this cycle.

## 3. Design decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | Viewer timezone is hardcoded to `America/Chicago` for this cycle | User explicitly chose Chicago; DST handled automatically via `zoneinfo`. Configurable timezone is out of scope. |
| D2 | Scanner adds a second data source: `%APPDATA%/Claude/claude-code-sessions/**/local_*.json` (Windows only) | All 126 existing desktop session files successfully join on `cliSessionId`. Enrichment is cheap and high-value. |
| D3 | Session metadata enrichment is additive: no existing fields are overwritten | Token totals and project names continue to be computed from JSONL data as today. Desktop metadata only adds labels. |
| D4 | Two new columns on `sessions`: `title`, `original_cwd` | Minimal schema change. Handled by the existing `try SELECT / catch / ALTER` pattern used for `message_id`. |
| D5 | Dashboard shows `title` in the Project column when present, falling back to `project_name` | Zero-disruption for terminal-only sessions; immediate wins for desktop-titled sessions. |
| D6 | Two new charts: averaged hour-of-day histogram (view A) + precise hourly timeline (view C) | User chose option A+C after reviewing four shapes. A answers "when typically?"; C answers "when exactly?". |
| D7 | Custom date range via two `<input type="date">` fields, mutually exclusive with preset buttons | Simplest UI that doesn't disrupt existing preset behavior. URL persistence via `?from=&to=`. |
| D8 | Peak-hour overlay is visual only — no stat cards or table columns | User explicitly chose option 1 of 4 reporting levels. Scope stays small. |
| D9 | Peak hours are driven by an editable `peak-hours.json` at repo root, defaulting to weekdays 05:00–11:00 Pacific | User supplied Reddit source; no authoritative Anthropic documentation. Config file is the honest middle ground. |
| D10 | Peak bands are stored in the source timezone (Pacific) and converted to viewer timezone at render time | Keeps the config portable; PT and CT both observe DST so the 2-hour offset stays stable. |

## 4. Component-level specification

### 4.1 Scanner (`scanner.py`)

**New constant:**

```python
DESKTOP_METADATA_DIR = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code-sessions"
```

Resolves to an empty-ish Path on non-Windows systems. `exists()` will return False; the enrichment pass will silently no-op.

**New function:**

```python
def read_desktop_metadata(desktop_dir=DESKTOP_METADATA_DIR):
    """Walk the Windows Claude Desktop session metadata dir and return
    a dict keyed by cliSessionId.

    Silently returns {} if the directory doesn't exist, any file is
    malformed, or the platform isn't Windows.

    Returns:
        dict: {cli_session_id: {"title": str, "original_cwd": str,
                                "model": str, "created_at_ms": int,
                                "last_activity_at_ms": int}}
    """
```

Walks via `desktop_dir.rglob("local_*.json")`. Each file is read as UTF-8, decoded with `json.loads`, and errors are caught per-file (never raised). Missing `cliSessionId` means skip the record.

**New function:**

```python
def enrich_sessions_with_desktop_metadata(conn, metadata):
    """Update sessions.title and sessions.original_cwd from desktop metadata.

    Only updates rows where session_id matches a key in metadata.
    Never clears an existing title — if a session was previously enriched
    and metadata now lacks it, the old title is preserved.
    """
```

Executed once at the end of `scan()`, after the existing total-recompute step.

**Schema evolution:**

Add to `init_db`:

```python
try:
    conn.execute("SELECT title FROM sessions LIMIT 1")
except sqlite3.OperationalError:
    conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
try:
    conn.execute("SELECT original_cwd FROM sessions LIMIT 1")
except sqlite3.OperationalError:
    conn.execute("ALTER TABLE sessions ADD COLUMN original_cwd TEXT")
```

### 4.2 Database schema

Sessions table gains two columns:

```sql
ALTER TABLE sessions ADD COLUMN title TEXT;
ALTER TABLE sessions ADD COLUMN original_cwd TEXT;
```

Both nullable. No new indices needed (both are low-cardinality per session and not filtered on in queries).

### 4.3 Dashboard backend (`dashboard.py`)

**New helper:**

```python
CHICAGO = ZoneInfo("America/Chicago")

def to_local_hour(iso_utc: str) -> tuple[str, int]:
    """Convert a UTC ISO timestamp to (local_date_str, local_hour_int) in Chicago.
    Returns ('', 0) for unparseable input.
    """
```

**New query + transformation in `get_dashboard_data`:**

```python
# Pull raw turn rows, convert each to Chicago local time, bucket by
# (local_day, local_hour, model).
turn_rows = conn.execute("""
    SELECT timestamp, model, input_tokens, output_tokens,
           cache_read_tokens, cache_creation_tokens
    FROM turns
""").fetchall()

hourly = {}  # (day_local, hour_local, model) -> counters
for r in turn_rows:
    day, hr = to_local_hour(r["timestamp"])
    if not day:
        continue
    key = (day, hr, r["model"] or "unknown")
    # accumulate tokens + turns
    ...

turns_by_hour_local = [
    {"day_local": k[0], "hour_local": k[1], "model": k[2], **v}
    for k, v in hourly.items()
]
```

This replaces no existing query — it's additive. Existing `daily_by_model`, `sessions_all`, and `all_models` remain. `daily_by_model` is kept for backwards compatibility with the existing Daily Token Usage chart, which continues to use UTC grouping (documented as a known divergence in section 6).

**Session list changes:**

The `sessions_all` builder reads `title` and `original_cwd` from each session row and passes them through. The existing `project` field is set to `title or project_name` so the UI renders correctly without an additional field. A separate `project_raw` field carries the unmodified `project_name` for CSV export and debugging.

**New config loader:**

```python
def load_peak_bands(path=Path(__file__).parent / "peak-hours.json"):
    """Load peak-hours.json. Returns [] on any error, logging a warning
    to stderr. Validates that each band has timezone, days, start, end."""
```

Bands are shipped verbatim in the API response.

**`/api/data` response additions:**

```json
{
  ...existing fields...,
  "turns_by_hour_local": [...],
  "peak_bands": [...],
  "viewer_timezone": "America/Chicago"
}
```

### 4.4 Dashboard frontend

**New state:**

```javascript
let customFrom = null;  // 'YYYY-MM-DD' or null
let customTo   = null;
let peakBands  = [];
const VIEWER_TZ = 'America/Chicago';  // shipped from server
```

**Filter bar additions:**

Two date inputs after the range-group:

```html
<div class="filter-sep"></div>
<div class="filter-label">Custom</div>
<input type="date" id="from-date" onchange="onCustomDateChange()">
<span class="muted">–</span>
<input type="date" id="to-date" onchange="onCustomDateChange()">
<button class="filter-btn" id="clear-custom" onclick="clearCustomDates()">×</button>
```

When either field is populated:

- `selectedRange` is set to `'custom'`
- All `.range-btn` elements get an `.inactive` class (greyed out)
- `applyFilter()` uses `customFrom` / `customTo` for the cutoff rather than `getRangeCutoff(selectedRange)`
- URL updates to `?from=YYYY-MM-DD&to=YYYY-MM-DD` (removing any `range=` param)

Clicking any preset button clears the custom dates and re-enables the preset behavior.

**New chart: Hour-of-Day histogram:**

- Element: `<canvas id="chart-hour-histogram">` inside a new `<div class="chart-card">`
- Data source: `turns_by_hour_local` filtered by selected models and date range, then grouped by `hour_local` and summed across all matching days, then divided by the number of distinct `day_local` values that have any data in the range. Always averaged — no toggle for a "totals" view in this cycle.
- 24 bars, one per hour (0–23 Chicago)
- Stacked by input / output / cache_read / cache_creation (same color palette as the existing daily chart)
- Peak bands rendered via a custom Chart.js plugin (see 4.5)
- Labeled "Usage by Hour of Day — America/Chicago"

**New chart: Hourly Timeline:**

- Element: `<canvas id="chart-hour-timeline">` inside a new `<div class="chart-card">`
- Data source: `turns_by_hour_local` filtered by selected models and date range, with one bar per `(day_local, hour_local)` pair in chronological order
- If total bars > 336 (14 days × 24 hours), the chart's wrapper div becomes horizontally scrollable via CSS `overflow-x: auto`
- X-axis tick label: `"04-10 15"` compact form; tooltip shows full "April 10, 3pm CT"
- Peak bands rendered on every 24-hour cycle
- Labeled "Hourly Timeline — America/Chicago"

**Sessions table:**

- The existing "Project" column header stays the same
- Cell content shows `s.project` (which is `title || project_name` from the server)
- Tooltip on the cell shows `s.project_raw` (original cwd-derived name) when different from displayed value
- CSV export gains a "Title" column and preserves the original "Project" as "Project (cwd-derived)"

### 4.5 Peak-band Chart.js plugin

A single reusable plugin registered once:

```javascript
const peakBandPlugin = {
  id: 'peakBands',
  beforeDraw(chart, args, opts) {
    // For each band in peakBands:
    //   1. Convert band.start/end/days to chart x-axis coordinates
    //      (by converting band's source TZ to America/Chicago for each
    //       visible day in the chart)
    //   2. Fill a rect from x1..x2, top..bottom with rgba(217,119,87,0.08)
  }
};
Chart.register(peakBandPlugin);
```

The plugin is used by both new charts. For the hour histogram the x-coordinates collapse to a single repeated band (0–23h space). For the hourly timeline the band repeats every 24 hours for weekday columns only.

**Timezone conversion at render time:**

```javascript
function convertBandToViewerTZ(band, localDay) {
  // band = {timezone: 'America/Los_Angeles', days: ['Mon','Tue',...],
  //         start: '05:00', end: '11:00'}
  // localDay = the day we're rendering, in viewer's timezone
  // Returns {start_hour: 7, end_hour: 13} in viewer local time, or null
  // if the band doesn't apply to localDay.
}
```

Uses browser `Intl.DateTimeFormat` with `timeZone` option to do the conversion without pulling in a timezone library.

### 4.6 Config file

New file at repo root: `peak-hours.json`

```json
{
  "source": "Reddit community synthesis, 2026-03-26 (Thariq Shihipar update)",
  "note": "Peak times are stored in their native Pacific timezone and converted to the viewer's timezone at render time. Edit this file and refresh the dashboard to change the overlay.",
  "bands": [
    {
      "timezone": "America/Los_Angeles",
      "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
      "start": "05:00",
      "end": "11:00",
      "label": "Anthropic peak"
    }
  ]
}
```

Schema:

- `bands`: list of band objects
  - `timezone`: IANA timezone string (required)
  - `days`: list of three-letter day names in English (required, case-insensitive)
  - `start`, `end`: `"HH:MM"` strings, 24-hour (required)
  - `label`: free text for tooltip (optional)

Validation rules applied in `load_peak_bands`:

1. File must exist and parse as JSON. If not → return `[]` and log warning.
2. Must have a `bands` key that is a list. If not → return `[]`.
3. Each band must have `timezone`, `days`, `start`, `end`. Missing bands are dropped individually, not fatal.
4. Invalid IANA timezone → drop the band, log warning.
5. `start` >= `end` → drop the band, log warning (overnight bands are out of scope for this cycle).

### 4.7 Out of scope (explicitly deferred)

The following are NOT part of this cycle and must be resisted during implementation:

- Per-turn drill-down UI (click a session → see individual prompts, tool calls, token breakdown per turn)
- Storing prompt content, tool call arguments, or assistant responses in the database
- Configurable viewer timezone (settings dropdown, URL param, etc.)
- Peak-hour stat cards or peak/off-peak columns in existing tables
- In-dashboard editing of `peak-hours.json`
- Support for overnight peak bands (start > end) or overlapping bands with intensity levels
- Peak-band overlay on the existing daily chart (only the two new charts get it)
- Migration of historical turns re-bucketed on first scan — not needed; conversion happens at read time
- macOS/Linux support for the desktop-metadata source (no known equivalent directory; this feature is Windows-only for now)

## 5. Testing plan

All tests follow the existing `unittest` style in the `tests/` directory.

### 5.1 `test_scanner.py` additions

**`TestReadDesktopMetadata`** (4 cases):

1. `test_reads_valid_metadata_file` — write one fixture JSON, assert parsed correctly and keyed by `cliSessionId`
2. `test_malformed_json_is_skipped` — write one valid + one invalid → only the valid one is returned, no exception raised
3. `test_missing_directory_returns_empty` — point at nonexistent path → returns `{}`
4. `test_missing_cli_session_id_is_skipped` — metadata without `cliSessionId` field is dropped silently

**`TestEnrichSessionsWithDesktopMetadata`** (3 cases):

1. `test_updates_matching_sessions` — insert a session with known id, call enrich, verify title/original_cwd are populated
2. `test_preserves_existing_title_when_metadata_absent` — session already has title, metadata dict is empty → title unchanged
3. `test_does_not_affect_unmatched_sessions` — insert session A, enrich with metadata for session B → A is untouched

**`TestScanIntegrationWithDesktopMetadata`** (1 case):

1. `test_full_scan_enriches_from_desktop_dir` — fixture JSONL + fixture desktop metadata → full scan populates title and original_cwd, token totals are unchanged

### 5.2 `test_dashboard.py` additions

**`TestTimezoneBucketing`** (4 cases):

1. `test_utc_midnight_converts_to_chicago_6pm_previous_day_standard_time` — verify a January timestamp buckets correctly
2. `test_utc_midnight_converts_to_chicago_7pm_previous_day_dst` — verify a July timestamp accounts for CDT
3. `test_dst_spring_forward_boundary` — timestamp at the March DST transition is assigned to the correct local hour
4. `test_dst_fall_back_boundary` — timestamp at the November DST transition is assigned to the correct local hour (ambiguous hour documented, either assignment acceptable)

**`TestHourlyAggregation`** (3 cases):

1. `test_single_turn_lands_in_correct_hour_bucket` — one fixture turn at UTC 14:30 → Chicago hour 8 or 9 depending on DST
2. `test_multiple_turns_same_hour_sum_correctly` — two turns at UTC 14:15 and 14:45 → single hour bucket with summed tokens
3. `test_turns_by_hour_local_structure` — dashboard response includes `turns_by_hour_local` with expected keys

**`TestLoadPeakBands`** (5 cases):

1. `test_valid_file_loads` — happy path
2. `test_missing_file_returns_empty` — no file → `[]`, warning logged
3. `test_malformed_json_returns_empty` — bad JSON → `[]`, warning logged
4. `test_invalid_band_is_dropped` — one valid + one missing `start` → returns only the valid band
5. `test_bad_timezone_is_dropped` — band with `"timezone": "Not/AReal_Zone"` is dropped

## 6. Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| Windows-only metadata path breaks tests on CI (Linux runners) | Tests use `tempfile.mkdtemp()` fixtures, not the real `%APPDATA%`. Production code's Windows-specific path is gated by `exists()` check. |
| DST boundary ambiguity could bucket turns into the wrong hour | `zoneinfo` handles the transition deterministically. Tests cover both spring-forward and fall-back. For the fall-back ambiguous hour, either assignment is accepted since it affects at most one hour per year. |
| The existing daily chart uses UTC grouping; the new hourly charts use Chicago grouping | Document this divergence in a code comment. Consider harmonizing in a follow-up cycle but do not harmonize in this cycle to limit blast radius. |
| Chart.js plugin rendering peak bands miscomputes coordinates | Unit-test the `convertBandToViewerTZ` helper with fixture inputs including DST boundaries. The plugin itself is tested manually via the dashboard. |
| Title enrichment overwrites a user's intended cwd-based grouping | Raw `project_name` is preserved in the session row's `project_raw` field for CSV export and hover tooltip. User can always see the original. |
| Non-Windows users get no desktop metadata benefit | Documented; this is a Windows-only feature for this cycle. Filed as known limitation in the README update. |

## 7. Documentation updates

- **`README.md`** — add a new "Windows: Desktop app session metadata" section explaining what the scanner picks up and why titles may appear for some sessions.
- **`CHANGELOG.md`** — new entry dated 2026-04-10 with bullet points for each feature (desktop metadata enrichment, Chicago hourly charts, custom date range, peak-hour overlay, two new `sessions` columns).
- **`peak-hours.json`** — self-documenting via the `source` and `note` fields.

## 8. Deliverables

At the end of this cycle:

1. `scanner.py` updated with `read_desktop_metadata`, `enrich_sessions_with_desktop_metadata`, schema evolution, and scan-time enrichment
2. `dashboard.py` updated with `to_local_hour`, hourly aggregation, `load_peak_bands`, API response additions, and title-aware sessions_all
3. `dashboard.py` HTML_TEMPLATE updated with: custom date inputs, two new chart canvases, peak-band Chart.js plugin, title-aware Project column, CSV export changes
4. `peak-hours.json` at repo root with default bands
5. `tests/test_scanner.py` with 8 new test cases
6. `tests/test_dashboard.py` with 12 new test cases
7. `README.md` and `CHANGELOG.md` updates
8. All existing tests still passing

## 9. Open questions

None. All design decisions are locked in as of 2026-04-10.
