"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import os
import re
import sys
import sqlite3
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from subscription import (
    load_subscription_config,
    get_week_window,
    calc_pace_ratio,
    pace_color,
    SUBSCRIPTION_PATH,
)

DB_PATH = Path.home() / ".claude" / "usage.db"

# Timezone constants — lazily initialized so dashboard.py remains importable
# on Windows systems without the tzdata package installed. The actual ZoneInfo
# lookups happen inside _get_chicago_tz(), which raises a clear error on first
# use if IANA timezone data is unavailable.
_CHICAGO_TZ = None
_UTC_TZ = None


def _get_chicago_tz():
    """Lazy accessor for the America/Chicago ZoneInfo. Raises a clear,
    actionable error on first use if Windows tzdata is missing."""
    global _CHICAGO_TZ, _UTC_TZ
    if _CHICAGO_TZ is None:
        try:
            _CHICAGO_TZ = ZoneInfo("America/Chicago")
            _UTC_TZ = ZoneInfo("UTC")
        except Exception as e:
            raise RuntimeError(
                "IANA timezone data for America/Chicago is not available. "
                "On Windows, install it with: pip install tzdata"
            ) from e
    return _CHICAGO_TZ, _UTC_TZ


def to_local_hour(iso_utc):
    """Convert a UTC ISO timestamp string to (local_date_str, local_hour_int)
    in America/Chicago. Returns ('', 0) for unparseable input.

    Handles the 'Z' suffix and timezone-aware inputs. DST transitions are
    handled deterministically by zoneinfo.

    Raises RuntimeError on first call if IANA timezone data is unavailable
    (Windows without tzdata). All parse errors return ('', 0) as before.
    """
    if not iso_utc or not isinstance(iso_utc, str):
        return ("", 0)
    chicago, utc = _get_chicago_tz()
    try:
        # Normalize trailing Z to +00:00 for fromisoformat
        normalized = iso_utc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=utc)
        local = dt.astimezone(chicago)
        return (local.strftime("%Y-%m-%d"), local.hour)
    except (ValueError, TypeError):
        return ("", 0)


_REQUIRED_BAND_FIELDS = ("timezone", "days", "start", "end")
_VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_HH_MM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PEAK_HOURS_PATH = Path(__file__).parent / "peak-hours.json"


def load_peak_bands(path=PEAK_HOURS_PATH):
    """Load peak-hours.json and return a list of validated band dicts.

    Silently returns [] on missing file, malformed JSON, or missing
    top-level 'bands' key, logging a single warning to stderr. Individual
    bands that fail validation are dropped from the returned list with a
    warning naming the reason.

    Validation rules:
      - Required fields: timezone, days, start, end
      - timezone must be a valid IANA zone (resolvable by ZoneInfo)
      - days must be a non-empty list of recognized day tokens
        (Mon, Tue, Wed, Thu, Fri, Sat, Sun — case-insensitive)
      - start and end must be 'HH:MM' strings (24-hour, zero-padded)
        with start < end
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"warning: peak-hours.json not found at {path}; overlay disabled", file=sys.stderr)
        return []
    except (OSError, json.JSONDecodeError) as e:
        print(f"warning: peak-hours.json could not be parsed: {e}", file=sys.stderr)
        return []

    if not isinstance(data, dict) or not isinstance(data.get("bands"), list):
        print("warning: peak-hours.json missing 'bands' list; overlay disabled", file=sys.stderr)
        return []

    valid = []
    for i, band in enumerate(data["bands"]):
        if not isinstance(band, dict):
            print(f"warning: peak band #{i} is not a dict; dropped", file=sys.stderr)
            continue
        if any(band.get(k) is None for k in _REQUIRED_BAND_FIELDS):
            print(f"warning: peak band #{i} missing required field; dropped", file=sys.stderr)
            continue
        if not isinstance(band["days"], list) or not band["days"]:
            print(f"warning: peak band #{i} 'days' must be a non-empty list; dropped",
                  file=sys.stderr)
            continue
        if not all(isinstance(d, str) and d.lower()[:3] in _VALID_DAYS for d in band["days"]):
            print(f"warning: peak band #{i} 'days' contains invalid tokens; dropped",
                  file=sys.stderr)
            continue
        try:
            ZoneInfo(band["timezone"])
        except (KeyError, TypeError, ValueError):
            print(f"warning: peak band #{i} has invalid timezone {band['timezone']!r}; dropped",
                  file=sys.stderr)
            continue
        if not (isinstance(band["start"], str) and isinstance(band["end"], str)):
            print(f"warning: peak band #{i} 'start'/'end' must be strings; dropped",
                  file=sys.stderr)
            continue
        if not (_HH_MM_RE.match(band["start"]) and _HH_MM_RE.match(band["end"])):
            print(f"warning: peak band #{i} 'start'/'end' must be HH:MM format; dropped",
                  file=sys.stderr)
            continue
        if band["start"] >= band["end"]:
            print(f"warning: peak band #{i} has start >= end; dropped", file=sys.stderr)
            continue
        valid.append(band)
    return valid


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Ensure schema is current (adds new columns if DB predates them).
    # This makes the dashboard self-healing if run against a DB that hasn't
    # been touched by a newer scanner yet.
    from scanner import init_db
    init_db(conn)

    # ── All models (for filter UI) ────────────────────────────────────────────
    model_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model
        FROM turns
        GROUP BY model
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Single pass: bucket every turn by Chicago-local (day, hour) and model.
    # We build BOTH daily_by_model and turns_by_hour_local from this one pass so
    # the Daily chart and the Hourly charts always agree on where the day starts.
    # Previously daily_by_model used SQL substr(timestamp, 1, 10) which is UTC —
    # that caused late-evening Chicago work to show up on the next calendar day.
    hourly_rows = conn.execute("""
        SELECT timestamp, COALESCE(model, 'unknown') as model,
               input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens
        FROM turns
    """).fetchall()

    def _new_bucket():
        return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "turns": 0}

    hourly_map = {}  # (day_local, hour_local, model) -> counters
    daily_map  = {}  # (day_local, model) -> counters
    for r in hourly_rows:
        day_local, hour_local = to_local_hour(r["timestamp"])
        if not day_local:
            continue
        model = r["model"]
        inp = r["input_tokens"] or 0
        out = r["output_tokens"] or 0
        cr  = r["cache_read_tokens"] or 0
        cc  = r["cache_creation_tokens"] or 0

        hkey = (day_local, hour_local, model)
        hb = hourly_map.get(hkey)
        if hb is None:
            hb = _new_bucket()
            hourly_map[hkey] = hb
        hb["input"] += inp; hb["output"] += out
        hb["cache_read"] += cr; hb["cache_creation"] += cc
        hb["turns"] += 1

        dkey = (day_local, model)
        db = daily_map.get(dkey)
        if db is None:
            db = _new_bucket()
            daily_map[dkey] = db
        db["input"] += inp; db["output"] += out
        db["cache_read"] += cr; db["cache_creation"] += cc
        db["turns"] += 1

    turns_by_hour_local = [
        {"day_local": k[0], "hour_local": k[1], "model": k[2], **v}
        for k, v in hourly_map.items()
    ]

    # Sort daily_by_model the same way the old SQL ORDER BY did (day asc, model asc)
    # so the front-end's existing sort-agnostic code still renders in order.
    daily_by_model = sorted(
        [{"day": k[0], "model": k[1], **v} for k, v in daily_map.items()],
        key=lambda d: (d["day"], d["model"]),
    )

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count,
            title, original_cwd
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()

    sessions_all = []
    chicago_tz, _ = _get_chicago_tz()
    for r in session_rows:
        t1 = t2 = None
        duration_min = 0
        try:
            t1 = datetime.fromisoformat((r["first_timestamp"] or "").replace("Z", "+00:00"))
            t2 = datetime.fromisoformat((r["last_timestamp"] or "").replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            pass

        # Show last-activity timestamp AND group by day in Chicago local time.
        # The old code used (last_timestamp or "")[:10] which was UTC — so a
        # session that ran at 10pm Chicago (=03:00 UTC next day) would show up
        # on the wrong day in both the filter and the "Last Active" column.
        if t2 is not None:
            t2_local = t2.astimezone(chicago_tz)
            last_str = t2_local.strftime("%Y-%m-%d %H:%M")
            last_date = t2_local.strftime("%Y-%m-%d")
        else:
            last_str = ""
            last_date = ""

        project_name = r["project_name"] or "unknown"
        title = r["title"]
        sessions_all.append({
            "session_id":    r["session_id"][:8],
            "project":       title or project_name,
            "project_raw":   project_name,
            "title":         title,
            "original_cwd":  r["original_cwd"],
            "last":          last_str,
            "last_date":     last_date,
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
        })

    conn.close()

    return {
        "all_models":     all_models,
        "daily_by_model": daily_by_model,
        "sessions_all":   sessions_all,
        "turns_by_hour_local": turns_by_hour_local,
        "peak_bands":     load_peak_bands(),
        "viewer_timezone": "America/Chicago",
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2d3a;
    --text: #e2e8f0;
    --muted: #8892a4;
    --accent: #d97757;
    --blue: #4f8ef7;
    --green: #4ade80;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--accent); }
  header .meta { color: var(--muted); font-size: 12px; }
  #rescan-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; margin-top: 4px; }
  #rescan-btn:hover { color: var(--text); border-color: var(--accent); }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  #filter-bar { background: var(--card); border-bottom: 1px solid var(--border); padding: 10px 24px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .filter-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
  #model-checkboxes { display: flex; flex-wrap: wrap; gap: 6px; }
  .model-cb-label { display: flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 20px; border: 1px solid var(--border); cursor: pointer; font-size: 12px; color: var(--muted); transition: border-color 0.15s, color 0.15s, background 0.15s; user-select: none; }
  .model-cb-label:hover { border-color: var(--accent); color: var(--text); }
  .model-cb-label.checked { background: rgba(217,119,87,0.12); border-color: var(--accent); color: var(--text); }
  .model-cb-label input { display: none; }
  .filter-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; white-space: nowrap; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .range-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; flex-shrink: 0; }
  .range-btn { padding: 4px 13px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 12px; cursor: pointer; transition: background 0.15s, color 0.15s; }
  .range-btn:last-child { border-right: none; }
  .range-btn:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .range-btn.active { background: rgba(217,119,87,0.15); color: var(--accent); font-weight: 600; }
  .date-input { background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 3px 8px; border-radius: 4px; font-size: 12px; font-family: inherit; }
  .date-input::-webkit-calendar-picker-indicator { filter: invert(0.7); cursor: pointer; }
  .range-btn.inactive { opacity: 0.4; }
  #clear-custom { padding: 3px 8px; }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }

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

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 240px; }
  .chart-wrap.tall { height: 300px; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  .sort-icon { font-size: 9px; opacity: 0.8; }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(79,142,247,0.15); color: var(--blue); }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .section-header .section-title { margin-bottom: 0; }
  .export-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 3px 10px; border-radius: 5px; cursor: pointer; font-size: 11px; }
  .export-btn:hover { color: var(--text); border-color: var(--accent); }
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; overflow-x: auto; }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 8px; }
  .footer-content { max-width: 1400px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } }

  /* Clickable session rows */
  #sessions-body tr { cursor: pointer; }
  #sessions-body tr:hover td { background: rgba(217,119,87,0.05); }

  /* Drill-down modal */
  .drill-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.75); display: flex; align-items: center; justify-content: center; z-index: 100; padding: 32px; overflow-y: auto; }
  .drill-modal.hidden { display: none; }
  .drill-panel { background: var(--card); border: 1px solid var(--border); border-radius: 10px; max-width: 1100px; width: 100%; max-height: 90vh; display: flex; flex-direction: column; box-shadow: 0 12px 48px rgba(0,0,0,0.6); }
  .drill-header { display: flex; align-items: flex-start; justify-content: space-between; padding: 20px 24px; border-bottom: 1px solid var(--border); }
  .drill-title { font-size: 18px; font-weight: 600; color: var(--accent); }
  .drill-sub { font-size: 12px; margin-top: 4px; }
  .drill-summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; padding: 16px 24px; border-bottom: 1px solid var(--border); background: rgba(0,0,0,0.15); }
  .drill-summary .ds-cell { display: flex; flex-direction: column; gap: 2px; }
  .drill-summary .ds-label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; }
  .drill-summary .ds-value { font-size: 14px; font-weight: 600; font-family: monospace; }
  .drill-body { padding: 12px 24px 24px 24px; overflow-y: auto; flex: 1 1 auto; }
  .drill-body .empty { color: var(--muted); padding: 24px 0; text-align: center; }
  .drill-body .loading { color: var(--muted); padding: 40px 0; text-align: center; }
  .drill-body .err { color: #f87171; padding: 24px 0; }

  .turn-row { border: 1px solid var(--border); border-radius: 6px; margin-bottom: 8px; background: rgba(255,255,255,0.015); }
  .turn-head { display: flex; gap: 12px; align-items: center; padding: 10px 14px; cursor: pointer; user-select: none; }
  .turn-head:hover { background: rgba(255,255,255,0.03); }
  .turn-idx { color: var(--muted); font-family: monospace; font-size: 11px; min-width: 32px; }
  .turn-time { color: var(--muted); font-family: monospace; font-size: 11px; min-width: 155px; }
  .turn-preview { flex: 1; color: var(--text); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .turn-preview.user { color: #a5c8ff; }
  .turn-preview.asst { color: var(--text); }
  .turn-tools { display: flex; gap: 4px; flex-wrap: wrap; }
  .turn-tool { display: inline-block; padding: 1px 7px; border-radius: 10px; background: rgba(79,142,247,0.18); color: var(--blue); font-size: 10px; font-family: monospace; }
  .turn-tokens { font-family: monospace; font-size: 11px; color: var(--muted); min-width: 150px; text-align: right; }
  .turn-cost { font-family: monospace; font-size: 11px; color: var(--green); min-width: 70px; text-align: right; }
  .turn-body { display: none; padding: 0 14px 14px 56px; border-top: 1px solid var(--border); color: var(--muted); font-size: 12px; }
  .turn-row.open .turn-body { display: block; }
  .turn-body .tb-label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; margin-top: 10px; margin-bottom: 4px; }
  .turn-body .tb-text { color: var(--text); white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.5; }
  .turn-body .tb-tools-list { margin: 4px 0 0 0; padding: 0; list-style: none; }
  .turn-body .tb-tools-list li { font-family: monospace; font-size: 11px; color: var(--text); padding: 2px 0; }
  .turn-body .tb-tools-list .tn { color: var(--blue); }
</style>
</head>
<body>
<header>
  <h1>Claude Code Usage Dashboard</h1>
  <div class="meta" id="meta">Loading...</div>
  <button id="rescan-btn" onclick="triggerRescan()" title="Rebuild the database from scratch by re-scanning all JSONL files. Use if data looks stale or costs seem wrong.">&#x21bb; Rescan</button>
</header>

<div id="filter-bar">
  <div class="filter-label">Models</div>
  <div id="model-checkboxes"></div>
  <button class="filter-btn" onclick="selectAllModels()">All</button>
  <button class="filter-btn" onclick="clearAllModels()">None</button>
  <div class="filter-sep"></div>
  <div class="filter-label">Range</div>
  <div class="range-group">
    <button class="range-btn" data-range="7d"  onclick="setRange('7d')">7d</button>
    <button class="range-btn" data-range="30d" onclick="setRange('30d')">30d</button>
    <button class="range-btn" data-range="90d" onclick="setRange('90d')">90d</button>
    <button class="range-btn" data-range="all" onclick="setRange('all')">All</button>
  </div>
  <div class="filter-sep"></div>
  <div class="filter-label">Custom</div>
  <input type="date" id="from-date" class="date-input" onchange="onCustomDateChange()">
  <span class="muted">–</span>
  <input type="date" id="to-date" class="date-input" onchange="onCustomDateChange()">
  <button class="filter-btn" id="clear-custom" onclick="clearCustomDates()" title="Clear custom dates">×</button>
</div>

<div class="container">
  <div id="gauge-container"></div>
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide">
      <h2 id="daily-chart-title">Daily Token Usage</h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card wide">
      <h2 id="hour-histogram-title">Usage by Hour of Day — America/Chicago (averaged)</h2>
      <div class="chart-wrap"><canvas id="chart-hour-histogram"></canvas></div>
    </div>
    <div class="chart-card wide">
      <h2 id="hour-timeline-title">Hourly Timeline — America/Chicago</h2>
      <div class="chart-wrap tall" style="overflow-x: auto;"><canvas id="chart-hour-timeline"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
  </div>
  <div class="table-card">
    <div class="section-title">Cost by Model</div>
    <table>
      <thead><tr>
        <th>Model</th>
        <th class="sortable" onclick="setModelSort('turns')">Turns <span class="sort-icon" id="msort-turns"></span></th>
        <th class="sortable" onclick="setModelSort('input')">Input <span class="sort-icon" id="msort-input"></span></th>
        <th class="sortable" onclick="setModelSort('output')">Output <span class="sort-icon" id="msort-output"></span></th>
        <th class="sortable" onclick="setModelSort('cache_read')">Cache Read <span class="sort-icon" id="msort-cache_read"></span></th>
        <th class="sortable" onclick="setModelSort('cache_creation')">Cache Creation <span class="sort-icon" id="msort-cache_creation"></span></th>
        <th class="sortable" onclick="setModelSort('cost')">Est. Cost <span class="sort-icon" id="msort-cost"></span></th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title">Recent Sessions</div><button class="export-btn" onclick="exportSessionsCSV()" title="Export all filtered sessions to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Session</th>
        <th>Project</th>
        <th class="sortable" onclick="setSessionSort('last')">Last Active <span class="sort-icon" id="sort-icon-last"></span></th>
        <th class="sortable" onclick="setSessionSort('duration_min')">Duration <span class="sort-icon" id="sort-icon-duration_min"></span></th>
        <th>Model</th>
        <th class="sortable" onclick="setSessionSort('turns')">Turns <span class="sort-icon" id="sort-icon-turns"></span></th>
        <th class="sortable" onclick="setSessionSort('input')">Input <span class="sort-icon" id="sort-icon-input"></span></th>
        <th class="sortable" onclick="setSessionSort('output')">Output <span class="sort-icon" id="sort-icon-output"></span></th>
        <th class="sortable" onclick="setSessionSort('cost')">Est. Cost <span class="sort-icon" id="sort-icon-cost"></span></th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title">Cost by Project</div><button class="export-btn" onclick="exportProjectsCSV()" title="Export all projects to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Project</th>
        <th class="sortable" onclick="setProjectSort('sessions')">Sessions <span class="sort-icon" id="psort-sessions"></span></th>
        <th class="sortable" onclick="setProjectSort('turns')">Turns <span class="sort-icon" id="psort-turns"></span></th>
        <th class="sortable" onclick="setProjectSort('input')">Input <span class="sort-icon" id="psort-input"></span></th>
        <th class="sortable" onclick="setProjectSort('output')">Output <span class="sort-icon" id="psort-output"></span></th>
        <th class="sortable" onclick="setProjectSort('cost')">Est. Cost <span class="sort-icon" id="psort-cost"></span></th>
      </tr></thead>
      <tbody id="project-cost-body"></tbody>
    </table>
  </div>
</div>

<!-- Session drill-down modal -->
<div id="drill-modal" class="drill-modal hidden" onclick="onDrillBackdrop(event)">
  <div class="drill-panel" role="dialog" aria-modal="true">
    <div class="drill-header">
      <div>
        <div id="drill-title" class="drill-title">Session</div>
        <div id="drill-sub" class="drill-sub muted"></div>
      </div>
      <button class="filter-btn" onclick="closeDrill()">×</button>
    </div>
    <div id="drill-summary" class="drill-summary"></div>
    <div id="drill-body" class="drill-body"></div>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of April 2026. Only models containing <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> in the name are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.</p>
    <p>
      GitHub: <a href="https://github.com/phuryn/claude-usage" target="_blank">https://github.com/phuryn/claude-usage</a>
      &nbsp;&middot;&nbsp;
      Created by: <a href="https://www.productcompass.pm" target="_blank">The Product Compass Newsletter</a>
      &nbsp;&middot;&nbsp;
      License: MIT
    </p>
  </div>
</footer>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let selectedRange = '30d';
let customFrom = null;  // 'YYYY-MM-DD' or null
let peakBands = [];
let customTo   = null;
let charts = {};
let sessionSortCol = 'last';
let modelSortCol = 'cost';
let modelSortDir = 'desc';
let projectSortCol = 'cost';
let projectSortDir = 'desc';
let lastFilteredSessions = [];
let lastByProject = [];
let sessionSortDir = 'desc';

// ── Pricing (Anthropic API, April 2026) ────────────────────────────────────
const PRICING = {
  'claude-opus-4-6':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-5':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-sonnet-4-6': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-sonnet-4-5': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-haiku-4-5':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
  'claude-haiku-4-6':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
};

function isBillable(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('opus') || m.includes('sonnet') || m.includes('haiku');
}

function getPricing(model) {
  if (!model) return null;
  if (PRICING[model]) return PRICING[model];
  for (const key of Object.keys(PRICING)) {
    if (model.startsWith(key)) return PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('opus'))   return PRICING['claude-opus-4-6'];
  if (m.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku'))  return PRICING['claude-haiku-4-5'];
  return null;
}

function calcCost(model, inp, out, cacheRead, cacheCreation) {
  if (!isBillable(model)) return 0;
  const p = getPricing(model);
  if (!p) return 0;
  return (
    inp           * p.input       / 1e6 +
    out           * p.output      / 1e6 +
    cacheRead     * p.cache_read  / 1e6 +
    cacheCreation * p.cache_write / 1e6
  );
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toFixed(4); }
function fmtCostBig(c) { return '$' + c.toFixed(2); }

// ── Chart colors ───────────────────────────────────────────────────────────
const TOKEN_COLORS = {
  input:          'rgba(79,142,247,0.8)',
  output:         'rgba(167,139,250,0.8)',
  cache_read:     'rgba(74,222,128,0.6)',
  cache_creation: 'rgba(251,191,36,0.6)',
};
const MODEL_COLORS = ['#d97757','#4f8ef7','#4ade80','#a78bfa','#fbbf24','#f472b6','#34d399','#60a5fa'];

// Chart.js plugin that draws peak-hour bands in the chart area.
// Each chart that wants bands passes options.plugins.peakBands.mode ('histogram' or 'timeline').
const peakBandPlugin = {
  id: 'peakBands',
  beforeDatasetsDraw(chart, args, pluginOpts) {
    if (!peakBands.length) return;
    const mode = pluginOpts && pluginOpts.mode;
    if (!mode) return;
    const ctx = chart.ctx;
    const xAxis = chart.scales.x;
    const yAxis = chart.scales.y;
    if (!xAxis || !yAxis) return;

    ctx.save();
    ctx.fillStyle = 'rgba(217,119,87,0.10)';

    if (mode === 'histogram') {
      // Histogram x-axis is 24 labels "00:00".."23:00". Draw one band per
      // configured peak, using a recent weekday as the reference day.
      const refDay = getRecentWeekday();
      for (const band of peakBands) {
        const range = convertBandForDay(band, refDay);
        if (!range) continue;
        const startLabel = String(Math.floor(range.start)).padStart(2,'0') + ':00';
        const endLabel   = String(Math.floor(range.end)).padStart(2,'0') + ':00';
        const xStart = xAxis.getPixelForValue(startLabel);
        const xEnd   = xAxis.getPixelForValue(endLabel);
        ctx.fillRect(xStart, yAxis.top, xEnd - xStart, yAxis.bottom - yAxis.top);
      }
    } else if (mode === 'timeline') {
      // Timeline has one label per (day, hour). Shade each bar whose hour falls
      // inside a band for that bar's day-of-week.
      const timelineData = pluginOpts.timelineData;
      if (!timelineData) { ctx.restore(); return; }
      const labels = chart.data.labels;
      for (let i = 0; i < labels.length; i++) {
        const b = timelineData[i];
        if (!b) continue;
        for (const band of peakBands) {
          const range = convertBandForDay(band, b.day);
          if (!range) continue;
          const hour = b.hour;
          if (hour >= Math.floor(range.start) && hour < Math.ceil(range.end)) {
            const x = xAxis.getPixelForValue(labels[i]);
            const nextLabel = labels[Math.min(i+1, labels.length-1)];
            const barWidth = xAxis.getPixelForValue(nextLabel) - x;
            ctx.fillRect(x - barWidth/2, yAxis.top, barWidth, yAxis.bottom - yAxis.top);
            break;
          }
        }
      }
    }
    ctx.restore();
  }
};

Chart.register(peakBandPlugin);

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time', 'custom': 'Custom Range' };
const RANGE_TICKS  = { '7d': 7, '30d': 15, '90d': 13, 'all': 12, 'custom': 15 };

function currentRangeLabel() {
  if (selectedRange === 'custom') {
    const from = customFrom || '(start)';
    const to   = customTo   || '(today)';
    return from + ' → ' + to;
  }
  return RANGE_LABELS[selectedRange] || 'Range';
}

// Convert a peak band to viewer-local (Chicago) hour range for a given day.
// Uses Intl.DateTimeFormat for cross-timezone conversion — no external libraries.
// Returns {start: float, end: float} in decimal hours (viewer time), or null if
// the band doesn't apply to the given day-of-week in viewer time.
function convertBandForDay(band, dayStr) {
  // dayStr = 'YYYY-MM-DD' interpreted in viewer time.
  const [y, m, d] = dayStr.split('-').map(Number);
  // Create a date at noon to safely determine day-of-week without DST edge cases
  const localNoon = new Date(Date.UTC(y, m-1, d, 18, 0, 0));
  const dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const dowViewer = dayNames[localNoon.getUTCDay()];
  if (!band.days.map(d => d.slice(0,3)).includes(dowViewer)) return null;

  const [sh, sm] = band.start.split(':').map(Number);
  const [eh, em] = band.end.split(':').map(Number);

  const bandStartUTC = zonedTimeToUTC(y, m, d, sh, sm, band.timezone);
  const bandEndUTC   = zonedTimeToUTC(y, m, d, eh, em, band.timezone);

  const startLocal = utcToViewerHour(bandStartUTC, 'America/Chicago');
  const endLocal   = utcToViewerHour(bandEndUTC,   'America/Chicago');
  return { start: startLocal, end: endLocal };
}

// Given Y/M/D and H/M in `tz`, return the corresponding UTC Date.
// Two-pass technique: assume the wall clock is UTC, measure the offset in tz, correct.
function zonedTimeToUTC(y, mo, d, h, mi, tz) {
  const guess = new Date(Date.UTC(y, mo-1, d, h, mi, 0));
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).formatToParts(guess);
  const p = Object.fromEntries(parts.filter(x => x.type !== 'literal').map(x => [x.type, parseInt(x.value, 10)]));
  const asUTCOfTZ = Date.UTC(p.year, p.month-1, p.day, p.hour === 24 ? 0 : p.hour, p.minute, p.second);
  const offsetMs = asUTCOfTZ - guess.getTime();
  return new Date(guess.getTime() - offsetMs);
}

// Convert a UTC Date to a decimal hour in viewer tz (e.g. 7.5 for 7:30am).
function utcToViewerHour(utcDate, tz) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hour12: false,
    hour: '2-digit', minute: '2-digit',
  }).formatToParts(utcDate);
  const p = Object.fromEntries(parts.filter(x => x.type !== 'literal').map(x => [x.type, parseInt(x.value, 10)]));
  const hour = p.hour === 24 ? 0 : p.hour;
  return hour + (p.minute / 60);
}

// Format a Date as YYYY-MM-DD using LOCAL getters (NOT toISOString, which is UTC).
// Matches the Chicago-local day keys the server ships in daily_by_model / sessions_all.
function formatLocalYMD(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return y + '-' + m + '-' + day;
}

// Return a recent weekday (Mon-Fri) in local YYYY-MM-DD format for the histogram
// peak band reference day. Using local getters so day-of-week lines up with the
// viewer's actual week.
function getRecentWeekday() {
  const d = new Date();
  while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() - 1);
  return formatLocalYMD(d);
}

function getRangeCutoff(range) {
  if (range === 'all') return null;
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return formatLocalYMD(d);
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return ['7d', '30d', '90d', 'all'].includes(p) ? p : '30d';
}

function readURLCustomDates() {
  const p = new URLSearchParams(window.location.search);
  return {
    from: p.get('from'),
    to:   p.get('to'),
  };
}

function setRange(range) {
  selectedRange = range;
  customFrom = null;
  customTo = null;
  document.getElementById('from-date').value = '';
  document.getElementById('to-date').value = '';
  document.getElementById('from-date').max = '';
  document.getElementById('to-date').min = '';
  document.querySelectorAll('.range-btn').forEach(btn => {
    btn.classList.remove('inactive');
    btn.classList.toggle('active', btn.dataset.range === range);
  });
  updateURL();
  applyFilter();
}

function onCustomDateChange() {
  const fromEl = document.getElementById('from-date');
  const toEl   = document.getElementById('to-date');
  customFrom = fromEl.value || null;
  customTo   = toEl.value || null;
  // Prevent inverted ranges via native min/max on the counterpart input
  fromEl.max = customTo || '';
  toEl.min = customFrom || '';
  // If either is set, deactivate preset buttons
  const anyCustom = customFrom || customTo;
  document.querySelectorAll('.range-btn').forEach(btn => {
    btn.classList.toggle('inactive', !!anyCustom);
    btn.classList.toggle('active', !anyCustom && btn.dataset.range === selectedRange);
  });
  if (anyCustom) selectedRange = 'custom';
  updateURL();
  applyFilter();
}

function clearCustomDates() {
  document.getElementById('from-date').value = '';
  document.getElementById('to-date').value = '';
  document.getElementById('from-date').max = '';
  document.getElementById('to-date').min = '';
  customFrom = null;
  customTo = null;
  selectedRange = '30d';
  document.querySelectorAll('.range-btn').forEach(btn => {
    btn.classList.remove('inactive');
    btn.classList.toggle('active', btn.dataset.range === '30d');
  });
  updateURL();
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('opus'))   return 0;
  if (ml.includes('sonnet')) return 1;
  if (ml.includes('haiku'))  return 2;
  return 3;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) return new Set(allModels.filter(m => isBillable(m)));
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  if (selectedModels.size !== billable.length) return false;
  return billable.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  const sorted = [...allModels].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
  selectedModels = readURLModels(allModels);
  const container = document.getElementById('model-checkboxes');
  container.innerHTML = sorted.map(m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(m)}">
      <input type="checkbox" value="${esc(m)}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      ${esc(m)}
    </label>`;
  }).join('');
}

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateURL(); applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (customFrom || customTo) {
    if (customFrom) params.set('from', customFrom);
    if (customTo)   params.set('to', customTo);
  } else if (selectedRange !== '30d') {
    params.set('range', selectedRange);
  }
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

// ── Session sort ───────────────────────────────────────────────────────────
function setSessionSort(col) {
  if (sessionSortCol === col) {
    sessionSortDir = sessionSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    sessionSortCol = col;
    sessionSortDir = 'desc';
  }
  updateSortIcons();
  applyFilter();
}

function updateSortIcons() {
  document.querySelectorAll('.sort-icon').forEach(el => el.textContent = '');
  const icon = document.getElementById('sort-icon-' + sessionSortCol);
  if (icon) icon.textContent = sessionSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortSessions(sessions) {
  return [...sessions].sort((a, b) => {
    let av, bv;
    if (sessionSortCol === 'cost') {
      av = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation);
      bv = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation);
    } else if (sessionSortCol === 'duration_min') {
      av = parseFloat(a.duration_min) || 0;
      bv = parseFloat(b.duration_min) || 0;
    } else {
      av = a[sessionSortCol] ?? 0;
      bv = b[sessionSortCol] ?? 0;
    }
    if (av < bv) return sessionSortDir === 'desc' ? 1 : -1;
    if (av > bv) return sessionSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

// ── Aggregation & filtering ────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  // Day keys in daily_by_model and sessions_all are now Chicago-local YYYY-MM-DD
  // (converted in Python via to_local_hour / astimezone in get_dashboard_data).
  // getRangeCutoff() and the custom picker both yield local YYYY-MM-DD, so the
  // lexicographic string comparison in inRange() is apples-to-apples.
  // Compute date range: custom overrides preset
  const isCustom = customFrom || customTo;
  const rangeFrom = isCustom ? customFrom : getRangeCutoff(selectedRange);
  const rangeTo = isCustom ? customTo : null;

  const inRange = (day) => {
    if (rangeFrom && day < rangeFrom) return false;
    if (rangeTo && day > rangeTo) return false;
    return true;
  };

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && inRange(r.day)
  );

  // Daily chart: aggregate by day
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  // By model: aggregate tokens + turns from daily data
  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0 };
    const m = modelMap[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.turns          += r.turns;
  }

  // Filter sessions by model + date range
  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && inRange(s.last_date)
  );

  // Add session counts into modelMap
  for (const s of filteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // ── Hour-of-day histogram: average tokens per hour across distinct days ──
  const filteredHourly = rawData.turns_by_hour_local.filter(r =>
    selectedModels.has(r.model) && inRange(r.day_local)
  );
  const hourBuckets = Array.from({length: 24}, (_, h) => ({
    hour: h, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0,
  }));
  const daysWithData = new Set();
  for (const r of filteredHourly) {
    daysWithData.add(r.day_local);
    const b = hourBuckets[r.hour_local];
    b.input          += r.input;
    b.output         += r.output;
    b.cache_read     += r.cache_read;
    b.cache_creation += r.cache_creation;
    b.turns          += r.turns;
  }
  const nDays = Math.max(daysWithData.size, 1);
  const hourHistogram = hourBuckets.map(b => ({
    hour:           b.hour,
    input:          b.input / nDays,
    output:         b.output / nDays,
    cache_read:     b.cache_read / nDays,
    cache_creation: b.cache_creation / nDays,
    turns:          b.turns / nDays,
  }));

  // ── Hourly timeline: one bar per (day, hour) in chronological order ──
  const timelineMap = {};  // "YYYY-MM-DD HH" -> bucket
  for (const r of filteredHourly) {
    const key = r.day_local + ' ' + String(r.hour_local).padStart(2, '0');
    if (!timelineMap[key]) {
      timelineMap[key] = {
        key, day: r.day_local, hour: r.hour_local,
        input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0,
      };
    }
    const b = timelineMap[key];
    b.input          += r.input;
    b.output         += r.output;
    b.cache_read     += r.cache_read;
    b.cache_creation += r.cache_creation;
    b.turns          += r.turns;
  }
  const hourTimeline = Object.values(timelineMap).sort((a, b) => a.key.localeCompare(b.key));

  // By project: aggregate from filtered sessions
  const projMap = {};
  for (const s of filteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const p = projMap[s.project];
    p.input          += s.input;
    p.output         += s.output;
    p.cache_read     += s.cache_read;
    p.cache_creation += s.cache_creation;
    p.turns          += s.turns;
    p.sessions++;
    p.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // Totals
  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation), 0),
  };

  // Update daily chart title
  document.getElementById('daily-chart-title').textContent = 'Daily Token Usage \u2014 ' + currentRangeLabel();

  renderStats(totals);
  fetchAndRenderGauge();
  renderDailyChart(daily);
  renderHourHistogram(hourHistogram);
  renderHourTimeline(hourTimeline);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  lastFilteredSessions = sortSessions(filteredSessions);
  lastByProject = sortProjects(byProject);
  renderSessionsTable(lastFilteredSessions.slice(0, 20));
  renderModelCostTable(byModel);
  renderProjectCostTable(lastByProject.slice(0, 20));
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeLabel = currentRangeLabel().toLowerCase();
  const stats = [
    { label: 'Sessions',       value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'Turns',          value: fmt(t.turns),                sub: rangeLabel },
    { label: 'Input Tokens',   value: fmt(t.input),                sub: rangeLabel },
    { label: 'Output Tokens',  value: fmt(t.output),               sub: rangeLabel },
    { label: 'Cache Read',     value: fmt(t.cache_read),           sub: 'from prompt cache' },
    { label: 'Cache Creation', value: fmt(t.cache_creation),       sub: 'writes to prompt cache' },
    { label: 'Est. Cost',      value: fmtCostBig(t.cost),          sub: 'API pricing, Apr 2026', color: '#4ade80' },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${esc(s.value)}</div>
      ${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}
    </div>
  `).join('');
}

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

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { label: 'Input',          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'tokens' },
        { label: 'Output',         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'tokens' },
        { label: 'Cache Read',     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      onClick: (evt, elements) => {
        if (!elements.length) return;
        const idx = elements[0].index;
        const day = daily[idx]?.day;
        if (day) applyClickFilter(day, day);
      },
      onHover: (evt, elements) => {
        evt.native.target.style.cursor = elements.length ? 'pointer' : 'default';
      },
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

// Click-through from charts: set custom range to the clicked day and apply.
function applyClickFilter(fromDay, toDay) {
  document.getElementById('from-date').value = fromDay;
  document.getElementById('to-date').value = toDay;
  customFrom = fromDay;
  customTo = toDay;
  selectedRange = 'custom';
  document.querySelectorAll('.range-btn').forEach(btn => {
    btn.classList.add('inactive');
    btn.classList.remove('active');
  });
  document.getElementById('from-date').max = toDay;
  document.getElementById('to-date').min = fromDay;
  updateURL();
  applyFilter();
  // Note: we intentionally do NOT scrollIntoView here — the user is already
  // looking at the chart they clicked, and the stats row at the top updates
  // to reflect the new totals. Jumping the page would be disorienting.
}

function renderHourHistogram(hourly) {
  const ctx = document.getElementById('chart-hour-histogram').getContext('2d');
  if (charts.hourHistogram) charts.hourHistogram.destroy();
  charts.hourHistogram = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: hourly.map(h => String(h.hour).padStart(2, '0') + ':00'),
      datasets: [
        { label: 'Input',          data: hourly.map(h => h.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'tokens' },
        { label: 'Output',         data: hourly.map(h => h.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'tokens' },
        { label: 'Cache Read',     data: hourly.map(h => h.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', data: hourly.map(h => h.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#8892a4', boxWidth: 12 } },
        peakBands: { mode: 'histogram' },
      },
      scales: {
        x: { ticks: { color: '#8892a4' }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

function renderHourTimeline(timeline) {
  const ctx = document.getElementById('chart-hour-timeline').getContext('2d');
  if (charts.hourTimeline) charts.hourTimeline.destroy();
  if (!timeline.length) { charts.hourTimeline = null; return; }
  // Compact label: "MM-DD HH" (e.g. "04-10 15")
  const labels = timeline.map(b => b.day.slice(5) + ' ' + String(b.hour).padStart(2, '0'));
  // Scale canvas width for many bars (approx 12px per bar)
  const canvas = document.getElementById('chart-hour-timeline');
  const minWidth = Math.max(800, timeline.length * 12);
  canvas.style.width = minWidth + 'px';

  charts.hourTimeline = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Input',          data: timeline.map(b => b.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'tokens' },
        { label: 'Output',         data: timeline.map(b => b.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'tokens' },
        { label: 'Cache Read',     data: timeline.map(b => b.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', data: timeline.map(b => b.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      onClick: (evt, elements) => {
        if (!elements.length) return;
        const idx = elements[0].index;
        const b = timeline[idx];
        if (b?.day) applyClickFilter(b.day, b.day);
      },
      onHover: (evt, elements) => {
        evt.native.target.style.cursor = elements.length ? 'pointer' : 'default';
      },
      plugins: {
        legend: { labels: { color: '#8892a4', boxWidth: 12 } },
        peakBands: { mode: 'timeline', timelineData: timeline },
        tooltip: {
          callbacks: {
            title: items => {
              if (!items.length) return '';
              const b = timeline[items[0].dataIndex];
              return b.day + ' ' + String(b.hour).padStart(2, '0') + ':00 CT (click to drill into this day)';
            }
          }
        }
      },
      scales: {
        x: { ticks: { color: '#8892a4', maxRotation: 0, autoSkip: true, autoSkipPadding: 20 }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

function renderModelChart(byModel) {
  const ctx = document.getElementById('chart-model').getContext('2d');
  if (charts.model) charts.model.destroy();
  if (!byModel.length) { charts.model = null; return; }
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, borderWidth: 2, borderColor: '#1a1d27' }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: '#8892a4', boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => p.project.length > 22 ? '\u2026' + p.project.slice(-20) : p.project),
      datasets: [
        { label: 'Input',  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input },
        { label: 'Output', data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', font: { size: 11 } }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

function renderSessionsTable(sessions) {
  document.getElementById('sessions-body').innerHTML = sessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr onclick="openDrill('${esc(s.session_id)}')" title="Click to see turn-by-turn breakdown">
      <td class="muted" style="font-family:monospace">${esc(s.session_id)}&hellip;</td>
      <td title="${esc(s.project_raw || '')}">${esc(s.project)}</td>
      <td class="muted">${esc(s.last)}</td>
      <td class="muted">${esc(s.duration_min)}m</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

// ── Session drill-down modal ─────────────────────────────────────────────
async function openDrill(sessionIdPrefix) {
  const modal = document.getElementById('drill-modal');
  modal.classList.remove('hidden');
  // Lock body scroll so the fixed-position modal sits in the viewport cleanly
  document.body.style.overflow = 'hidden';
  document.getElementById('drill-title').textContent = 'Loading…';
  document.getElementById('drill-sub').textContent = sessionIdPrefix;
  document.getElementById('drill-summary').innerHTML = '';
  document.getElementById('drill-body').innerHTML = '<div class="loading">Fetching session…</div>';
  // ESC to close
  document.addEventListener('keydown', onDrillKey);
  try {
    const resp = await fetch('/api/session/' + encodeURIComponent(sessionIdPrefix));
    const data = await resp.json();
    if (data.error) {
      document.getElementById('drill-body').innerHTML = '<div class="err">' + esc(data.error) + '</div>';
      document.getElementById('drill-title').textContent = 'Error';
      return;
    }
    renderDrill(data);
  } catch (e) {
    document.getElementById('drill-body').innerHTML = '<div class="err">' + esc(String(e)) + '</div>';
  }
}

function closeDrill() {
  document.getElementById('drill-modal').classList.add('hidden');
  document.body.style.overflow = '';
  document.removeEventListener('keydown', onDrillKey);
}

function onDrillBackdrop(e) {
  if (e.target.id === 'drill-modal') closeDrill();
}

function onDrillKey(e) {
  if (e.key === 'Escape') closeDrill();
}

function renderDrill(d) {
  const titleLabel = d.title || d.project_name || 'Session';
  document.getElementById('drill-title').textContent = titleLabel;
  const cwdInfo = d.original_cwd || d.project_name || '';
  const branch = d.git_branch ? ' · ' + esc(d.git_branch) : '';
  document.getElementById('drill-sub').innerHTML =
    esc(d.session_id_short) + ' · ' + esc(d.first_timestamp) + ' → ' + esc(d.last_timestamp)
    + branch + ' · <span style="font-family:monospace">' + esc(cwdInfo) + '</span>';

  const totalCost = calcCost(d.model, d.total_input_tokens, d.total_output_tokens, d.total_cache_read, d.total_cache_creation);
  const summaryCells = [
    ['Turns',          fmt(d.turn_count || d.turns.length)],
    ['Input',          fmt(d.total_input_tokens)],
    ['Output',         fmt(d.total_output_tokens)],
    ['Cache read',     fmt(d.total_cache_read)],
    ['Cache write',    fmt(d.total_cache_creation)],
    ['Model',          d.model || 'unknown'],
    ['Est. cost',      fmtCost(totalCost)],
  ];
  document.getElementById('drill-summary').innerHTML = summaryCells.map(([label, value]) =>
    `<div class="ds-cell"><div class="ds-label">${esc(label)}</div><div class="ds-value">${esc(value)}</div></div>`
  ).join('');

  if (!d.turns.length) {
    document.getElementById('drill-body').innerHTML = '<div class="empty">No turns recorded in this session.</div>';
    return;
  }

  const trimmedNotice = d.trimmed
    ? '<div class="muted" style="padding:8px 0;font-size:11px">Showing the most recent turns (large session truncated).</div>'
    : '';

  const rows = d.turns.map((t, i) => {
    const cost = calcCost(t.model, t.input_tokens, t.output_tokens, t.cache_read, t.cache_creation);
    const toolsBar = (t.tools || []).slice(0, 5).map(tc =>
      `<span class="turn-tool" title="${esc(tc.summary || '')}">${esc(tc.name)}</span>`
    ).join('');
    const userPreview = t.user_preview ? t.user_preview : (t.assistant_preview || '(no preview)');
    const previewClass = t.user_preview ? 'user' : 'asst';
    const tokens = `${fmt(t.input_tokens)}/${fmt(t.output_tokens)}`;
    const cacheRead = t.cache_read ? ' cr:' + fmt(t.cache_read) : '';
    return `
      <div class="turn-row" id="turn-${i}">
        <div class="turn-head" onclick="document.getElementById('turn-${i}').classList.toggle('open')">
          <div class="turn-idx">#${i+1}</div>
          <div class="turn-time">${esc(t.local_time)}</div>
          <div class="turn-preview ${previewClass}">${esc(userPreview)}</div>
          <div class="turn-tools">${toolsBar}</div>
          <div class="turn-tokens">${esc(tokens)}${esc(cacheRead)}</div>
          <div class="turn-cost">${fmtCost(cost)}</div>
        </div>
        <div class="turn-body">
          ${t.user_preview ? `<div class="tb-label">User prompt</div><div class="tb-text">${esc(t.user_preview)}</div>` : ''}
          <div class="tb-label">Assistant</div><div class="tb-text">${esc(t.assistant_preview || '(no content)')}</div>
          ${(t.tools && t.tools.length) ? `<div class="tb-label">Tool calls</div><ul class="tb-tools-list">${
            t.tools.map(tc => `<li><span class="tn">${esc(tc.name)}</span> ${esc(tc.summary || '')}</li>`).join('')
          }</ul>` : ''}
        </div>
      </div>`;
  }).join('');

  document.getElementById('drill-body').innerHTML = trimmedNotice + rows;
}

function setModelSort(col) {
  if (modelSortCol === col) {
    modelSortDir = modelSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    modelSortCol = col;
    modelSortDir = 'desc';
  }
  updateModelSortIcons();
  applyFilter();
}

function updateModelSortIcons() {
  document.querySelectorAll('[id^="msort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('msort-' + modelSortCol);
  if (icon) icon.textContent = modelSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortModels(byModel) {
  return [...byModel].sort((a, b) => {
    let av, bv;
    if (modelSortCol === 'cost') {
      av = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation);
      bv = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation);
    } else {
      av = a[modelSortCol] ?? 0;
      bv = b[modelSortCol] ?? 0;
    }
    if (av < bv) return modelSortDir === 'desc' ? 1 : -1;
    if (av > bv) return modelSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderModelCostTable(byModel) {
  document.getElementById('model-cost-body').innerHTML = sortModels(byModel).map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    const costCell = isBillable(m.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td><span class="model-tag">${esc(m.model)}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(m.input)}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

// ── Project cost table sorting ────────────────────────────────────────────
function setProjectSort(col) {
  if (projectSortCol === col) {
    projectSortDir = projectSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    projectSortCol = col;
    projectSortDir = 'desc';
  }
  updateProjectSortIcons();
  applyFilter();
}

function updateProjectSortIcons() {
  document.querySelectorAll('[id^="psort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('psort-' + projectSortCol);
  if (icon) icon.textContent = projectSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjects(byProject) {
  return [...byProject].sort((a, b) => {
    const av = a[projectSortCol] ?? 0;
    const bv = b[projectSortCol] ?? 0;
    if (av < bv) return projectSortDir === 'desc' ? 1 : -1;
    if (av > bv) return projectSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectCostTable(byProject) {
  document.getElementById('project-cost-body').innerHTML = sortProjects(byProject).map(p => {
    return `<tr>
      <td>${esc(p.project)}</td>
      <td class="num">${p.sessions}</td>
      <td class="num">${fmt(p.turns)}</td>
      <td class="num">${fmt(p.input)}</td>
      <td class="num">${fmt(p.output)}</td>
      <td class="cost">${fmtCost(p.cost)}</td>
    </tr>`;
  }).join('');
}

// ── CSV Export ────────────────────────────────────────────────────────────
function csvField(val) {
  const s = String(val);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function csvTimestamp() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
    + '_' + String(d.getHours()).padStart(2,'0') + String(d.getMinutes()).padStart(2,'0');
}

function downloadCSV(reportType, header, rows) {
  const lines = [header.map(csvField).join(',')];
  for (const row of rows) {
    lines.push(row.map(csvField).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = reportType + '_' + csvTimestamp() + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function exportSessionsCSV() {
  const header = ['Session', 'Title', 'Project (cwd-derived)', 'Last Active', 'Duration (min)', 'Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastFilteredSessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    return [s.session_id, s.title || '', s.project_raw || s.project, s.last, s.duration_min, s.model, s.turns, s.input, s.output, s.cache_read, s.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('sessions', header, rows);
}

function exportProjectsCSV() {
  const header = ['Project', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProject.map(p => {
    return [p.project, p.sessions, p.turns, p.input, p.output, p.cache_read, p.cache_creation, p.cost.toFixed(4)];
  });
  downloadCSV('projects', header, rows);
}

// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  const btn = document.getElementById('rescan-btn');
  btn.disabled = true;
  btn.textContent = '\u21bb Scanning...';
  try {
    const resp = await fetch('/api/rescan', { method: 'POST' });
    const d = await resp.json();
    btn.textContent = '\u21bb Rescan (' + d.new + ' new, ' + d.updated + ' updated)';
    await loadData();
  } catch(e) {
    btn.textContent = '\u21bb Rescan (error)';
    console.error(e);
  }
  setTimeout(() => { btn.textContent = '\u21bb Rescan'; btn.disabled = false; }, 3000);
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      document.body.innerHTML = '<div style="padding:40px;color:#f87171">' + esc(d.error) + '</div>';
      return;
    }
    document.getElementById('meta').textContent = 'Updated: ' + d.generated_at + ' \u00b7 Auto-refresh in 30s';

    const isFirstLoad = rawData === null;
    rawData = d;
    peakBands = d.peak_bands || [];

    if (isFirstLoad) {
      // Restore range from URL, mark active button
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
      // Restore custom date range from URL if present
      const urlCustom = readURLCustomDates();
      if (urlCustom.from || urlCustom.to) {
        customFrom = urlCustom.from;
        customTo = urlCustom.to;
        if (customFrom) document.getElementById('from-date').value = customFrom;
        if (customTo)   document.getElementById('to-date').value = customTo;
        if (customTo)   document.getElementById('from-date').max = customTo;
        if (customFrom) document.getElementById('to-date').min = customFrom;
        selectedRange = 'custom';
        document.querySelectorAll('.range-btn').forEach(btn => {
          btn.classList.add('inactive');
          btn.classList.remove('active');
        });
      }
      // Build model filter (reads URL for model selection too)
      buildFilterUI(d.all_models);
      updateSortIcons();
      updateModelSortIcons();
      updateProjectSortIcons();
    }

    applyFilter();
  } catch(e) {
    console.error(e);
  }
}

loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>
"""


def _preview_from_content(content, max_len=400):
    """Extract a short text preview from a Claude message.content field.
    content may be a string, a list of blocks (dicts with 'type': 'text'/'tool_use'/etc),
    or None. Returns at most max_len characters.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        s = content.strip()
        return s[:max_len] + ("…" if len(s) > max_len else "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "text":
                parts.append(str(item.get("text", "")).strip())
            elif t == "tool_use":
                name = item.get("name", "?")
                parts.append(f"[tool_use: {name}]")
            elif t == "tool_result":
                rc = item.get("content", "")
                if isinstance(rc, list):
                    rc = " ".join(
                        str(x.get("text", "")) for x in rc if isinstance(x, dict)
                    )
                parts.append(f"[tool_result] {str(rc)[:200]}")
            elif t == "thinking":
                parts.append(f"[thinking] {str(item.get('thinking',''))[:200]}")
        joined = " ".join(p for p in parts if p).strip()
        return joined[:max_len] + ("…" if len(joined) > max_len else "")
    return ""


def _tool_calls_from_content(content):
    """Return a list of {name, input_summary} for tool_use blocks in content."""
    calls = []
    if not isinstance(content, list):
        return calls
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            name = item.get("name", "?")
            inp = item.get("input", {})
            # Produce a short input summary without shipping huge payloads
            summary = ""
            if isinstance(inp, dict):
                for k in ("file_path", "path", "command", "url", "query", "pattern"):
                    if k in inp:
                        summary = f"{k}={str(inp[k])[:160]}"
                        break
                if not summary:
                    keys = ",".join(list(inp.keys())[:4])
                    summary = f"keys=[{keys}]"
            calls.append({"name": name, "summary": summary})
    return calls


def read_session_turns(session_id, db_path=DB_PATH, max_turns=500):
    """Read a session's JSONL file and return turn-by-turn drill-down data.

    Looks up jsonl_path from the sessions table, parses the JSONL, and
    produces a list of turn objects with prompt previews, tool calls,
    tokens, and local-time stamps. Returns an error dict if the session
    or file is missing/unreadable.
    """
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    from scanner import init_db
    init_db(conn)

    # Accept either a full session_id or the 8-char prefix shown in the UI
    row = conn.execute("""
        SELECT session_id, title, original_cwd, project_name, git_branch,
               first_timestamp, last_timestamp, total_input_tokens,
               total_output_tokens, total_cache_read, total_cache_creation,
               model, turn_count, jsonl_path
        FROM sessions
        WHERE session_id = ? OR session_id LIKE ?
        LIMIT 1
    """, (session_id, session_id + "%")).fetchone()
    conn.close()

    if row is None:
        return {"error": f"Session {session_id!r} not found"}

    jsonl_path = row["jsonl_path"]
    if not jsonl_path:
        return {
            "error": "No JSONL path recorded for this session. "
                     "Run `python cli.py scan` to populate it."
        }
    if not Path(jsonl_path).exists():
        return {"error": f"JSONL file missing on disk: {jsonl_path}"}

    # Parse the JSONL — group user+assistant messages into turns keyed by
    # message_id on the assistant side (same dedup strategy as scanner).
    turns_by_msg = {}   # message_id -> turn dict
    turn_order = []     # list of message_ids in first-seen order
    pending_user = None  # last user message text preview

    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = record.get("type")
                if rtype == "user":
                    msg = record.get("message", {})
                    content = msg.get("content") if isinstance(msg, dict) else None
                    pending_user = _preview_from_content(content)
                    continue
                if rtype != "assistant":
                    continue

                msg = record.get("message", {})
                usage = msg.get("usage", {}) or {}
                model = msg.get("model", "")
                message_id = msg.get("id", "")

                inp = usage.get("input_tokens", 0) or 0
                out = usage.get("output_tokens", 0) or 0
                cr  = usage.get("cache_read_input_tokens", 0) or 0
                cc  = usage.get("cache_creation_input_tokens", 0) or 0
                if inp + out + cr + cc == 0:
                    continue

                content = msg.get("content", [])
                preview = _preview_from_content(content)
                tools = _tool_calls_from_content(content)
                timestamp = record.get("timestamp", "")
                day_local, hour_local = to_local_hour(timestamp)
                local_label = f"{day_local} {hour_local:02d}:00 CT" if day_local else timestamp

                turn = {
                    "message_id": message_id or f"anon-{len(turn_order)}",
                    "timestamp": timestamp,
                    "local_time": local_label,
                    "user_preview": pending_user or "",
                    "assistant_preview": preview,
                    "tools": tools,
                    "model": model,
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_read": cr,
                    "cache_creation": cc,
                }
                pending_user = None  # consumed

                key = turn["message_id"]
                if key not in turns_by_msg:
                    turn_order.append(key)
                turns_by_msg[key] = turn  # last write wins (streaming dedup)
    except OSError as e:
        return {"error": f"Could not read JSONL: {e}"}

    turns = [turns_by_msg[k] for k in turn_order]
    if len(turns) > max_turns:
        # Truncate to most recent max_turns, but mark the truncation
        trimmed = True
        turns = turns[-max_turns:]
    else:
        trimmed = False

    def safe_iso_slice(ts):
        return (ts or "")[:16].replace("T", " ")

    return {
        "session_id": row["session_id"],
        "session_id_short": row["session_id"][:8],
        "title": row["title"],
        "project_name": row["project_name"],
        "original_cwd": row["original_cwd"],
        "git_branch": row["git_branch"],
        "model": row["model"],
        "first_timestamp": safe_iso_slice(row["first_timestamp"]),
        "last_timestamp": safe_iso_slice(row["last_timestamp"]),
        "total_input_tokens": row["total_input_tokens"],
        "total_output_tokens": row["total_output_tokens"],
        "total_cache_read": row["total_cache_read"],
        "total_cache_creation": row["total_cache_creation"],
        "turn_count": row["turn_count"],
        "trimmed": trimmed,
        "turns": turns,
    }


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
    """, (
        start.astimezone(ZoneInfo("UTC")).isoformat(),
        end.astimezone(ZoneInfo("UTC")).isoformat(),
    )).fetchall()
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


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

        elif self.path == "/api/data":
            data = get_dashboard_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path.startswith("/api/session/"):
            sid = self.path[len("/api/session/"):]
            data = read_session_turns(sid)
            status = 404 if "error" in data else 200
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/api/subscription":
            data = get_subscription_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/rescan":
            # Full rebuild: delete DB and rescan from scratch
            if DB_PATH.exists():
                DB_PATH.unlink()
            from scanner import scan
            result = scan(verbose=False)
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def serve(host=None, port=None):
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
