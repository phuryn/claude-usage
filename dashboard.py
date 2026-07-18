"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import os
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime

from scanner import VERSION, init_db

DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))

# Which surface is rendering the dashboard: "web" (standalone `cli.py dashboard`)
# or "vscode" (embedded in the extension's sidebar webview). serve() sets this
# from the --surface flag the extension passes. The footer reads it to decide
# what to show — the web build promotes the VS Code extension and offers a
# "check GitHub for a newer release" update link; the embedded build shows just
# the version (VS Code updates the extension itself, and a GitHub-release check
# would misfire there because the Marketplace publish lags the GitHub release).
SURFACE = "web"


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    # The dashboard reads while a background scan may be committing (cmd_dashboard
    # serves first, scans in a background thread; /api/rescan scans in-process too).
    # Wait briefly for write locks instead of raising "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    # Ensure the schema is current before querying. cmd_dashboard binds and serves
    # *before* its background scan runs init_db, so on the first load after an
    # upgrade a pre-existing DB may still be on the old schema — the subagent
    # queries below reference the `agents` table and the `is_subagent`/`agent_id`
    # columns and would raise "no such table: agents" until the scan caught up.
    # init_db is idempotent (CREATE ... IF NOT EXISTS + additive column checks),
    # so this is a cheap no-op once migrated.
    init_db(conn)

    # ── All models (for filter UI) ────────────────────────────────────────────
    # GROUP BY uses the normalised expression too so NULL and '' don't end up
    # as two separate "unknown" rows.
    model_rows = conn.execute("""
        SELECT COALESCE(NULLIF(model, ''), 'unknown') as model
        FROM turns
        GROUP BY COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns
        FROM turns
        GROUP BY day, COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY day, model
    """).fetchall()

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── Hourly per-day per-model (client filters by range + TZ-shifts) ────────
    # Timestamps are ISO8601 UTC (e.g. "2026-04-08T09:30:00Z"); chars 12-13 = hour.
    hourly_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)                  as day,
            CAST(substr(timestamp, 12, 2) AS INTEGER) as hour,
            COALESCE(NULLIF(model, ''), 'unknown')    as model,
            SUM(output_tokens)                        as output,
            COUNT(*)                                  as turns
        FROM turns
        WHERE timestamp IS NOT NULL AND length(timestamp) >= 13
        GROUP BY day, hour, COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY day, hour, model
    """).fetchall()

    hourly_by_model = [{
        "day":    r["day"],
        "hour":   r["hour"] if r["hour"] is not None else 0,
        "model":  r["model"],
        "output": r["output"] or 0,
        "turns":  r["turns"] or 0,
    } for r in hourly_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count,
            git_branch, topic
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()

    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        sessions_all.append({
            # Full id: the table truncates for display, but the CSV export
            # needs the whole thing (an 8-char prefix isn't uniquely useful).
            "session_id":    r["session_id"],
            "project":       r["project_name"] or "unknown",
            "branch":        r["git_branch"] or "",
            "topic":         r["topic"] or "",
            "last":          (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":     (r["last_timestamp"] or "")[:10],
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
        })

    # ── Subagent breakdown by type, by day & model ────────────────────────────
    # JOIN turns to agents (parent tool_result metadata captured by the scanner).
    # acompact-* ids are Claude Code's auto-compaction subagent (no parent
    # dispatch record); anything else without a match is shown as 'unknown'.
    AGENT_TYPE_EXPR = (
        "COALESCE(a.agent_type, "
        "CASE WHEN t.agent_id LIKE 'acompact-%' THEN 'auto-compact' "
        "ELSE 'unknown' END)"
    )

    subagent_daily_rows = conn.execute(f"""
        SELECT
            substr(t.timestamp, 1, 10)               as day,
            {AGENT_TYPE_EXPR}                        as agent_type,
            COALESCE(NULLIF(t.model, ''), 'unknown') as model,
            SUM(t.input_tokens)                      as input,
            SUM(t.output_tokens)                     as output,
            SUM(t.cache_read_tokens)                 as cache_read,
            SUM(t.cache_creation_tokens)             as cache_creation,
            COUNT(DISTINCT t.agent_id)               as dispatches,
            COUNT(*)                                 as turns
        FROM turns t
        LEFT JOIN agents a ON t.agent_id = a.agent_id
        WHERE t.is_subagent = 1
        GROUP BY day, agent_type, model
        ORDER BY day, agent_type
    """).fetchall()

    subagent_by_type = [{
        "day":            r["day"],
        "agent_type":     r["agent_type"],
        "model":          r["model"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "dispatches":     r["dispatches"] or 0,
        "turns":          r["turns"] or 0,
    } for r in subagent_daily_rows]

    # ── Top individual subagent dispatches (one row per agent_id) ─────────────
    top_dispatch_rows = conn.execute(f"""
        SELECT
            t.agent_id                               as agent_id,
            {AGENT_TYPE_EXPR}                        as agent_type,
            COALESCE(NULLIF(t.model, ''), 'unknown') as model,
            MIN(t.timestamp)                         as start_ts,
            SUM(t.input_tokens)                      as input,
            SUM(t.output_tokens)                     as output,
            SUM(t.cache_read_tokens)                 as cache_read,
            SUM(t.cache_creation_tokens)             as cache_creation,
            COUNT(*)                                 as turns,
            a.dispatched_in_session                  as parent_session,
            a.total_duration_ms                      as duration_ms,
            a.tool_use_count                         as tool_uses,
            a.status                                 as status
        FROM turns t
        LEFT JOIN agents a ON t.agent_id = a.agent_id
        WHERE t.is_subagent = 1 AND t.agent_id IS NOT NULL
        GROUP BY t.agent_id
        ORDER BY (SUM(t.input_tokens) + SUM(t.output_tokens)
                  + SUM(t.cache_read_tokens) + SUM(t.cache_creation_tokens)) DESC
    """).fetchall()

    top_dispatches = [{
        "agent_id":       r["agent_id"],
        "agent_type":     r["agent_type"],
        "model":          r["model"],
        "start":          (r["start_ts"] or "")[:16].replace("T", " "),
        "start_date":     (r["start_ts"] or "")[:10],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
        "duration_ms":    r["duration_ms"],
        "tool_uses":      r["tool_uses"],
        "status":         r["status"],
    } for r in top_dispatch_rows]

    conn.close()

    return {
        "all_models":      all_models,
        "daily_by_model":  daily_by_model,
        "hourly_by_model": hourly_by_model,
        "sessions_all":    sessions_all,
        "subagent_by_type": subagent_by_type,
        "top_dispatches":  top_dispatches,
        "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>window.APP_CONFIG = __APP_CONFIG_JSON__;</script>
<style>
  :root {
    --bg: #161617;      /* page base */
    --card: #1E1F20;    /* raised one step above the page */
    --border: #2C2D2E;
    --text: #BFBFBF;
    --muted: #4F4F50;
    --accent: #d97757;
    --blue: #48A0C7;
    --green: #74C991;
    --red: #C74E39;
    --raised: #2E2F31;  /* hover / raised surfaces — top of the elevation ladder */
    --selected: #262626;  /* selected chips / tabs (neutral, not accent) */
    --jump-h: 45px;  /* sticky jump-bar height; JS keeps it in sync for scroll offsets */
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  /* VS Code-style scrollbars. The dashboard renders inside a webview iframe,
     which doesn't inherit VS Code's --vscode-* theme variables, so we set the
     scrollbar here: no arrows, grey thumb (#28292B, #8B8B8D on hover) over a
     #121314 track, in a 21px gutter. Also fits the dark UI standalone. */
  * { scrollbar-width: auto; scrollbar-color: #28292B #121314; }
  ::-webkit-scrollbar { width: 21px; height: 21px; }
  ::-webkit-scrollbar-track { background: #121314; }
  ::-webkit-scrollbar-thumb { background-color: #28292B; border: 3px solid transparent; background-clip: padding-box; }
  ::-webkit-scrollbar-thumb:hover { background-color: #8B8B8D; }
  ::-webkit-scrollbar-thumb:active { background-color: #8B8B8D; }
  ::-webkit-scrollbar-corner { background: #121314; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--text); }
  header .header-title { display: flex; align-items: center; gap: 10px; }
  /* The icon is a monochrome silhouette (white shape on transparent). We paint
     it with the title color via a CSS mask + background-color, so it matches
     `header h1` — the lightest text color. */
  header .header-icon {
    width: 26px; height: 26px; flex-shrink: 0; display: block;
    background-color: var(--text);
    -webkit-mask: url("icon.svg") no-repeat center / contain;
    mask: url("icon.svg") no-repeat center / contain;
  }
  header .meta { color: var(--muted); font-size: 12px; text-align: right; line-height: 1.5; margin-right: 20px; }
  #rescan-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; margin-top: 4px; }
  #rescan-btn:hover { color: var(--text); border-color: var(--accent); }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  #filter-bar { background: var(--card); border-bottom: 1px solid var(--border); padding: 10px 24px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .filter-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
  /* Model multi-select: a compact trigger in the bar that opens a grouped panel. */
  .model-select { position: relative; flex-shrink: 0; }
  .model-trigger { display: flex; align-items: center; gap: 8px; min-width: 170px; max-width: 320px; padding: 5px 10px; background: var(--card); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-size: 12px; cursor: pointer; transition: border-color 0.15s; }
  .model-trigger:hover, .model-trigger.open { border-color: var(--accent); }
  #model-trigger-label { flex: 1; text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .model-caret { color: var(--muted); font-size: 10px; flex-shrink: 0; transition: transform 0.15s; }
  .model-trigger.open .model-caret { transform: rotate(180deg); }
  .model-panel { position: absolute; top: calc(100% + 6px); left: 0; z-index: 50; min-width: 250px; max-width: 340px; max-height: 360px; overflow-y: auto; background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.35); }
  .model-panel[hidden] { display: none; }
  .model-panel-actions { display: flex; gap: 6px; padding-bottom: 8px; margin-bottom: 4px; border-bottom: 1px solid var(--border); }
  .model-group-label { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); padding: 8px 8px 4px; }
  .model-cb-label { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; font-size: 12px; color: var(--muted); transition: background 0.12s, color 0.12s; user-select: none; }
  .model-cb-label:hover { background: var(--raised); color: var(--text); }
  .model-cb-label.checked { color: var(--text); }
  .model-cb-label input { display: none; }
  .model-cb-box { width: 15px; height: 15px; flex-shrink: 0; border-radius: 4px; border: 1px solid var(--border); display: flex; align-items: center; justify-content: center; font-size: 10px; line-height: 1; color: transparent; transition: background 0.12s, border-color 0.12s; }
  .model-cb-label.checked .model-cb-box { background: var(--accent); border-color: var(--accent); color: #fff; }
  .model-cb-text { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .filter-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; white-space: nowrap; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  /* Date range — a compact dropdown. The old segmented button row (8 buttons)
     wrapped badly in the narrow VS Code panel; a single select stays put. Styled
     to match the model trigger. */
  .range-select { position: relative; flex-shrink: 0; }
  .range-select select { appearance: none; -webkit-appearance: none; min-width: 150px; padding: 5px 30px 5px 10px; background: var(--card); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-size: 12px; cursor: pointer; transition: border-color 0.15s; }
  .range-select select:hover, .range-select select:focus { border-color: var(--accent); outline: none; }
  .range-select::after { content: "\25BE"; position: absolute; right: 11px; top: 50%; transform: translateY(-50%); color: var(--muted); font-size: 10px; pointer-events: none; }
  .range-select option { background: var(--card); color: var(--text); }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  /* min-width:0 lets the grid column shrink below the canvas's intrinsic
     pixel width; without it, narrowing the window can't narrow the container,
     so Chart.js's ResizeObserver never fires until a data refresh rebuilds the
     canvas. (Expanding already works — 1fr columns grow freely.) */
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; min-width: 0; }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 240px; }
  .chart-wrap.tall { height: 300px; }
  .chart-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
  .chart-header h2 { margin-bottom: 0; }
  .chart-header-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .chart-day-count { font-size: 11px; color: var(--muted); }
  .tz-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .tz-btn { padding: 3px 10px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 11px; cursor: pointer; transition: background 0.15s, color 0.15s; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .tz-btn:last-child { border-right: none; }
  .tz-btn:hover { background: var(--raised); color: var(--text); }
  .tz-btn.active { background: var(--selected); color: var(--text); }
  .peak-legend { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
  .peak-swatch { width: 10px; height: 10px; background: var(--red); border-radius: 2px; display: inline-block; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  .sort-icon { font-size: 9px; opacity: 0.8; }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--raised); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(72,160,199,0.15); color: var(--blue); }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .topic-cell { box-sizing: border-box; min-width: 160px; max-width: 260px; overflow-wrap: anywhere; font-size: 12px; color: var(--text); }
  .untitled { color: var(--muted); font-style: italic; }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .section-header .section-title { margin-bottom: 0; }
  .export-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 3px 10px; border-radius: 5px; cursor: pointer; font-size: 11px; }
  .export-btn:hover { color: var(--text); border-color: var(--accent); }
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; overflow-x: auto; }
  .table-foot { display: flex; justify-content: flex-end; align-items: center; gap: 12px; margin-top: 12px; }
  .table-foot:empty { margin-top: 0; }
  .show-more-btn { background: transparent; border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  .show-more-btn:hover { color: var(--text); border-color: var(--accent); }
  .show-more-link { color: var(--blue); text-decoration: none; font-size: 12px; cursor: pointer; }
  .show-more-link:hover { text-decoration: underline; }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 8px; }
  .footer-content { max-width: 1400px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }
  .footer-content a.update-link { color: var(--accent); font-weight: 600; }

  /* Jump bar — a sticky table-of-contents for a long report. Styled as a sibling
     of the filter bar (same card surface + bottom border) so it reads as part of
     the same control strip. It pins to the viewport top once the header/filter
     scroll away. z-index sits below the model panel (50) so the dropdown still
     overlays it. */
  /* Sticky table-of-contents for the long report: three compact entries —
     Overview, plus Graphs and Tables menus that reveal their sections on hover
     (or keyboard focus). Stays small so it never crowds the narrow VS Code panel. */
  #jump-bar { position: sticky; top: 0; z-index: 20; background: var(--card); border-bottom: 1px solid var(--border); padding: 7px 24px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; box-shadow: 0 2px 8px rgba(0,0,0,0.18); }
  .jump-menu { position: relative; }
  .jump-trigger { display: inline-flex; align-items: center; gap: 6px; padding: 3px 11px; border-radius: 6px; border: 1px solid transparent; background: transparent; color: var(--muted); font-size: 12px; cursor: pointer; transition: background 0.12s, color 0.12s, border-color 0.12s; }
  .jump-trigger svg { display: block; }
  .jump-caret { font-size: 9px; }
  .jump-trigger:hover, .jump-menu:focus-within .jump-trigger { color: var(--text); background: var(--raised); }
  .jump-trigger.active { color: var(--text); border-color: var(--border); }
  .jump-panel { position: absolute; top: calc(100% + 5px); left: 0; z-index: 50; min-width: 160px; display: none; flex-direction: column; gap: 2px; padding: 6px; background: var(--card); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.35); }
  /* Invisible bridge over the 5px gap so the menu doesn't close as the pointer
     travels from the trigger down to the panel. */
  .jump-panel::before { content: ""; position: absolute; left: 0; right: 0; top: -8px; height: 8px; }
  .jump-menu-end .jump-panel { left: auto; right: 0; }
  .jump-menu:hover .jump-panel, .jump-menu:focus-within .jump-panel { display: flex; }
  .jump-link { padding: 3px 11px; border-radius: 6px; border: 1px solid transparent; background: transparent; color: var(--muted); font-size: 12px; cursor: pointer; white-space: nowrap; transition: background 0.12s, color 0.12s, border-color 0.12s; }
  .jump-panel .jump-link { display: block; width: 100%; text-align: left; padding: 5px 10px; }
  .jump-link:hover { color: var(--text); background: var(--raised); }
  .jump-link.active { color: var(--text); background: var(--selected); border-color: var(--border); font-weight: 600; }
  /* Inline info affordance (e.g. the dispatches table) — native title tooltip. */
  .info-icon { display: inline-flex; align-items: center; vertical-align: middle; margin-left: 3px; color: var(--muted); cursor: help; }
  .info-icon svg { display: block; }
  .info-icon:hover { color: var(--text); }
  /* Anchored sections clear the sticky bar when jumped/collapsed to. */
  .stats-row, .chart-card, .table-card { scroll-margin-top: calc(var(--jump-h) + 14px); }

  /* Collapsible cards — a full section fold, independent of in-table Show
     more/less (which only pages rows). Collapsing hides the card body and its
     header controls, leaving just the caret + title. State persists per card in
     localStorage. */
  .card-caret { display: inline-block; width: 0.9em; margin-right: 7px; font-size: 14px; line-height: 1; color: inherit; transform: rotate(90deg); transition: transform 0.15s; }
  .collapsed .card-caret { transform: rotate(0deg); }
  .chart-card > h2, .chart-header > h2, .section-title { cursor: pointer; user-select: none; }
  .chart-card > h2:hover, .chart-header > h2:hover, .section-title:hover { color: var(--text); }
  .jump-link:focus-visible, .jump-trigger:focus-visible, .info-icon:focus-visible, .chart-card > h2:focus-visible, .chart-header > h2:focus-visible, .section-title:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .chart-card.collapsed > h2, .chart-card.collapsed > .chart-header { margin-bottom: 0; }
  .table-card.collapsed > .section-title, .table-card.collapsed > .section-header { margin-bottom: 0; }
  .chart-card.collapsed > *:not(h2):not(.chart-header),
  .chart-card.collapsed .chart-header > *:not(h2),
  .table-card.collapsed > *:not(.section-title):not(.section-header),
  .table-card.collapsed .section-header > *:not(.section-title) { display: none; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } }
</style>
</head>
<body>
<header>
  <div class="header-title">
    <span class="header-icon" role="img" aria-label="Claude Usage"></span>
    <h1 data-i18n="header_title">Claude Code Usage</h1>
  </div>
  <div class="meta" id="meta" data-i18n="meta_loading">Loading...</div>
  <button id="rescan-btn" onclick="triggerRescan()" data-i18n="rescan" data-i18n-title="rescan_title" title="Scan for new usage since the last update. Adds new turns without affecting existing history.">&#x21bb; Rescan</button>
</header>

<div id="filter-bar">
  <div class="filter-label" data-i18n="filter_models">Models</div>
  <div class="model-select" id="model-select">
    <button class="model-trigger" id="model-trigger" aria-haspopup="true" aria-expanded="false" onclick="toggleModelPanel(event)">
      <span id="model-trigger-label">All models</span>
      <span class="model-caret">&#9662;</span>
    </button>
    <div class="model-panel" id="model-panel" hidden>
      <div class="model-panel-actions">
        <button class="filter-btn" onclick="selectAllModels()" data-i18n="btn_all">All</button>
        <button class="filter-btn" onclick="clearAllModels()" data-i18n="btn_none">None</button>
      </div>
      <div id="model-checkboxes"></div>
    </div>
  </div>
  <div class="filter-sep"></div>
  <div class="filter-label" data-i18n="filter_range">Range</div>
  <div class="range-select">
    <select id="range-select" aria-label="Date range" data-i18n-aria="range_aria" onchange="setRange(this.value)">
      <option value="today">Today</option>
      <option value="week">This Week</option>
      <option value="month">This Month</option>
      <option value="prev-month">Previous Month</option>
      <option value="7d">Last 7 Days</option>
      <option value="30d">Last 30 Days</option>
      <option value="90d">Last 90 Days</option>
      <option value="all">All Time</option>
    </select>
  </div>
  <div class="filter-sep"></div>
  <div class="filter-label" data-i18n="filter_language">Language</div>
  <div class="range-select">
    <select id="lang-select" aria-label="Language" data-i18n-aria="filter_language" onchange="setLang(this.value)">
      <option value="en">English</option>
      <option value="zh">&#20013;&#25991;</option>
      <option value="es">Espa&#241;ol</option>
      <option value="fr">Fran&#231;ais</option>
      <option value="de">Deutsch</option>
      <option value="ja">&#26085;&#26412;&#35486;</option>
      <option value="ko">&#54620;&#44397;&#50612;</option>
      <option value="pt">Portugu&#234;s</option>
      <option value="ru">&#1056;&#1091;&#1089;&#1089;&#1082;&#1080;&#1081;</option>
    </select>
  </div>
</div>

<nav id="jump-bar" aria-label="Jump to section" data-i18n-aria="jump_aria">
  <button class="jump-link" data-target="stats-row" data-i18n="jump_overview">Overview</button>
  <div class="jump-menu">
    <button type="button" class="jump-trigger" aria-haspopup="true" aria-expanded="false">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M8 17v-4"/><path d="M13 17V8"/><path d="M18 17v-7"/></svg>
      <span data-i18n="jump_graphs">Graphs</span> <span class="jump-caret">&#9662;</span>
    </button>
    <div class="jump-panel">
      <button class="jump-link" data-target="sec-daily" data-i18n="jump_daily">Daily</button>
      <button class="jump-link" data-target="sec-hourly" data-i18n="jump_distribution">Distribution</button>
      <button class="jump-link" data-target="sec-models" data-i18n="jump_bymodel">By Model</button>
      <button class="jump-link" data-target="sec-projects" data-i18n="jump_topprojects">Top Projects</button>
      <button class="jump-link" data-target="sec-subagents" data-i18n="jump_subagents">Subagents</button>
    </div>
  </div>
  <div class="jump-menu jump-menu-end">
    <button type="button" class="jump-trigger" aria-haspopup="true" aria-expanded="false">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v18"/><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/></svg>
      <span data-i18n="jump_tables">Tables</span> <span class="jump-caret">&#9662;</span>
    </button>
    <div class="jump-panel">
      <button class="jump-link" data-target="sec-cost-model" data-i18n="jump_costmodel">Cost by Model</button>
      <button class="jump-link" data-target="sec-dispatches" data-i18n="jump_dispatches">Dispatches</button>
      <button class="jump-link" data-target="sec-sessions" data-i18n="jump_sessions">Sessions</button>
      <button class="jump-link" data-target="sec-cost-project" data-i18n="jump_costproject">Cost by Project</button>
      <button class="jump-link" data-target="sec-cost-branch" data-i18n="jump_costbranch">Cost by Project &amp; Branch</button>
    </div>
  </div>
</nav>

<div class="container">
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide" id="sec-daily" data-card="daily">
      <h2><span class="card-caret">&#9656;</span><span id="daily-chart-title">Daily Token Usage</span></h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card wide" id="sec-hourly" data-card="hourly">
      <div class="chart-header">
        <h2><span class="card-caret">&#9656;</span><span id="hourly-chart-title">Average Hourly Distribution</span></h2>
        <div class="chart-header-right">
          <span class="peak-legend" data-i18n-title="peak_title" title="Mon–Fri 05:00–11:00 PT — Anthropic peak-hour throttling window"><span class="peak-swatch"></span><span data-i18n="peak_legend">Peak hours (PT)</span></span>
          <span class="chart-day-count" id="hourly-day-count"></span>
          <div class="tz-group">
            <button class="tz-btn" data-tz="local" onclick="setHourlyTZ('local')" data-i18n="tz_local">Local</button>
            <button class="tz-btn" data-tz="utc"   onclick="setHourlyTZ('utc')" data-i18n="tz_utc">UTC</button>
          </div>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="chart-hourly"></canvas></div>
    </div>
    <div class="chart-card" id="sec-models" data-card="model-chart">
      <h2><span class="card-caret">&#9656;</span><span data-i18n="chart_bymodel">By Model</span></h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card" id="sec-projects" data-card="project-chart">
      <h2><span class="card-caret">&#9656;</span><span data-i18n="chart_topprojects">Top Projects by Tokens</span></h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
    <div class="chart-card wide" id="sec-subagents" data-card="subagent-chart">
      <h2><span class="card-caret">&#9656;</span><span id="subagent-chart-title">Subagent Tokens by Type</span></h2>
      <div class="chart-wrap"><canvas id="chart-subagent"></canvas></div>
    </div>
  </div>
  <div class="table-card" id="sec-cost-model" data-card="cost-by-model">
    <div class="section-title"><span class="card-caret">&#9656;</span><span data-i18n="sec_costmodel">Cost by Model</span></div>
    <table>
      <thead><tr>
        <th data-i18n="th_model">Model</th>
        <th class="sortable" onclick="setModelSort('turns')"><span data-i18n="th_turns">Turns</span> <span class="sort-icon" id="msort-turns"></span></th>
        <th class="sortable" onclick="setModelSort('input')"><span data-i18n="th_input">Input</span> <span class="sort-icon" id="msort-input"></span></th>
        <th class="sortable" onclick="setModelSort('output')"><span data-i18n="th_output">Output</span> <span class="sort-icon" id="msort-output"></span></th>
        <th class="sortable" onclick="setModelSort('cache_read')"><span data-i18n="th_cache_read">Cache Read</span> <span class="sort-icon" id="msort-cache_read"></span></th>
        <th class="sortable" onclick="setModelSort('cache_creation')"><span data-i18n="th_cache_creation">Cache Creation</span> <span class="sort-icon" id="msort-cache_creation"></span></th>
        <th class="sortable" onclick="setModelSort('cost')"><span data-i18n="th_est_cost">Est. Cost</span> <span class="sort-icon" id="msort-cost"></span></th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
    <div class="table-foot" id="model-cost-foot"></div>
  </div>
  <div class="table-card" id="sec-dispatches" data-card="dispatches">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span><span data-i18n="sec_dispatches">Top Subagent Dispatches</span> <span class="info-icon" tabindex="0" role="img" aria-label="About this table" data-i18n-title="sec_dispatches_info" title="Ranked by total tokens. &quot;unknown&quot; means the parent dispatch record wasn't found."><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg></span></div><button class="export-btn" onclick="exportDispatchesCSV()" data-i18n-title="csv_title_dispatches" title="Export all filtered subagent dispatches to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th data-i18n="th_type">Type</th><th data-i18n="th_started">Started</th><th data-i18n="th_model">Model</th><th data-i18n="th_turns">Turns</th><th data-i18n="th_tool_uses">Tool Uses</th>
        <th data-i18n="th_duration">Duration</th><th data-i18n="th_input">Input</th><th data-i18n="th_output">Output</th><th data-i18n="th_cache_read">Cache Read</th><th data-i18n="th_tokens">Tokens</th><th data-i18n="th_est_cost">Est. Cost</th>
      </tr></thead>
      <tbody id="dispatches-body"></tbody>
    </table>
    <div class="table-foot" id="dispatches-foot"></div>
  </div>
  <div class="table-card" id="sec-sessions" data-card="sessions">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span><span data-i18n="sec_sessions">Recent Sessions</span></div><button class="export-btn" onclick="exportSessionsCSV()" data-i18n-title="csv_title_sessions" title="Export all filtered sessions to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th data-i18n="th_session">Session</th>
        <th data-i18n="th_project">Project</th>
        <th data-i18n="th_title">Title</th>
        <th class="sortable" onclick="setSessionSort('last')"><span data-i18n="th_last_active">Last Active</span> <span class="sort-icon" id="sort-icon-last"></span></th>
        <th class="sortable" onclick="setSessionSort('duration_min')"><span data-i18n="th_duration">Duration</span> <span class="sort-icon" id="sort-icon-duration_min"></span></th>
        <th data-i18n="th_model">Model</th>
        <th class="sortable" onclick="setSessionSort('turns')"><span data-i18n="th_turns">Turns</span> <span class="sort-icon" id="sort-icon-turns"></span></th>
        <th class="sortable" onclick="setSessionSort('input')"><span data-i18n="th_input">Input</span> <span class="sort-icon" id="sort-icon-input"></span></th>
        <th class="sortable" onclick="setSessionSort('output')"><span data-i18n="th_output">Output</span> <span class="sort-icon" id="sort-icon-output"></span></th>
        <th class="sortable" onclick="setSessionSort('cost')"><span data-i18n="th_est_cost">Est. Cost</span> <span class="sort-icon" id="sort-icon-cost"></span></th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
    <div class="table-foot" id="sessions-foot"></div>
  </div>
  <div class="table-card" id="sec-cost-project" data-card="cost-by-project">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span><span data-i18n="sec_costproject">Cost by Project</span></div><button class="export-btn" onclick="exportProjectsCSV()" data-i18n-title="csv_title_projects" title="Export all projects to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th data-i18n="th_project">Project</th>
        <th class="sortable" onclick="setProjectSort('sessions')"><span data-i18n="th_sessions">Sessions</span> <span class="sort-icon" id="psort-sessions"></span></th>
        <th class="sortable" onclick="setProjectSort('turns')"><span data-i18n="th_turns">Turns</span> <span class="sort-icon" id="psort-turns"></span></th>
        <th class="sortable" onclick="setProjectSort('input')"><span data-i18n="th_input">Input</span> <span class="sort-icon" id="psort-input"></span></th>
        <th class="sortable" onclick="setProjectSort('output')"><span data-i18n="th_output">Output</span> <span class="sort-icon" id="psort-output"></span></th>
        <th class="sortable" onclick="setProjectSort('cost')"><span data-i18n="th_est_cost">Est. Cost</span> <span class="sort-icon" id="psort-cost"></span></th>
      </tr></thead>
      <tbody id="project-cost-body"></tbody>
    </table>
    <div class="table-foot" id="project-cost-foot"></div>
  </div>
  <div class="table-card" id="sec-cost-branch" data-card="cost-by-branch">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span><span data-i18n="sec_costbranch">Cost by Project &amp; Branch</span></div><button class="export-btn" onclick="exportProjectBranchCSV()" data-i18n-title="csv_title_branch" title="Export project+branch breakdown to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th data-i18n="th_project">Project</th>
        <th data-i18n="th_branch">Branch</th>
        <th class="sortable" onclick="setProjectBranchSort('sessions')"><span data-i18n="th_sessions">Sessions</span> <span class="sort-icon" id="pbsort-sessions"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('turns')"><span data-i18n="th_turns">Turns</span> <span class="sort-icon" id="pbsort-turns"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('input')"><span data-i18n="th_input">Input</span> <span class="sort-icon" id="pbsort-input"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('output')"><span data-i18n="th_output">Output</span> <span class="sort-icon" id="pbsort-output"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('cost')"><span data-i18n="th_est_cost">Est. Cost</span> <span class="sort-icon" id="pbsort-cost"></span></th>
      </tr></thead>
      <tbody id="project-branch-cost-body"></tbody>
    </table>
    <div class="table-foot" id="project-branch-cost-foot"></div>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p data-i18n-html="footer_pricing">Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of June 2026. Only models containing <em>fable</em>, <em>mythos</em>, <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> in the name are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.</p>
    <p>
      GitHub: <a href="https://github.com/phuryn/claude-usage" target="_blank">https://github.com/phuryn/claude-usage</a>
      &nbsp;&middot;&nbsp;
      <span data-i18n="footer_created_by">Created by:</span> <a href="https://www.productcompass.pm" target="_blank">The Product Compass Newsletter</a>
      &nbsp;&middot;&nbsp;
      <span data-i18n="footer_license">License: MIT</span>
    </p>
    <p id="footer-meta"></p>
  </div>
</footer>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

// ── i18n ───────────────────────────────────────────────────────────────────
// Language is chosen client-side (persisted in localStorage) and applied to the
// static markup via data-i18n* attributes plus t() calls threaded through the
// dynamic renderers. English is always the literal fallback in the template, so
// the page still reads correctly if a key is missing or JS fails to run.
const I18N = {
  en: {
    app_title: 'Claude Code Usage Dashboard',
    header_title: 'Claude Code Usage',
    meta_loading: 'Loading...',
    meta_updated: 'Updated: {t}',
    meta_autorefresh: 'Auto-refresh in 30s',
    meta_retrying: '{e} — retrying…',
    rescan: '↻ Rescan',
    rescan_title: 'Scan for new usage since the last update. Adds new turns without affecting existing history.',
    rescan_scanning: '↻ Scanning...',
    rescan_result: '↻ Rescan ({new} new, {updated} updated)',
    rescan_error: '↻ Rescan (error)',
    filter_models: 'Models',
    filter_range: 'Range',
    filter_language: 'Language',
    models_all: 'All models',
    models_none_sel: 'No models',
    models_all_anthropic: 'All Anthropic',
    models_all_anthropic_plus: 'All Anthropic +{n}',
    btn_all: 'All',
    btn_none: 'None',
    range_today: 'Today', range_week: 'This Week', range_month: 'This Month',
    'range_prev-month': 'Previous Month', range_7d: 'Last 7 Days', range_30d: 'Last 30 Days',
    range_90d: 'Last 90 Days', range_all: 'All Time', range_aria: 'Date range',
    jump_aria: 'Jump to section', jump_overview: 'Overview', jump_graphs: 'Graphs',
    jump_daily: 'Daily', jump_distribution: 'Distribution', jump_bymodel: 'By Model',
    jump_topprojects: 'Top Projects', jump_subagents: 'Subagents', jump_tables: 'Tables',
    jump_costmodel: 'Cost by Model', jump_dispatches: 'Dispatches', jump_sessions: 'Sessions',
    jump_costproject: 'Cost by Project', jump_costbranch: 'Cost by Project & Branch',
    chart_daily: 'Daily Token Usage', chart_hourly: 'Average Hourly Distribution',
    chart_subagent: 'Subagent Tokens by Type', chart_bymodel: 'By Model',
    chart_topprojects: 'Top Projects by Tokens',
    peak_legend: 'Peak hours (PT)',
    peak_title: 'Mon–Fri 05:00–11:00 PT — Anthropic peak-hour throttling window',
    tz_local: 'Local', tz_utc: 'UTC',
    axis_avg_turns: 'Avg turns / hour', axis_avg_output: 'Avg output tokens / hour',
    axis_cache: 'Cache', axis_io: 'Input / Output',
    series_input: 'Input', series_output: 'Output', series_cache_read: 'Cache Read',
    series_cache_creation: 'Cache Creation', series_est_cost: 'Est. Cost',
    tt_est_cost: ' Est. Cost: {c}', tt_tokens: ' {label}: {v} tokens',
    tt_total: ' Total: {v} · {turns} turns', tt_series: ' {label}: {v}',
    tt_peak_suffix: ' · Peak — Anthropic US hours',
    tt_avg_turns: ' Avg turns: {v}', tt_avg_output: ' Avg output: {v}',
    hourly_days: '{n} days averaged · {tz}', hourly_day: '{n} day averaged · {tz}',
    hourly_nodata: 'No data · {tz}',
    th_model: 'Model', th_turns: 'Turns', th_input: 'Input', th_output: 'Output',
    th_cache_read: 'Cache Read', th_cache_creation: 'Cache Creation', th_est_cost: 'Est. Cost',
    th_type: 'Type', th_started: 'Started', th_tool_uses: 'Tool Uses', th_duration: 'Duration',
    th_tokens: 'Tokens', th_session: 'Session', th_project: 'Project', th_title: 'Title',
    th_last_active: 'Last Active', th_sessions: 'Sessions', th_branch: 'Branch',
    sec_costmodel: 'Cost by Model', sec_dispatches: 'Top Subagent Dispatches',
    sec_dispatches_info: 'Ranked by total tokens. "unknown" means the parent dispatch record wasn\'t found.',
    sec_sessions: 'Recent Sessions', sec_costproject: 'Cost by Project',
    sec_costbranch: 'Cost by Project & Branch',
    csv_title_dispatches: 'Export all filtered subagent dispatches to CSV',
    csv_title_sessions: 'Export all filtered sessions to CSV',
    csv_title_projects: 'Export all projects to CSV',
    csv_title_branch: 'Export project+branch breakdown to CSV',
    stat_sessions: 'Sessions', stat_turns: 'Turns', stat_input: 'Input Tokens',
    stat_output: 'Output Tokens', stat_subagent: 'Subagent Tokens', stat_cache_read: 'Cache Read',
    stat_cache_creation: 'Cache Creation', stat_est_cost: 'Est. Cost',
    sub_included: 'included in totals', sub_from_cache: 'from prompt cache',
    sub_writes_cache: 'writes to prompt cache', sub_pricing: 'API pricing, June 2026',
    show_less: 'Show less ▴', show_more: 'Show more ▾',
    csv_download_all: 'Download CSV to see all ({n})',
    no_dispatches: 'No subagent dispatches in selected range.',
    untitled: 'Untitled', duration_suffix: 'm',
    collapse_title: 'Collapse / expand section',
    footer_pricing: 'Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of June 2026. Only models containing <em>fable</em>, <em>mythos</em>, <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> in the name are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.',
    footer_created_by: 'Created by:', footer_license: 'License: MIT',
    footer_version: 'Version', footer_get_ext: 'Get the VS Code extension',
    footer_update: 'Update to v{v}',
  },
  zh: {
    app_title: 'Claude Code 用量看板',
    header_title: 'Claude Code 用量',
    meta_loading: '加载中…',
    meta_updated: '更新时间：{t}',
    meta_autorefresh: '30 秒后自动刷新',
    meta_retrying: '{e} — 重试中…',
    rescan: '↻ 重新扫描',
    rescan_title: '扫描自上次更新以来的新用量。仅新增记录，不影响已有历史。',
    rescan_scanning: '↻ 扫描中…',
    rescan_result: '↻ 重新扫描（新增 {new}，更新 {updated}）',
    rescan_error: '↻ 重新扫描（出错）',
    filter_models: '模型',
    filter_range: '时间范围',
    filter_language: '语言',
    models_all: '全部模型',
    models_none_sel: '未选模型',
    models_all_anthropic: '全部 Anthropic',
    models_all_anthropic_plus: '全部 Anthropic +{n}',
    btn_all: '全选',
    btn_none: '清空',
    range_today: '今天', range_week: '本周', range_month: '本月',
    'range_prev-month': '上月', range_7d: '近 7 天', range_30d: '近 30 天',
    range_90d: '近 90 天', range_all: '全部时间', range_aria: '时间范围',
    jump_aria: '跳转到板块', jump_overview: '概览', jump_graphs: '图表',
    jump_daily: '每日', jump_distribution: '分布', jump_bymodel: '按模型',
    jump_topprojects: '热门项目', jump_subagents: '子代理', jump_tables: '表格',
    jump_costmodel: '按模型费用', jump_dispatches: '调度', jump_sessions: '会话',
    jump_costproject: '按项目费用', jump_costbranch: '按项目和分支费用',
    chart_daily: '每日 Token 用量', chart_hourly: '平均每小时分布',
    chart_subagent: '子代理 Token（按类型）', chart_bymodel: '按模型',
    chart_topprojects: 'Token 最多的项目',
    peak_legend: '高峰时段（PT）',
    peak_title: '周一至周五 05:00–11:00 PT — Anthropic 高峰限流时段',
    tz_local: '本地', tz_utc: 'UTC',
    axis_avg_turns: '平均轮次 / 小时', axis_avg_output: '平均输出 Token / 小时',
    axis_cache: '缓存', axis_io: '输入 / 输出',
    series_input: '输入', series_output: '输出', series_cache_read: '缓存读取',
    series_cache_creation: '缓存写入', series_est_cost: '预估费用',
    tt_est_cost: ' 预估费用：{c}', tt_tokens: ' {label}：{v} tokens',
    tt_total: ' 合计：{v} · {turns} 轮', tt_series: ' {label}：{v}',
    tt_peak_suffix: ' · 高峰 — Anthropic 美区时段',
    tt_avg_turns: ' 平均轮次：{v}', tt_avg_output: ' 平均输出：{v}',
    hourly_days: '{n} 天平均 · {tz}', hourly_day: '{n} 天平均 · {tz}',
    hourly_nodata: '无数据 · {tz}',
    th_model: '模型', th_turns: '轮次', th_input: '输入', th_output: '输出',
    th_cache_read: '缓存读取', th_cache_creation: '缓存写入', th_est_cost: '预估费用',
    th_type: '类型', th_started: '开始时间', th_tool_uses: '工具调用', th_duration: '时长',
    th_tokens: 'Token', th_session: '会话', th_project: '项目', th_title: '标题',
    th_last_active: '最近活跃', th_sessions: '会话数', th_branch: '分支',
    sec_costmodel: '按模型费用', sec_dispatches: '子代理调度排行',
    sec_dispatches_info: '按总 Token 排序。“unknown”表示未找到父级调度记录。',
    sec_sessions: '最近会话', sec_costproject: '按项目费用',
    sec_costbranch: '按项目和分支费用',
    csv_title_dispatches: '导出所有已筛选的子代理调度为 CSV',
    csv_title_sessions: '导出所有已筛选的会话为 CSV',
    csv_title_projects: '导出所有项目为 CSV',
    csv_title_branch: '导出项目+分支明细为 CSV',
    stat_sessions: '会话数', stat_turns: '轮次', stat_input: '输入 Token',
    stat_output: '输出 Token', stat_subagent: '子代理 Token', stat_cache_read: '缓存读取',
    stat_cache_creation: '缓存写入', stat_est_cost: '预估费用',
    sub_included: '已计入总计', sub_from_cache: '来自提示缓存',
    sub_writes_cache: '写入提示缓存', sub_pricing: 'API 定价，2026 年 6 月',
    show_less: '收起 ▴', show_more: '展开 ▾',
    csv_download_all: '下载 CSV 查看全部（{n}）',
    no_dispatches: '所选范围内没有子代理调度。',
    untitled: '未命名', duration_suffix: ' 分',
    collapse_title: '折叠 / 展开板块',
    footer_pricing: '费用估算基于 Anthropic API 定价（<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>），截至 2026 年 6 月。仅当模型名称包含 <em>fable</em>、<em>mythos</em>、<em>opus</em>、<em>sonnet</em> 或 <em>haiku</em> 时才计入费用计算。Max/Pro 订阅用户的实际费用与 API 定价不同。',
    footer_created_by: '创建者：', footer_license: '许可证：MIT',
    footer_version: '版本', footer_get_ext: '获取 VS Code 扩展',
    footer_update: '更新到 v{v}',
  },
  es: {
    app_title: 'Panel de uso de Claude Code',
    header_title: 'Uso de Claude Code',
    meta_loading: 'Cargando...',
    meta_updated: 'Actualizado: {t}',
    meta_autorefresh: 'Actualización automática en 30 s',
    meta_retrying: '{e} — reintentando…',
    rescan: '↻ Reescanear',
    rescan_title: 'Buscar uso nuevo desde la última actualización. Añade turnos nuevos sin afectar el historial existente.',
    rescan_scanning: '↻ Escaneando...',
    rescan_result: '↻ Reescanear ({new} nuevos, {updated} actualizados)',
    rescan_error: '↻ Reescanear (error)',
    filter_models: 'Modelos',
    filter_range: 'Rango',
    filter_language: 'Idioma',
    models_all: 'Todos los modelos',
    models_none_sel: 'Ningún modelo',
    models_all_anthropic: 'Todo Anthropic',
    models_all_anthropic_plus: 'Todo Anthropic +{n}',
    btn_all: 'Todos',
    btn_none: 'Ninguno',
    range_today: 'Hoy', range_week: 'Esta semana', range_month: 'Este mes',
    'range_prev-month': 'Mes anterior', range_7d: 'Últimos 7 días', range_30d: 'Últimos 30 días',
    range_90d: 'Últimos 90 días', range_all: 'Todo el tiempo', range_aria: 'Rango de fechas',
    jump_aria: 'Ir a la sección', jump_overview: 'Resumen', jump_graphs: 'Gráficos',
    jump_daily: 'Diario', jump_distribution: 'Distribución', jump_bymodel: 'Por modelo',
    jump_topprojects: 'Proyectos', jump_subagents: 'Subagentes', jump_tables: 'Tablas',
    jump_costmodel: 'Costo por modelo', jump_dispatches: 'Despachos', jump_sessions: 'Sesiones',
    jump_costproject: 'Costo por proyecto', jump_costbranch: 'Costo por proyecto y rama',
    chart_daily: 'Uso diario de tokens', chart_hourly: 'Distribución horaria media',
    chart_subagent: 'Tokens de subagentes por tipo', chart_bymodel: 'Por modelo',
    chart_topprojects: 'Proyectos con más tokens',
    peak_legend: 'Horas pico (PT)',
    peak_title: 'Lun–Vie 05:00–11:00 PT — ventana de limitación de horas pico de Anthropic',
    tz_local: 'Local', tz_utc: 'UTC',
    axis_avg_turns: 'Turnos medios / hora', axis_avg_output: 'Tokens de salida medios / hora',
    axis_cache: 'Caché', axis_io: 'Entrada / Salida',
    series_input: 'Entrada', series_output: 'Salida', series_cache_read: 'Lectura de caché',
    series_cache_creation: 'Creación de caché', series_est_cost: 'Costo est.',
    tt_est_cost: ' Costo est.: {c}', tt_tokens: ' {label}: {v} tokens',
    tt_total: ' Total: {v} · {turns} turnos', tt_series: ' {label}: {v}',
    tt_peak_suffix: ' · Pico — horas de EE. UU. de Anthropic',
    tt_avg_turns: ' Turnos medios: {v}', tt_avg_output: ' Salida media: {v}',
    hourly_days: '{n} días promediados · {tz}', hourly_day: '{n} día promediado · {tz}',
    hourly_nodata: 'Sin datos · {tz}',
    th_model: 'Modelo', th_turns: 'Turnos', th_input: 'Entrada', th_output: 'Salida',
    th_cache_read: 'Lectura de caché', th_cache_creation: 'Creación de caché', th_est_cost: 'Costo est.',
    th_type: 'Tipo', th_started: 'Iniciado', th_tool_uses: 'Usos de herramientas', th_duration: 'Duración',
    th_tokens: 'Tokens', th_session: 'Sesión', th_project: 'Proyecto', th_title: 'Título',
    th_last_active: 'Última actividad', th_sessions: 'Sesiones', th_branch: 'Rama',
    sec_costmodel: 'Costo por modelo', sec_dispatches: 'Principales despachos de subagentes',
    sec_dispatches_info: 'Ordenado por tokens totales. "unknown" significa que no se encontró el registro de despacho padre.',
    sec_sessions: 'Sesiones recientes', sec_costproject: 'Costo por proyecto',
    sec_costbranch: 'Costo por proyecto y rama',
    csv_title_dispatches: 'Exportar todos los despachos de subagentes filtrados a CSV',
    csv_title_sessions: 'Exportar todas las sesiones filtradas a CSV',
    csv_title_projects: 'Exportar todos los proyectos a CSV',
    csv_title_branch: 'Exportar el desglose por proyecto+rama a CSV',
    stat_sessions: 'Sesiones', stat_turns: 'Turnos', stat_input: 'Tokens de entrada',
    stat_output: 'Tokens de salida', stat_subagent: 'Tokens de subagentes', stat_cache_read: 'Lectura de caché',
    stat_cache_creation: 'Creación de caché', stat_est_cost: 'Costo est.',
    sub_included: 'incluido en los totales', sub_from_cache: 'de la caché de prompts',
    sub_writes_cache: 'escrituras en la caché de prompts', sub_pricing: 'precios API, junio de 2026',
    show_less: 'Mostrar menos ▴', show_more: 'Mostrar más ▾',
    csv_download_all: 'Descargar CSV para ver todo ({n})',
    no_dispatches: 'No hay despachos de subagentes en el rango seleccionado.',
    untitled: 'Sin título', duration_suffix: ' min',
    collapse_title: 'Contraer / expandir sección',
    footer_pricing: 'Estimaciones de costo basadas en los precios de la API de Anthropic (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) a junio de 2026. Solo se incluyen en los cálculos de costo los modelos cuyo nombre contiene <em>fable</em>, <em>mythos</em>, <em>opus</em>, <em>sonnet</em> o <em>haiku</em>. Los costos reales para suscriptores Max/Pro difieren de los precios de la API.',
    footer_created_by: 'Creado por:', footer_license: 'Licencia: MIT',
    footer_version: 'Versión', footer_get_ext: 'Obtener la extensión de VS Code',
    footer_update: 'Actualizar a v{v}',
  },
  fr: {
    app_title: 'Tableau de bord d\'utilisation de Claude Code',
    header_title: 'Utilisation de Claude Code',
    meta_loading: 'Chargement...',
    meta_updated: 'Mis à jour : {t}',
    meta_autorefresh: 'Actualisation auto dans 30 s',
    meta_retrying: '{e} — nouvelle tentative…',
    rescan: '↻ Rescanner',
    rescan_title: 'Rechercher les nouvelles données depuis la dernière mise à jour. Ajoute de nouveaux tours sans affecter l\'historique existant.',
    rescan_scanning: '↻ Analyse...',
    rescan_result: '↻ Rescanner ({new} nouveaux, {updated} mis à jour)',
    rescan_error: '↻ Rescanner (erreur)',
    filter_models: 'Modèles',
    filter_range: 'Période',
    filter_language: 'Langue',
    models_all: 'Tous les modèles',
    models_none_sel: 'Aucun modèle',
    models_all_anthropic: 'Tout Anthropic',
    models_all_anthropic_plus: 'Tout Anthropic +{n}',
    btn_all: 'Tous',
    btn_none: 'Aucun',
    range_today: 'Aujourd\'hui', range_week: 'Cette semaine', range_month: 'Ce mois',
    'range_prev-month': 'Mois précédent', range_7d: '7 derniers jours', range_30d: '30 derniers jours',
    range_90d: '90 derniers jours', range_all: 'Tout', range_aria: 'Plage de dates',
    jump_aria: 'Aller à la section', jump_overview: 'Aperçu', jump_graphs: 'Graphiques',
    jump_daily: 'Quotidien', jump_distribution: 'Distribution', jump_bymodel: 'Par modèle',
    jump_topprojects: 'Projets', jump_subagents: 'Sous-agents', jump_tables: 'Tableaux',
    jump_costmodel: 'Coût par modèle', jump_dispatches: 'Répartitions', jump_sessions: 'Sessions',
    jump_costproject: 'Coût par projet', jump_costbranch: 'Coût par projet et branche',
    chart_daily: 'Utilisation quotidienne des tokens', chart_hourly: 'Distribution horaire moyenne',
    chart_subagent: 'Tokens de sous-agents par type', chart_bymodel: 'Par modèle',
    chart_topprojects: 'Projets avec le plus de tokens',
    peak_legend: 'Heures de pointe (PT)',
    peak_title: 'Lun–Ven 05:00–11:00 PT — fenêtre de limitation aux heures de pointe d\'Anthropic',
    tz_local: 'Local', tz_utc: 'UTC',
    axis_avg_turns: 'Tours moyens / heure', axis_avg_output: 'Tokens de sortie moyens / heure',
    axis_cache: 'Cache', axis_io: 'Entrée / Sortie',
    series_input: 'Entrée', series_output: 'Sortie', series_cache_read: 'Lecture cache',
    series_cache_creation: 'Création cache', series_est_cost: 'Coût est.',
    tt_est_cost: ' Coût est. : {c}', tt_tokens: ' {label} : {v} tokens',
    tt_total: ' Total : {v} · {turns} tours', tt_series: ' {label} : {v}',
    tt_peak_suffix: ' · Pointe — heures US d\'Anthropic',
    tt_avg_turns: ' Tours moyens : {v}', tt_avg_output: ' Sortie moyenne : {v}',
    hourly_days: '{n} jours moyennés · {tz}', hourly_day: '{n} jour moyenné · {tz}',
    hourly_nodata: 'Aucune donnée · {tz}',
    th_model: 'Modèle', th_turns: 'Tours', th_input: 'Entrée', th_output: 'Sortie',
    th_cache_read: 'Lecture cache', th_cache_creation: 'Création cache', th_est_cost: 'Coût est.',
    th_type: 'Type', th_started: 'Démarré', th_tool_uses: 'Utilisations d\'outils', th_duration: 'Durée',
    th_tokens: 'Tokens', th_session: 'Session', th_project: 'Projet', th_title: 'Titre',
    th_last_active: 'Dernière activité', th_sessions: 'Sessions', th_branch: 'Branche',
    sec_costmodel: 'Coût par modèle', sec_dispatches: 'Principales répartitions de sous-agents',
    sec_dispatches_info: 'Classé par tokens totaux. « unknown » signifie que l\'enregistrement de répartition parent est introuvable.',
    sec_sessions: 'Sessions récentes', sec_costproject: 'Coût par projet',
    sec_costbranch: 'Coût par projet et branche',
    csv_title_dispatches: 'Exporter toutes les répartitions de sous-agents filtrées en CSV',
    csv_title_sessions: 'Exporter toutes les sessions filtrées en CSV',
    csv_title_projects: 'Exporter tous les projets en CSV',
    csv_title_branch: 'Exporter la répartition projet+branche en CSV',
    stat_sessions: 'Sessions', stat_turns: 'Tours', stat_input: 'Tokens d\'entrée',
    stat_output: 'Tokens de sortie', stat_subagent: 'Tokens de sous-agents', stat_cache_read: 'Lecture cache',
    stat_cache_creation: 'Création cache', stat_est_cost: 'Coût est.',
    sub_included: 'inclus dans les totaux', sub_from_cache: 'depuis le cache de prompts',
    sub_writes_cache: 'écritures dans le cache de prompts', sub_pricing: 'tarifs API, juin 2026',
    show_less: 'Afficher moins ▴', show_more: 'Afficher plus ▾',
    csv_download_all: 'Télécharger le CSV pour tout voir ({n})',
    no_dispatches: 'Aucune répartition de sous-agent dans la plage sélectionnée.',
    untitled: 'Sans titre', duration_suffix: ' min',
    collapse_title: 'Réduire / développer la section',
    footer_pricing: 'Estimations de coût basées sur les tarifs de l\'API Anthropic (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) en juin 2026. Seuls les modèles dont le nom contient <em>fable</em>, <em>mythos</em>, <em>opus</em>, <em>sonnet</em> ou <em>haiku</em> sont inclus dans les calculs de coût. Les coûts réels pour les abonnés Max/Pro diffèrent des tarifs de l\'API.',
    footer_created_by: 'Créé par :', footer_license: 'Licence : MIT',
    footer_version: 'Version', footer_get_ext: 'Obtenir l\'extension VS Code',
    footer_update: 'Mettre à jour vers v{v}',
  },
  de: {
    app_title: 'Claude Code Nutzungs-Dashboard',
    header_title: 'Claude Code Nutzung',
    meta_loading: 'Wird geladen...',
    meta_updated: 'Aktualisiert: {t}',
    meta_autorefresh: 'Auto-Aktualisierung in 30 s',
    meta_retrying: '{e} — erneuter Versuch…',
    rescan: '↻ Neu scannen',
    rescan_title: 'Nach neuer Nutzung seit der letzten Aktualisierung suchen. Fügt neue Züge hinzu, ohne den bestehenden Verlauf zu beeinflussen.',
    rescan_scanning: '↻ Scannen...',
    rescan_result: '↻ Neu scannen ({new} neu, {updated} aktualisiert)',
    rescan_error: '↻ Neu scannen (Fehler)',
    filter_models: 'Modelle',
    filter_range: 'Zeitraum',
    filter_language: 'Sprache',
    models_all: 'Alle Modelle',
    models_none_sel: 'Keine Modelle',
    models_all_anthropic: 'Alle Anthropic',
    models_all_anthropic_plus: 'Alle Anthropic +{n}',
    btn_all: 'Alle',
    btn_none: 'Keine',
    range_today: 'Heute', range_week: 'Diese Woche', range_month: 'Dieser Monat',
    'range_prev-month': 'Vormonat', range_7d: 'Letzte 7 Tage', range_30d: 'Letzte 30 Tage',
    range_90d: 'Letzte 90 Tage', range_all: 'Gesamt', range_aria: 'Datumsbereich',
    jump_aria: 'Zum Abschnitt springen', jump_overview: 'Übersicht', jump_graphs: 'Diagramme',
    jump_daily: 'Täglich', jump_distribution: 'Verteilung', jump_bymodel: 'Nach Modell',
    jump_topprojects: 'Projekte', jump_subagents: 'Subagenten', jump_tables: 'Tabellen',
    jump_costmodel: 'Kosten nach Modell', jump_dispatches: 'Zuweisungen', jump_sessions: 'Sitzungen',
    jump_costproject: 'Kosten nach Projekt', jump_costbranch: 'Kosten nach Projekt & Branch',
    chart_daily: 'Tägliche Token-Nutzung', chart_hourly: 'Durchschnittliche stündliche Verteilung',
    chart_subagent: 'Subagent-Tokens nach Typ', chart_bymodel: 'Nach Modell',
    chart_topprojects: 'Projekte mit den meisten Tokens',
    peak_legend: 'Spitzenzeiten (PT)',
    peak_title: 'Mo–Fr 05:00–11:00 PT — Anthropic-Drosselungsfenster für Spitzenzeiten',
    tz_local: 'Lokal', tz_utc: 'UTC',
    axis_avg_turns: 'Ø Züge / Stunde', axis_avg_output: 'Ø Ausgabe-Tokens / Stunde',
    axis_cache: 'Cache', axis_io: 'Eingabe / Ausgabe',
    series_input: 'Eingabe', series_output: 'Ausgabe', series_cache_read: 'Cache-Lesen',
    series_cache_creation: 'Cache-Erstellung', series_est_cost: 'Gesch. Kosten',
    tt_est_cost: ' Gesch. Kosten: {c}', tt_tokens: ' {label}: {v} Tokens',
    tt_total: ' Gesamt: {v} · {turns} Züge', tt_series: ' {label}: {v}',
    tt_peak_suffix: ' · Spitze — Anthropic US-Zeiten',
    tt_avg_turns: ' Ø Züge: {v}', tt_avg_output: ' Ø Ausgabe: {v}',
    hourly_days: '{n} Tage gemittelt · {tz}', hourly_day: '{n} Tag gemittelt · {tz}',
    hourly_nodata: 'Keine Daten · {tz}',
    th_model: 'Modell', th_turns: 'Züge', th_input: 'Eingabe', th_output: 'Ausgabe',
    th_cache_read: 'Cache-Lesen', th_cache_creation: 'Cache-Erstellung', th_est_cost: 'Gesch. Kosten',
    th_type: 'Typ', th_started: 'Gestartet', th_tool_uses: 'Tool-Nutzungen', th_duration: 'Dauer',
    th_tokens: 'Tokens', th_session: 'Sitzung', th_project: 'Projekt', th_title: 'Titel',
    th_last_active: 'Zuletzt aktiv', th_sessions: 'Sitzungen', th_branch: 'Branch',
    sec_costmodel: 'Kosten nach Modell', sec_dispatches: 'Top-Subagent-Zuweisungen',
    sec_dispatches_info: 'Nach Gesamt-Tokens sortiert. "unknown" bedeutet, dass der übergeordnete Zuweisungsdatensatz nicht gefunden wurde.',
    sec_sessions: 'Letzte Sitzungen', sec_costproject: 'Kosten nach Projekt',
    sec_costbranch: 'Kosten nach Projekt & Branch',
    csv_title_dispatches: 'Alle gefilterten Subagent-Zuweisungen als CSV exportieren',
    csv_title_sessions: 'Alle gefilterten Sitzungen als CSV exportieren',
    csv_title_projects: 'Alle Projekte als CSV exportieren',
    csv_title_branch: 'Projekt+Branch-Aufschlüsselung als CSV exportieren',
    stat_sessions: 'Sitzungen', stat_turns: 'Züge', stat_input: 'Eingabe-Tokens',
    stat_output: 'Ausgabe-Tokens', stat_subagent: 'Subagent-Tokens', stat_cache_read: 'Cache-Lesen',
    stat_cache_creation: 'Cache-Erstellung', stat_est_cost: 'Gesch. Kosten',
    sub_included: 'in Summen enthalten', sub_from_cache: 'aus dem Prompt-Cache',
    sub_writes_cache: 'Schreibvorgänge in den Prompt-Cache', sub_pricing: 'API-Preise, Juni 2026',
    show_less: 'Weniger anzeigen ▴', show_more: 'Mehr anzeigen ▾',
    csv_download_all: 'CSV herunterladen, um alle anzuzeigen ({n})',
    no_dispatches: 'Keine Subagent-Zuweisungen im ausgewählten Zeitraum.',
    untitled: 'Ohne Titel', duration_suffix: ' Min',
    collapse_title: 'Abschnitt ein-/ausklappen',
    footer_pricing: 'Kostenschätzungen basieren auf den Anthropic-API-Preisen (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) mit Stand Juni 2026. Nur Modelle, deren Name <em>fable</em>, <em>mythos</em>, <em>opus</em>, <em>sonnet</em> oder <em>haiku</em> enthält, werden in die Kostenberechnung einbezogen. Die tatsächlichen Kosten für Max/Pro-Abonnenten weichen von den API-Preisen ab.',
    footer_created_by: 'Erstellt von:', footer_license: 'Lizenz: MIT',
    footer_version: 'Version', footer_get_ext: 'VS Code-Erweiterung holen',
    footer_update: 'Auf v{v} aktualisieren',
  },
  ja: {
    app_title: 'Claude Code 使用状況ダッシュボード',
    header_title: 'Claude Code 使用状況',
    meta_loading: '読み込み中...',
    meta_updated: '更新: {t}',
    meta_autorefresh: '30秒後に自動更新',
    meta_retrying: '{e} — 再試行中…',
    rescan: '↻ 再スキャン',
    rescan_title: '前回の更新以降の新しい使用状況をスキャンします。既存の履歴に影響を与えずに新しいターンを追加します。',
    rescan_scanning: '↻ スキャン中...',
    rescan_result: '↻ 再スキャン (新規 {new}、更新 {updated})',
    rescan_error: '↻ 再スキャン (エラー)',
    filter_models: 'モデル',
    filter_range: '期間',
    filter_language: '言語',
    models_all: 'すべてのモデル',
    models_none_sel: 'モデルなし',
    models_all_anthropic: 'すべての Anthropic',
    models_all_anthropic_plus: 'すべての Anthropic +{n}',
    btn_all: 'すべて',
    btn_none: 'なし',
    range_today: '今日', range_week: '今週', range_month: '今月',
    'range_prev-month': '先月', range_7d: '過去7日間', range_30d: '過去30日間',
    range_90d: '過去90日間', range_all: '全期間', range_aria: '日付範囲',
    jump_aria: 'セクションへ移動', jump_overview: '概要', jump_graphs: 'グラフ',
    jump_daily: '日次', jump_distribution: '分布', jump_bymodel: 'モデル別',
    jump_topprojects: 'プロジェクト', jump_subagents: 'サブエージェント', jump_tables: 'テーブル',
    jump_costmodel: 'モデル別コスト', jump_dispatches: 'ディスパッチ', jump_sessions: 'セッション',
    jump_costproject: 'プロジェクト別コスト', jump_costbranch: 'プロジェクト・ブランチ別コスト',
    chart_daily: '日次トークン使用量', chart_hourly: '平均時間帯別分布',
    chart_subagent: 'タイプ別サブエージェントトークン', chart_bymodel: 'モデル別',
    chart_topprojects: 'トークンの多いプロジェクト',
    peak_legend: 'ピーク時間 (PT)',
    peak_title: '月〜金 05:00〜11:00 PT — Anthropic のピーク時間帯スロットリング',
    tz_local: 'ローカル', tz_utc: 'UTC',
    axis_avg_turns: '平均ターン / 時間', axis_avg_output: '平均出力トークン / 時間',
    axis_cache: 'キャッシュ', axis_io: '入力 / 出力',
    series_input: '入力', series_output: '出力', series_cache_read: 'キャッシュ読取',
    series_cache_creation: 'キャッシュ作成', series_est_cost: '推定コスト',
    tt_est_cost: ' 推定コスト: {c}', tt_tokens: ' {label}: {v} トークン',
    tt_total: ' 合計: {v} · {turns} ターン', tt_series: ' {label}: {v}',
    tt_peak_suffix: ' · ピーク — Anthropic 米国時間',
    tt_avg_turns: ' 平均ターン: {v}', tt_avg_output: ' 平均出力: {v}',
    hourly_days: '{n}日平均 · {tz}', hourly_day: '{n}日平均 · {tz}',
    hourly_nodata: 'データなし · {tz}',
    th_model: 'モデル', th_turns: 'ターン', th_input: '入力', th_output: '出力',
    th_cache_read: 'キャッシュ読取', th_cache_creation: 'キャッシュ作成', th_est_cost: '推定コスト',
    th_type: 'タイプ', th_started: '開始', th_tool_uses: 'ツール使用', th_duration: '期間',
    th_tokens: 'トークン', th_session: 'セッション', th_project: 'プロジェクト', th_title: 'タイトル',
    th_last_active: '最終アクティブ', th_sessions: 'セッション', th_branch: 'ブランチ',
    sec_costmodel: 'モデル別コスト', sec_dispatches: 'サブエージェントディスパッチ上位',
    sec_dispatches_info: '合計トークン順。"unknown" は親ディスパッチレコードが見つからないことを意味します。',
    sec_sessions: '最近のセッション', sec_costproject: 'プロジェクト別コスト',
    sec_costbranch: 'プロジェクト・ブランチ別コスト',
    csv_title_dispatches: 'フィルタ済みのすべてのサブエージェントディスパッチを CSV に出力',
    csv_title_sessions: 'フィルタ済みのすべてのセッションを CSV に出力',
    csv_title_projects: 'すべてのプロジェクトを CSV に出力',
    csv_title_branch: 'プロジェクト+ブランチの内訳を CSV に出力',
    stat_sessions: 'セッション', stat_turns: 'ターン', stat_input: '入力トークン',
    stat_output: '出力トークン', stat_subagent: 'サブエージェントトークン', stat_cache_read: 'キャッシュ読取',
    stat_cache_creation: 'キャッシュ作成', stat_est_cost: '推定コスト',
    sub_included: '合計に含む', sub_from_cache: 'プロンプトキャッシュから',
    sub_writes_cache: 'プロンプトキャッシュへの書込', sub_pricing: 'API 料金、2026年6月',
    show_less: '折りたたむ ▴', show_more: 'もっと見る ▾',
    csv_download_all: 'CSV をダウンロードしてすべて表示 ({n})',
    no_dispatches: '選択した期間にサブエージェントディスパッチはありません。',
    untitled: '無題', duration_suffix: '分',
    collapse_title: 'セクションの折りたたみ / 展開',
    footer_pricing: 'コスト見積もりは Anthropic API 料金 (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) に基づき、2026年6月時点のものです。名前に <em>fable</em>、<em>mythos</em>、<em>opus</em>、<em>sonnet</em>、<em>haiku</em> を含むモデルのみコスト計算に含まれます。Max/Pro 加入者の実際のコストは API 料金と異なります。',
    footer_created_by: '作成者:', footer_license: 'ライセンス: MIT',
    footer_version: 'バージョン', footer_get_ext: 'VS Code 拡張機能を入手',
    footer_update: 'v{v} に更新',
  },
  ko: {
    app_title: 'Claude Code 사용량 대시보드',
    header_title: 'Claude Code 사용량',
    meta_loading: '로딩 중...',
    meta_updated: '업데이트: {t}',
    meta_autorefresh: '30초 후 자동 새로고침',
    meta_retrying: '{e} — 재시도 중…',
    rescan: '↻ 다시 스캔',
    rescan_title: '마지막 업데이트 이후의 새 사용량을 스캔합니다. 기존 기록에 영향을 주지 않고 새 턴을 추가합니다.',
    rescan_scanning: '↻ 스캔 중...',
    rescan_result: '↻ 다시 스캔 (신규 {new}, 업데이트 {updated})',
    rescan_error: '↻ 다시 스캔 (오류)',
    filter_models: '모델',
    filter_range: '범위',
    filter_language: '언어',
    models_all: '모든 모델',
    models_none_sel: '모델 없음',
    models_all_anthropic: '모든 Anthropic',
    models_all_anthropic_plus: '모든 Anthropic +{n}',
    btn_all: '전체',
    btn_none: '없음',
    range_today: '오늘', range_week: '이번 주', range_month: '이번 달',
    'range_prev-month': '지난 달', range_7d: '지난 7일', range_30d: '지난 30일',
    range_90d: '지난 90일', range_all: '전체 기간', range_aria: '날짜 범위',
    jump_aria: '섹션으로 이동', jump_overview: '개요', jump_graphs: '그래프',
    jump_daily: '일별', jump_distribution: '분포', jump_bymodel: '모델별',
    jump_topprojects: '프로젝트', jump_subagents: '서브에이전트', jump_tables: '테이블',
    jump_costmodel: '모델별 비용', jump_dispatches: '디스패치', jump_sessions: '세션',
    jump_costproject: '프로젝트별 비용', jump_costbranch: '프로젝트·브랜치별 비용',
    chart_daily: '일별 토큰 사용량', chart_hourly: '평균 시간대별 분포',
    chart_subagent: '유형별 서브에이전트 토큰', chart_bymodel: '모델별',
    chart_topprojects: '토큰이 가장 많은 프로젝트',
    peak_legend: '피크 시간 (PT)',
    peak_title: '월–금 05:00–11:00 PT — Anthropic 피크 시간대 제한 구간',
    tz_local: '로컬', tz_utc: 'UTC',
    axis_avg_turns: '평균 턴 / 시간', axis_avg_output: '평균 출력 토큰 / 시간',
    axis_cache: '캐시', axis_io: '입력 / 출력',
    series_input: '입력', series_output: '출력', series_cache_read: '캐시 읽기',
    series_cache_creation: '캐시 생성', series_est_cost: '예상 비용',
    tt_est_cost: ' 예상 비용: {c}', tt_tokens: ' {label}: {v} 토큰',
    tt_total: ' 합계: {v} · {turns} 턴', tt_series: ' {label}: {v}',
    tt_peak_suffix: ' · 피크 — Anthropic 미국 시간',
    tt_avg_turns: ' 평균 턴: {v}', tt_avg_output: ' 평균 출력: {v}',
    hourly_days: '{n}일 평균 · {tz}', hourly_day: '{n}일 평균 · {tz}',
    hourly_nodata: '데이터 없음 · {tz}',
    th_model: '모델', th_turns: '턴', th_input: '입력', th_output: '출력',
    th_cache_read: '캐시 읽기', th_cache_creation: '캐시 생성', th_est_cost: '예상 비용',
    th_type: '유형', th_started: '시작', th_tool_uses: '도구 사용', th_duration: '기간',
    th_tokens: '토큰', th_session: '세션', th_project: '프로젝트', th_title: '제목',
    th_last_active: '마지막 활동', th_sessions: '세션', th_branch: '브랜치',
    sec_costmodel: '모델별 비용', sec_dispatches: '상위 서브에이전트 디스패치',
    sec_dispatches_info: '총 토큰 기준 정렬. "unknown"은 상위 디스패치 레코드를 찾을 수 없음을 의미합니다.',
    sec_sessions: '최근 세션', sec_costproject: '프로젝트별 비용',
    sec_costbranch: '프로젝트·브랜치별 비용',
    csv_title_dispatches: '필터링된 모든 서브에이전트 디스패치를 CSV로 내보내기',
    csv_title_sessions: '필터링된 모든 세션을 CSV로 내보내기',
    csv_title_projects: '모든 프로젝트를 CSV로 내보내기',
    csv_title_branch: '프로젝트+브랜치 분석을 CSV로 내보내기',
    stat_sessions: '세션', stat_turns: '턴', stat_input: '입력 토큰',
    stat_output: '출력 토큰', stat_subagent: '서브에이전트 토큰', stat_cache_read: '캐시 읽기',
    stat_cache_creation: '캐시 생성', stat_est_cost: '예상 비용',
    sub_included: '합계에 포함', sub_from_cache: '프롬프트 캐시에서',
    sub_writes_cache: '프롬프트 캐시에 쓰기', sub_pricing: 'API 요금, 2026년 6월',
    show_less: '접기 ▴', show_more: '더 보기 ▾',
    csv_download_all: '전체 보기 CSV 다운로드 ({n})',
    no_dispatches: '선택한 범위에 서브에이전트 디스패치가 없습니다.',
    untitled: '제목 없음', duration_suffix: '분',
    collapse_title: '섹션 접기 / 펼치기',
    footer_pricing: '비용 추정치는 2026년 6월 기준 Anthropic API 요금 (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>)을 기반으로 합니다. 이름에 <em>fable</em>, <em>mythos</em>, <em>opus</em>, <em>sonnet</em> 또는 <em>haiku</em>가 포함된 모델만 비용 계산에 포함됩니다. Max/Pro 구독자의 실제 비용은 API 요금과 다릅니다.',
    footer_created_by: '제작:', footer_license: '라이선스: MIT',
    footer_version: '버전', footer_get_ext: 'VS Code 확장 프로그램 받기',
    footer_update: 'v{v}로 업데이트',
  },
  pt: {
    app_title: 'Painel de uso do Claude Code',
    header_title: 'Uso do Claude Code',
    meta_loading: 'Carregando...',
    meta_updated: 'Atualizado: {t}',
    meta_autorefresh: 'Atualização automática em 30 s',
    meta_retrying: '{e} — tentando novamente…',
    rescan: '↻ Reescanear',
    rescan_title: 'Procurar uso novo desde a última atualização. Adiciona novos turnos sem afetar o histórico existente.',
    rescan_scanning: '↻ Escaneando...',
    rescan_result: '↻ Reescanear ({new} novos, {updated} atualizados)',
    rescan_error: '↻ Reescanear (erro)',
    filter_models: 'Modelos',
    filter_range: 'Período',
    filter_language: 'Idioma',
    models_all: 'Todos os modelos',
    models_none_sel: 'Nenhum modelo',
    models_all_anthropic: 'Todos Anthropic',
    models_all_anthropic_plus: 'Todos Anthropic +{n}',
    btn_all: 'Todos',
    btn_none: 'Nenhum',
    range_today: 'Hoje', range_week: 'Esta semana', range_month: 'Este mês',
    'range_prev-month': 'Mês anterior', range_7d: 'Últimos 7 dias', range_30d: 'Últimos 30 dias',
    range_90d: 'Últimos 90 dias', range_all: 'Todo o período', range_aria: 'Intervalo de datas',
    jump_aria: 'Ir para a seção', jump_overview: 'Visão geral', jump_graphs: 'Gráficos',
    jump_daily: 'Diário', jump_distribution: 'Distribuição', jump_bymodel: 'Por modelo',
    jump_topprojects: 'Projetos', jump_subagents: 'Subagentes', jump_tables: 'Tabelas',
    jump_costmodel: 'Custo por modelo', jump_dispatches: 'Despachos', jump_sessions: 'Sessões',
    jump_costproject: 'Custo por projeto', jump_costbranch: 'Custo por projeto e ramo',
    chart_daily: 'Uso diário de tokens', chart_hourly: 'Distribuição horária média',
    chart_subagent: 'Tokens de subagentes por tipo', chart_bymodel: 'Por modelo',
    chart_topprojects: 'Projetos com mais tokens',
    peak_legend: 'Horário de pico (PT)',
    peak_title: 'Seg–Sex 05:00–11:00 PT — janela de limitação de horário de pico da Anthropic',
    tz_local: 'Local', tz_utc: 'UTC',
    axis_avg_turns: 'Turnos médios / hora', axis_avg_output: 'Tokens de saída médios / hora',
    axis_cache: 'Cache', axis_io: 'Entrada / Saída',
    series_input: 'Entrada', series_output: 'Saída', series_cache_read: 'Leitura de cache',
    series_cache_creation: 'Criação de cache', series_est_cost: 'Custo est.',
    tt_est_cost: ' Custo est.: {c}', tt_tokens: ' {label}: {v} tokens',
    tt_total: ' Total: {v} · {turns} turnos', tt_series: ' {label}: {v}',
    tt_peak_suffix: ' · Pico — horário dos EUA da Anthropic',
    tt_avg_turns: ' Turnos médios: {v}', tt_avg_output: ' Saída média: {v}',
    hourly_days: '{n} dias calculados · {tz}', hourly_day: '{n} dia calculado · {tz}',
    hourly_nodata: 'Sem dados · {tz}',
    th_model: 'Modelo', th_turns: 'Turnos', th_input: 'Entrada', th_output: 'Saída',
    th_cache_read: 'Leitura de cache', th_cache_creation: 'Criação de cache', th_est_cost: 'Custo est.',
    th_type: 'Tipo', th_started: 'Iniciado', th_tool_uses: 'Usos de ferramentas', th_duration: 'Duração',
    th_tokens: 'Tokens', th_session: 'Sessão', th_project: 'Projeto', th_title: 'Título',
    th_last_active: 'Última atividade', th_sessions: 'Sessões', th_branch: 'Ramo',
    sec_costmodel: 'Custo por modelo', sec_dispatches: 'Principais despachos de subagentes',
    sec_dispatches_info: 'Ordenado por tokens totais. "unknown" significa que o registro de despacho pai não foi encontrado.',
    sec_sessions: 'Sessões recentes', sec_costproject: 'Custo por projeto',
    sec_costbranch: 'Custo por projeto e ramo',
    csv_title_dispatches: 'Exportar todos os despachos de subagentes filtrados para CSV',
    csv_title_sessions: 'Exportar todas as sessões filtradas para CSV',
    csv_title_projects: 'Exportar todos os projetos para CSV',
    csv_title_branch: 'Exportar o detalhamento por projeto+ramo para CSV',
    stat_sessions: 'Sessões', stat_turns: 'Turnos', stat_input: 'Tokens de entrada',
    stat_output: 'Tokens de saída', stat_subagent: 'Tokens de subagentes', stat_cache_read: 'Leitura de cache',
    stat_cache_creation: 'Criação de cache', stat_est_cost: 'Custo est.',
    sub_included: 'incluído nos totais', sub_from_cache: 'do cache de prompts',
    sub_writes_cache: 'gravações no cache de prompts', sub_pricing: 'preços da API, junho de 2026',
    show_less: 'Mostrar menos ▴', show_more: 'Mostrar mais ▾',
    csv_download_all: 'Baixar CSV para ver tudo ({n})',
    no_dispatches: 'Nenhum despacho de subagente no intervalo selecionado.',
    untitled: 'Sem título', duration_suffix: ' min',
    collapse_title: 'Recolher / expandir seção',
    footer_pricing: 'Estimativas de custo baseadas nos preços da API da Anthropic (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) em junho de 2026. Apenas modelos cujo nome contém <em>fable</em>, <em>mythos</em>, <em>opus</em>, <em>sonnet</em> ou <em>haiku</em> são incluídos nos cálculos de custo. Os custos reais para assinantes Max/Pro diferem dos preços da API.',
    footer_created_by: 'Criado por:', footer_license: 'Licença: MIT',
    footer_version: 'Versão', footer_get_ext: 'Obter a extensão do VS Code',
    footer_update: 'Atualizar para v{v}',
  },
  ru: {
    app_title: 'Панель использования Claude Code',
    header_title: 'Использование Claude Code',
    meta_loading: 'Загрузка...',
    meta_updated: 'Обновлено: {t}',
    meta_autorefresh: 'Автообновление через 30 с',
    meta_retrying: '{e} — повтор…',
    rescan: '↻ Пересканировать',
    rescan_title: 'Поиск нового использования с момента последнего обновления. Добавляет новые обмены, не затрагивая существующую историю.',
    rescan_scanning: '↻ Сканирование...',
    rescan_result: '↻ Пересканировать ({new} новых, {updated} обновлено)',
    rescan_error: '↻ Пересканировать (ошибка)',
    filter_models: 'Модели',
    filter_range: 'Период',
    filter_language: 'Язык',
    models_all: 'Все модели',
    models_none_sel: 'Нет моделей',
    models_all_anthropic: 'Все Anthropic',
    models_all_anthropic_plus: 'Все Anthropic +{n}',
    btn_all: 'Все',
    btn_none: 'Нет',
    range_today: 'Сегодня', range_week: 'Эта неделя', range_month: 'Этот месяц',
    'range_prev-month': 'Прошлый месяц', range_7d: 'Последние 7 дней', range_30d: 'Последние 30 дней',
    range_90d: 'Последние 90 дней', range_all: 'Всё время', range_aria: 'Диапазон дат',
    jump_aria: 'Перейти к разделу', jump_overview: 'Обзор', jump_graphs: 'Графики',
    jump_daily: 'По дням', jump_distribution: 'Распределение', jump_bymodel: 'По модели',
    jump_topprojects: 'Проекты', jump_subagents: 'Субагенты', jump_tables: 'Таблицы',
    jump_costmodel: 'Стоимость по модели', jump_dispatches: 'Диспетчеризации', jump_sessions: 'Сессии',
    jump_costproject: 'Стоимость по проекту', jump_costbranch: 'Стоимость по проекту и ветке',
    chart_daily: 'Ежедневное использование токенов', chart_hourly: 'Среднее почасовое распределение',
    chart_subagent: 'Токены субагентов по типу', chart_bymodel: 'По модели',
    chart_topprojects: 'Проекты с наибольшим числом токенов',
    peak_legend: 'Часы пик (PT)',
    peak_title: 'Пн–Пт 05:00–11:00 PT — окно ограничения в часы пик Anthropic',
    tz_local: 'Местное', tz_utc: 'UTC',
    axis_avg_turns: 'Ср. обмены / час', axis_avg_output: 'Ср. токены вывода / час',
    axis_cache: 'Кэш', axis_io: 'Ввод / Вывод',
    series_input: 'Ввод', series_output: 'Вывод', series_cache_read: 'Чтение кэша',
    series_cache_creation: 'Создание кэша', series_est_cost: 'Оцен. стоимость',
    tt_est_cost: ' Оцен. стоимость: {c}', tt_tokens: ' {label}: {v} токенов',
    tt_total: ' Итого: {v} · {turns} обменов', tt_series: ' {label}: {v}',
    tt_peak_suffix: ' · Пик — время США Anthropic',
    tt_avg_turns: ' Ср. обмены: {v}', tt_avg_output: ' Ср. вывод: {v}',
    hourly_days: 'усреднено за {n} дн. · {tz}', hourly_day: 'усреднено за {n} дн. · {tz}',
    hourly_nodata: 'Нет данных · {tz}',
    th_model: 'Модель', th_turns: 'Обмены', th_input: 'Ввод', th_output: 'Вывод',
    th_cache_read: 'Чтение кэша', th_cache_creation: 'Создание кэша', th_est_cost: 'Оцен. стоимость',
    th_type: 'Тип', th_started: 'Начато', th_tool_uses: 'Вызовы инструментов', th_duration: 'Длительность',
    th_tokens: 'Токены', th_session: 'Сессия', th_project: 'Проект', th_title: 'Название',
    th_last_active: 'Последняя активность', th_sessions: 'Сессии', th_branch: 'Ветка',
    sec_costmodel: 'Стоимость по модели', sec_dispatches: 'Топ диспетчеризаций субагентов',
    sec_dispatches_info: 'Сортировка по общему числу токенов. "unknown" означает, что родительская запись диспетчеризации не найдена.',
    sec_sessions: 'Недавние сессии', sec_costproject: 'Стоимость по проекту',
    sec_costbranch: 'Стоимость по проекту и ветке',
    csv_title_dispatches: 'Экспортировать все отфильтрованные диспетчеризации субагентов в CSV',
    csv_title_sessions: 'Экспортировать все отфильтрованные сессии в CSV',
    csv_title_projects: 'Экспортировать все проекты в CSV',
    csv_title_branch: 'Экспортировать разбивку по проекту+ветке в CSV',
    stat_sessions: 'Сессии', stat_turns: 'Обмены', stat_input: 'Токены ввода',
    stat_output: 'Токены вывода', stat_subagent: 'Токены субагентов', stat_cache_read: 'Чтение кэша',
    stat_cache_creation: 'Создание кэша', stat_est_cost: 'Оцен. стоимость',
    sub_included: 'включено в итоги', sub_from_cache: 'из кэша промптов',
    sub_writes_cache: 'записи в кэш промптов', sub_pricing: 'цены API, июнь 2026',
    show_less: 'Свернуть ▴', show_more: 'Показать больше ▾',
    csv_download_all: 'Скачать CSV, чтобы увидеть всё ({n})',
    no_dispatches: 'Нет диспетчеризаций субагентов в выбранном диапазоне.',
    untitled: 'Без названия', duration_suffix: ' мин',
    collapse_title: 'Свернуть / развернуть раздел',
    footer_pricing: 'Оценки стоимости основаны на ценах API Anthropic (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) по состоянию на июнь 2026. В расчёт стоимости включаются только модели, имя которых содержит <em>fable</em>, <em>mythos</em>, <em>opus</em>, <em>sonnet</em> или <em>haiku</em>. Фактические расходы подписчиков Max/Pro отличаются от цен API.',
    footer_created_by: 'Создано:', footer_license: 'Лицензия: MIT',
    footer_version: 'Версия', footer_get_ext: 'Получить расширение VS Code',
    footer_update: 'Обновить до v{v}',
  },
};

// Supported UI languages. Order matters only for navigator.language matching
// (first prefix hit wins); none is a prefix of another, so there's no ambiguity.
const SUPPORTED_LANGS = ['en', 'zh', 'es', 'fr', 'de', 'ja', 'ko', 'pt', 'ru'];
// Map each to a BCP-47 tag for <html lang>. Only zh needs a region for correct
// Han glyph selection; the rest use their bare subtag.
const LANG_TAGS = { en: 'en', zh: 'zh-CN', es: 'es', fr: 'fr', de: 'de', ja: 'ja', ko: 'ko', pt: 'pt', ru: 'ru' };

function detectLang() {
  try {
    const saved = localStorage.getItem('cu_lang');
    if (SUPPORTED_LANGS.includes(saved)) return saved;
  } catch (e) {}
  const nav = (navigator.language || '').toLowerCase();
  for (const l of SUPPORTED_LANGS) {
    if (nav === l || nav.startsWith(l + '-')) return l;
  }
  return 'en';
}
let currentLang = detectLang();

// Translate a key, interpolating {name} placeholders from params. Falls back to
// the English string, then the raw key, so a missing translation never blanks UI.
function t(key, params) {
  const dict = I18N[currentLang] || I18N.en;
  let s = dict[key];
  if (s == null) s = I18N.en[key];
  if (s == null) s = key;
  if (params) {
    for (const k in params) s = s.split('{' + k + '}').join(params[k]);
  }
  return s;
}

// Localized display name for a date range (VALID_RANGES stays keyed off the
// English RANGE_LABELS map, which the tests assert on).
function rangeName(r) { return t('range_' + r); }

// Walk the static markup and (re)apply translations for the current language.
function applyStaticI18n() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.getAttribute('data-i18n'));
  });
  document.querySelectorAll('[data-i18n-html]').forEach(el => {
    el.innerHTML = t(el.getAttribute('data-i18n-html'));
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    el.title = t(el.getAttribute('data-i18n-title'));
  });
  document.querySelectorAll('[data-i18n-aria]').forEach(el => {
    el.setAttribute('aria-label', t(el.getAttribute('data-i18n-aria')));
  });
  // Range dropdown options carry stable values but localized labels.
  document.querySelectorAll('#range-select option').forEach(opt => {
    opt.textContent = rangeName(opt.value);
  });
  document.title = t('app_title');
}

// Switch language: persist, restyle the static chrome, and re-render everything
// dynamic (charts, tables, stat cards, footer, meta) so no English lingers.
function setLang(lang) {
  if (!SUPPORTED_LANGS.includes(lang)) lang = 'en';
  currentLang = lang;
  try { localStorage.setItem('cu_lang', lang); } catch (e) {}
  applyLang();
}

function applyLang() {
  document.documentElement.lang = LANG_TAGS[currentLang] || 'en';
  const langSel = document.getElementById('lang-select');
  if (langSel) langSel.value = currentLang;
  applyStaticI18n();
  // Refresh collapse toggle tooltips (set at init time).
  document.querySelectorAll('[data-card] h2, [data-card] .section-title').forEach(el => {
    if (el.getAttribute('role') === 'button') el.title = t('collapse_title');
  });
  if (allModelsList.length) {
    updateModelTriggerLabel();
  } else {
    const mtl = document.getElementById('model-trigger-label');
    if (mtl) mtl.textContent = t('models_all');
  }
  initFooterMeta();
  if (rawData) {
    // Refresh the "Updated:" line without waiting for the next poll.
    const meta = document.getElementById('meta');
    if (meta && rawData.generated_at) {
      const note = rangeIncludesToday(selectedRange) ? '<br>' + esc(t('meta_autorefresh')) : '';
      meta.innerHTML = t('meta_updated', { t: esc(rawData.generated_at) }) + note;
    }
    applyFilter();
  }
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let allModelsList = [];
let selectedRange = '30d';
let charts = {};
let sessionSortCol = 'last';
let modelSortCol = 'cost';
let modelSortDir = 'desc';
let projectSortCol = 'cost';
let projectSortDir = 'desc';
let branchSortCol = 'cost';
let branchSortDir = 'desc';
let lastFilteredSessions = [];
let lastByModel = [];
let lastByProject = [];
let lastByProjectBranch = [];
let lastFilteredDispatches = [];
let sessionSortDir = 'desc';

// Tables reveal rows in steps: 10 -> 25 -> 50, capped at 50 because rendering
// more than that visibly hurts performance. Past 50 the footer offers a
// "Download CSV to see more" link instead of another in-table step, plus a
// Show less button that resets straight back to 10. Limits persist across
// re-renders so sorting/filtering keeps the user's chosen depth (visible rows
// always reflect the active sort).
const TABLE_STEPS = [10, 25, 50];
const TABLE_MAX = TABLE_STEPS[TABLE_STEPS.length - 1];  // hard cap on in-table rows
// Don't paginate a table that barely exceeds the first step — paging away one or
// two rows just to show a "Show more" button is more annoying than helpful. Below
// this many rows a table always renders in full (no toggle).
const PAGINATE_THRESHOLD = 12;
function nextTableLimit(current, total) {
  for (const s of TABLE_STEPS) {
    if (s > current && s < total) return s;
  }
  return Math.min(total, TABLE_MAX);  // reveal everything, but never past the cap
}
// Rows to actually show: everything when the table is small enough to skip
// paging, otherwise the user's current step.
function shownCount(limit, total) {
  return total <= PAGINATE_THRESHOLD ? total : limit;
}
let modelLimit = TABLE_STEPS[0];
let sessionsLimit = TABLE_STEPS[0];
let projectLimit = TABLE_STEPS[0];
let branchLimit = TABLE_STEPS[0];
let dispatchesLimit = TABLE_STEPS[0];
let hourlyTZ = 'local';  // 'local' or 'utc'

// ── Peak-hour config ───────────────────────────────────────────────────────
// Anthropic throttles Mon–Fri 05:00–11:00 PT. We approximate as fixed UTC hours
// 12–17 (matches PDT; during PST the window shifts by 1h — accepted simplification).
const PEAK_HOURS_UTC = new Set([12, 13, 14, 15, 16, 17]);

// Local-timezone offset in hours (signed). Fractional offsets (e.g. India UTC+5:30)
// are rounded to the nearest hour for bucket alignment.
function localOffsetHours() {
  return Math.round(-new Date().getTimezoneOffset() / 60);
}

// Return the UTC hour (0–23) corresponding to a displayed-hour bucket.
function displayHourToUTC(displayHour, tzMode) {
  if (tzMode === 'utc') return displayHour;
  return ((displayHour - localOffsetHours()) % 24 + 24) % 24;
}

// Return the displayed-hour bucket for a UTC hour.
function utcHourToDisplay(utcHour, tzMode) {
  if (tzMode === 'utc') return utcHour;
  return ((utcHour + localOffsetHours()) % 24 + 24) % 24;
}

function isPeakHour(displayHour, tzMode) {
  return PEAK_HOURS_UTC.has(displayHourToUTC(displayHour, tzMode));
}

function formatHourLabel(h) {
  return String(h).padStart(2, '0') + ':00';
}

function tzDisplayName(tzMode) {
  if (tzMode === 'utc') return 'UTC';
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';
  } catch(e) {
    return 'Local';
  }
}

// ── Pricing (Anthropic API, June 2026) ─────────────────────────────────────
const PRICING = {
  // Fable / Mythos — Anthropic's most capable class, priced at 2x Opus.
  // (Mythos 5 shares Fable 5's pricing; Project-Glasswing access only.)
  'claude-fable-5':    { input: 10.00, output: 50.00, cache_write: 12.50, cache_read: 1.00 },
  'claude-mythos-5':   { input: 10.00, output: 50.00, cache_write: 12.50, cache_read: 1.00 },
  'claude-opus-4-8':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-7':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-6':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-5':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-sonnet-4-7': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-sonnet-4-6': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-sonnet-4-5': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-haiku-4-7':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
  'claude-haiku-4-6':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
  'claude-haiku-4-5':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
};

function isBillable(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('fable') || m.includes('mythos') ||
         m.includes('opus') || m.includes('sonnet') || m.includes('haiku');
}

function getPricing(model) {
  if (!model) return null;
  if (PRICING[model]) return PRICING[model];
  for (const key of Object.keys(PRICING)) {
    if (model.startsWith(key)) return PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('fable') || m.includes('mythos')) return PRICING['claude-fable-5'];
  if (m.includes('opus'))   return PRICING['claude-opus-4-8'];
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
function fmtCost(c)    { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 }); }
function fmtCostBig(c) { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

// ── Chart colors ───────────────────────────────────────────────────────────
// Warm/neutral palette kept in sync with the CSS :root variables so charts match
// the Claude Code interface (less blue). Chart legends/axes use C.axis (a touch
// lighter than --muted so small labels stay legible on the dark card); grid uses
// C.border.
const C = {
  text:   '#BFBFBF',
  muted:  '#4F4F50',
  axis:   '#6F6F70',
  border: '#2C2D2E',
  card:   '#1E1F20',
  blue:   '#48A0C7',
  green:  '#74C991',
  red:    '#C74E39',
  accent: '#d97757',
  amber:  '#D9A84E',
  purple: '#9B7EC7',
  teal:   '#5BB8A3',
  mauve:  '#C77E9B',
};
const TOKEN_COLORS = {
  input:          'rgba(72,160,199,0.85)',   // blue
  output:         'rgba(217,119,87,0.85)',    // accent / coral
  cache_read:     'rgba(116,201,145,0.75)',   // green
  cache_creation: 'rgba(217,168,78,0.75)',    // amber
};
// Hover lifts on a dark theme: bars/series go to full opacity (a touch brighter).
const TOKEN_HOVER = {
  input:          'rgba(72,160,199,1)',
  output:         'rgba(217,119,87,1)',
  cache_read:     'rgba(116,201,145,1)',
  cache_creation: 'rgba(217,168,78,1)',
};
// Donut / categorical palette — warm, Anthropic-leaning (clay, tan, sage, dusty
// blue, mauve, ochre, taupe, terracotta) rather than a saturated rainbow.
const MODEL_COLORS = ['#D97757','#C9A26B','#7FA98C','#6E97A8','#B98AA0','#D9A84E','#A88B6A','#C2705A'];

// Subagent type swatches (table tag tint) — warm/neutral, matching the palette.
const AGENT_TYPE_COLORS = {
  'general-purpose':   '#6E97A8',
  'Explore':           '#9B7EC7',
  'Plan':              '#D9A84E',
  'claude-code-guide': '#48A0C7',
  'auto-compact':      '#A88B6A',
  'unknown':           '#4F4F50',
};
function colorForAgentType(t) { return AGENT_TYPE_COLORS[t] || '#7FA98C'; }
function fmtDuration(ms) {
  if (!ms || ms < 0) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? `${m}m${r}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60}m`;
}

// Tooltip color swatches: solid fill, no border (Chart.js's default draws a
// bordered box that looked offset/inconsistent). Lines use their solid stroke
// color instead of the translucent area fill.
Chart.defaults.color = C.axis;
// multiKeyBackground defaults to white and is drawn behind each tooltip swatch,
// peeking out as a thin white border on plain-box charts — make it transparent.
Chart.defaults.plugins.tooltip.multiKeyBackground = 'transparent';
Chart.defaults.plugins.tooltip.callbacks.labelColor = (ctx) => {
  const ds = ctx.dataset || {};
  let col = Array.isArray(ds.backgroundColor) ? ds.backgroundColor[ctx.dataIndex] : ds.backgroundColor;
  if (ds.type === 'line') col = ds.borderColor;
  return { borderColor: col, backgroundColor: col, borderWidth: 0 };
};

// Legend visibility must survive repaints (filter changes, auto-refresh, sort) —
// the charts are destroyed and rebuilt each render, which otherwise resets any
// series the user toggled off. We track hidden series by label per chart and
// reapply on rebuild: dataset charts via `dataset.hidden`, the doughnut via
// per-slice data visibility (see applyModelHidden).
const hiddenSeries = { daily: new Set(), hourly: new Set(), project: new Set(), model: new Set(), subagent: new Set() };
function legendToggle(key) {
  return (e, item, legend) => {
    const ci = legend.chart;
    const ds = ci.data.datasets[item.datasetIndex];
    ds.hidden = !ds.hidden;
    // Track by a stable seriesKey (not the translated label) so toggles survive
    // both repaints and language switches.
    const sk = ds.seriesKey || ds.label;
    if (ds.hidden) hiddenSeries[key].add(sk); else hiddenSeries[key].delete(sk);
    ci.update();
  };
}

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { 'today': 'Today', 'week': 'This Week', 'month': 'This Month', 'prev-month': 'Previous Month', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time' };
const RANGE_TICKS  = { 'today': 1, 'week': 7, 'month': 15, 'prev-month': 15, '7d': 7, '30d': 15, '90d': 13, 'all': 12 };
const VALID_RANGES = Object.keys(RANGE_LABELS);

// Local calendar date as YYYY-MM-DD. NOT toISOString(), which formats in UTC and
// shifts the day back in UTC+ timezones (that was the "This Month" bug, #151).
function localISODate(d) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function rangeIncludesToday(range) {
  if (range === 'all') return true;
  const { start, end } = getRangeBounds(range);
  const today = localISODate(new Date());
  if (start && today < start) return false;
  if (end && today > end) return false;
  return true;
}

function getRangeBounds(range) {
  if (range === 'all') return { start: null, end: null };
  const today = new Date();
  const iso = localISODate;
  if (range === 'today') {
    const t = iso(today);
    return { start: t, end: t };
  }
  if (range === 'week') {
    const day = today.getDay();
    const diffToMon = day === 0 ? 6 : day - 1;
    const mon = new Date(today); mon.setDate(today.getDate() - diffToMon);
    const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
    return { start: iso(mon), end: iso(sun) };
  }
  if (range === 'month') {
    const start = new Date(today.getFullYear(), today.getMonth(), 1);
    const end = new Date(today.getFullYear(), today.getMonth() + 1, 0);
    return { start: iso(start), end: iso(end) };
  }
  if (range === 'prev-month') {
    const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const end = new Date(today.getFullYear(), today.getMonth(), 0);
    return { start: iso(start), end: iso(end) };
  }
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return { start: iso(d), end: null };
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return VALID_RANGES.includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  const sel = document.getElementById('range-select');
  if (sel) sel.value = range;  // keep the dropdown in sync with programmatic calls
  updateURL();
  applyFilter();
  scheduleAutoRefresh();
}

function setHourlyTZ(mode) {
  hourlyTZ = mode;
  document.querySelectorAll('.tz-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tz === mode)
  );
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('fable') || ml.includes('mythos')) return 0;
  if (ml.includes('opus'))   return 1;
  if (ml.includes('sonnet')) return 2;
  if (ml.includes('haiku'))  return 3;
  return 4;
}

function sortedModels(models) {
  return [...models].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
}

// Compact display name for the collapsed trigger, e.g. "claude-opus-4-8" ->
// "Opus 4.8", "claude-fable-5" -> "Fable 5". Non-Anthropic ids fall back to the
// basename with any provider prefix and trailing date suffix stripped.
function shortModelName(m) {
  const ml = m.toLowerCase();
  let family = null;
  if (ml.includes('fable'))       family = 'Fable';
  else if (ml.includes('mythos')) family = 'Mythos';
  else if (ml.includes('opus'))   family = 'Opus';
  else if (ml.includes('sonnet')) family = 'Sonnet';
  else if (ml.includes('haiku'))  family = 'Haiku';
  if (family) {
    const two = m.match(/(\d+)[._-](\d+)/);
    if (two) return family + ' ' + two[1] + '.' + two[2];
    const one = m.match(/(\d+)/);
    return one ? family + ' ' + one[1] : family;
  }
  let base = m.split('/').pop().split(':')[0];
  base = base.replace(/[-_]?\d{6,}.*$/, '');
  return base || m;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) {
    const billable = allModels.filter(m => isBillable(m));
    // Fallback: if the user only has non-billable / unknown models (e.g. all
    // local-LLM runs), default to all models so the dashboard isn't blank.
    return new Set(billable.length ? billable : allModels);
  }
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  const expected = billable.length ? billable : allModels;
  if (selectedModels.size !== expected.length) return false;
  return expected.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  allModelsList = [...allModels];
  selectedModels = readURLModels(allModels);
  const sorted = sortedModels(allModels);
  const anthropic = sorted.filter(m => isBillable(m));
  const other     = sorted.filter(m => !isBillable(m));
  const rowHTML = m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(m)}" title="${esc(m)}">
      <input type="checkbox" value="${esc(m)}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      <span class="model-cb-box">&#10003;</span>
      <span class="model-cb-text">${esc(m)}</span>
    </label>`;
  };
  let html = '';
  // Only show a group heading when both groups are present — a single-group
  // list doesn't need a label.
  const labelled = anthropic.length && other.length;
  if (anthropic.length) {
    if (labelled) html += '<div class="model-group-label">Anthropic</div>';
    html += anthropic.map(rowHTML).join('');
  }
  if (other.length) {
    if (labelled) html += '<div class="model-group-label">Other providers</div>';
    html += other.map(rowHTML).join('');
  }
  document.getElementById('model-checkboxes').innerHTML = html;
  updateModelTriggerLabel();
}

// Collapsed trigger text, in priority order:
//   "All models"     — everything selected
//   "No models"      — nothing selected
//   "All Anthropic"  — every Anthropic model (opus/sonnet/haiku/mythos/fable)
//                      selected and no other provider; "+N" if some others too
//   "Fable 5, Opus 4.7 +5" — otherwise, first two names + overflow count
function updateModelTriggerLabel() {
  const labelEl = document.getElementById('model-trigger-label');
  if (!labelEl) return;
  const n = selectedModels.size;
  if (n === 0)                    { labelEl.textContent = t('models_none_sel'); return; }
  if (n === allModelsList.length) { labelEl.textContent = t('models_all');      return; }
  const anthropic = allModelsList.filter(m => isBillable(m));
  const others    = allModelsList.filter(m => !isBillable(m));
  if (anthropic.length && anthropic.every(m => selectedModels.has(m))) {
    // n < total (handled above), so when others exist at least one is unselected.
    const otherSel = others.filter(m => selectedModels.has(m)).length;
    labelEl.textContent = otherSel ? t('models_all_anthropic_plus', { n: otherSel }) : t('models_all_anthropic');
    return;
  }
  const chosen = sortedModels(allModelsList).filter(m => selectedModels.has(m));
  const shown = chosen.slice(0, 2).map(shortModelName);
  const extra = chosen.length - shown.length;
  labelEl.textContent = shown.join(', ') + (extra > 0 ? ' +' + extra : '');
}

function toggleModelPanel(event) {
  if (event) event.stopPropagation();
  const panel = document.getElementById('model-panel');
  const trigger = document.getElementById('model-trigger');
  const open = panel.hidden;
  panel.hidden = !open;
  trigger.classList.toggle('open', open);
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function closeModelPanel() {
  const panel = document.getElementById('model-panel');
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  const trigger = document.getElementById('model-trigger');
  trigger.classList.remove('open');
  trigger.setAttribute('aria-expanded', 'false');
}

// Close the panel on outside click or Escape. Clicks inside #model-select
// (including the checkboxes and All/None) keep it open so multiple models can
// be toggled in one pass.
document.addEventListener('click', (e) => {
  const sel = document.getElementById('model-select');
  if (sel && !sel.contains(e.target)) closeModelPanel();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModelPanel(); });

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateModelTriggerLabel();
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateModelTriggerLabel(); updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateModelTriggerLabel(); updateURL(); applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (selectedRange !== '30d') params.set('range', selectedRange);
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

  const { start, end } = getRangeBounds(selectedRange);

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );

  // Daily chart: aggregate by day
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0, cost: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
    d.cost           += calcCost(r.model, r.input, r.output, r.cache_read, r.cache_creation);
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
    selectedModels.has(s.model) && (!start || s.last_date >= start) && (!end || s.last_date <= end)
  );

  // Add session counts into modelMap
  for (const s of filteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

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

  // By project+branch: aggregate from filtered sessions
  const projBranchMap = {};
  for (const s of filteredSessions) {
    const key = s.project + '\x00' + (s.branch || '');
    if (!projBranchMap[key]) projBranchMap[key] = { project: s.project, branch: s.branch || '', input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const pb = projBranchMap[key];
    pb.input          += s.input;
    pb.output         += s.output;
    pb.cache_read     += s.cache_read;
    pb.cache_creation += s.cache_creation;
    pb.turns          += s.turns;
    pb.sessions++;
    pb.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  }
  const byProjectBranch = Object.values(projBranchMap).sort((a, b) => b.cost - a.cost);

  // Totals
  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation), 0),
    subagent_tokens: (rawData.subagent_by_type || [])
      .filter(r => selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end))
      .reduce((s, r) => s + r.input + r.output + r.cache_read + r.cache_creation, 0),
  };

  // Hourly aggregation (filtered by model + range, then bucketed by UTC hour)
  const hourlySrc = (rawData.hourly_by_model || []).filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );
  const hourlyAgg = aggregateHourly(hourlySrc, hourlyTZ);

  // Subagent breakdown by type (filtered by range + selected models)
  const subagentTypeMap = {};
  for (const r of (rawData.subagent_by_type || [])) {
    if (!selectedModels.has(r.model)) continue;
    if (start && r.day < start) continue;
    if (end && r.day > end) continue;
    const k = r.agent_type;
    if (!subagentTypeMap[k]) subagentTypeMap[k] = { agent_type: k, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0 };
    const m = subagentTypeMap[k];
    m.input += r.input; m.output += r.output;
    m.cache_read += r.cache_read; m.cache_creation += r.cache_creation;
    m.turns += r.turns;
  }
  const byAgentType = Object.values(subagentTypeMap).sort((a, b) =>
    (b.input + b.output + b.cache_read + b.cache_creation) -
    (a.input + a.output + a.cache_read + a.cache_creation));

  // Top dispatches: filter by range + selected model. Keep the full filtered set
  // (already ranked by tokens server-side) so the table can page it like Recent
  // Sessions — show more/less plus CSV export of everything.
  const filteredDispatches = (rawData.top_dispatches || []).filter(d =>
    selectedModels.has(d.model) && (!start || d.start_date >= start) && (!end || d.start_date <= end)
  );

  // Update daily chart title
  document.getElementById('daily-chart-title').textContent = t('chart_daily') + ' \u2014 ' + rangeName(selectedRange);
  document.getElementById('hourly-chart-title').textContent = t('chart_hourly') + ' \u2014 ' + rangeName(selectedRange);
  document.getElementById('subagent-chart-title').textContent = t('chart_subagent') + ' \u2014 ' + rangeName(selectedRange);

  renderStats(totals);
  renderDailyChart(daily);
  renderHourlyChart(hourlyAgg);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  renderSubagentChart(byAgentType);
  lastFilteredDispatches = filteredDispatches;
  renderTopDispatches(lastFilteredDispatches);
  lastFilteredSessions = sortSessions(filteredSessions);
  lastByModel = byModel;
  lastByProject = sortProjects(byProject);
  lastByProjectBranch = sortProjectBranch(byProjectBranch);
  renderSessionsTable(lastFilteredSessions);
  renderModelCostTable(lastByModel);
  renderProjectCostTable(lastByProject);
  renderProjectBranchCostTable(lastByProjectBranch);
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(tot) {
  const rangeLabel = rangeName(selectedRange).toLowerCase();
  const stats = [
    { label: t('stat_sessions'),       value: tot.sessions.toLocaleString(), sub: rangeLabel },
    { label: t('stat_turns'),          value: fmt(tot.turns),                sub: rangeLabel },
    { label: t('stat_input'),          value: fmt(tot.input),                sub: rangeLabel },
    { label: t('stat_output'),         value: fmt(tot.output),               sub: rangeLabel },
    { label: t('stat_subagent'),       value: fmt(tot.subagent_tokens || 0), sub: t('sub_included') },
    { label: t('stat_cache_read'),     value: fmt(tot.cache_read),           sub: t('sub_from_cache') },
    { label: t('stat_cache_creation'), value: fmt(tot.cache_creation),       sub: t('sub_writes_cache') },
    { label: t('stat_est_cost'),       value: fmtCostBig(tot.cost),          sub: t('sub_pricing'), color: C.green },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${esc(s.value)}</div>
      ${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}
    </div>
  `).join('');
}

// Bucket rows into 24 hours (display-TZ), summing turns + output, and count
// the unique days in the input so the caller can compute per-day averages.
function aggregateHourly(rows, tzMode) {
  const byHour = {};
  for (let h = 0; h < 24; h++) byHour[h] = { turns: 0, output: 0 };
  const days = new Set();
  for (const r of rows) {
    const displayHour = utcHourToDisplay(r.hour, tzMode);
    byHour[displayHour].turns  += r.turns  || 0;
    byHour[displayHour].output += r.output || 0;
    if (r.day) days.add(r.day);
  }
  const dayCount = days.size;
  const hours = [];
  for (let h = 0; h < 24; h++) {
    hours.push({
      hour:       h,
      avgTurns:   dayCount ? byHour[h].turns  / dayCount : 0,
      avgOutput:  dayCount ? byHour[h].output / dayCount : 0,
      totalTurns: byHour[h].turns,
      peak:       isPeakHour(h, tzMode),
    });
  }
  return { hours, dayCount };
}

function renderHourlyChart(agg) {
  const dayCountEl = document.getElementById('hourly-day-count');
  dayCountEl.textContent = agg.dayCount
    ? t(agg.dayCount === 1 ? 'hourly_day' : 'hourly_days', { n: agg.dayCount, tz: tzDisplayName(hourlyTZ) })
    : t('hourly_nodata', { tz: tzDisplayName(hourlyTZ) });

  const ctx = document.getElementById('chart-hourly').getContext('2d');
  if (charts.hourly) charts.hourly.destroy();

  const labels = agg.hours.map(h => formatHourLabel(h.hour));
  const turns  = agg.hours.map(h => h.avgTurns);
  const output = agg.hours.map(h => h.avgOutput);
  const barColors      = agg.hours.map(h => h.peak ? 'rgba(199,78,57,0.9)' : TOKEN_COLORS.input);
  const barHoverColors = agg.hours.map(h => h.peak ? 'rgba(199,78,57,1)'   : TOKEN_HOVER.input);

  charts.hourly = new Chart(ctx, {
    data: {
      labels: labels,
      datasets: [
        {
          type: 'bar',
          seriesKey: 'avgTurns',
          label: t('axis_avg_turns'),
          hidden: hiddenSeries.hourly.has('avgTurns'),
          data: turns,
          backgroundColor: barColors,
          hoverBackgroundColor: barHoverColors,
          pointStyle: 'rect',
          yAxisID: 'y',
          order: 2,
        },
        {
          type: 'line',
          seriesKey: 'avgOutput',
          label: t('axis_avg_output'),
          hidden: hiddenSeries.hourly.has('avgOutput'),
          data: output,
          borderColor: TOKEN_COLORS.output,
          backgroundColor: 'rgba(217,119,87,0.15)',
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: TOKEN_HOVER.output,
          pointStyle: 'circle',
          pointBackgroundColor: TOKEN_COLORS.output,
          pointBorderColor: TOKEN_COLORS.output,
          tension: 0.3,
          yAxisID: 'y1',
          order: 1,
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { onClick: legendToggle('hourly'), labels: { color: C.axis, usePointStyle: true, boxWidth: 8, boxHeight: 8 } },
        tooltip: {
          usePointStyle: true,
          callbacks: {
            title: (items) => {
              if (!items.length) return '';
              const idx = items[0].dataIndex;
              const h = agg.hours[idx];
              const base = formatHourLabel(h.hour) + ' ' + tzDisplayName(hourlyTZ);
              return h.peak ? base + t('tt_peak_suffix') : base;
            },
            label: (item) => {
              if (item.dataset.seriesKey === 'avgTurns') {
                return t('tt_avg_turns', { v: item.parsed.y.toFixed(2) });
              }
              return t('tt_avg_output', { v: fmt(item.parsed.y) });
            },
          }
        },
      },
      scales: {
        x: { ticks: { color: C.axis, maxRotation: 0, autoSkip: false, font: { size: 10 } }, grid: { color: C.border } },
        y:  { position: 'left',  beginAtZero: true, ticks: { color: C.axis, callback: v => v.toFixed(1) },     grid: { color: C.border }, title: { display: true, text: t('axis_avg_turns'),  color: C.axis, font: { size: 11 } } },
        y1: { position: 'right', beginAtZero: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { drawOnChartArea: false },   title: { display: true, text: t('axis_avg_output'), color: C.axis, font: { size: 11 } } },
      }
    }
  });
}

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { seriesKey: 'input',          label: t('series_input'),          hidden: hiddenSeries.daily.has('input'),          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          hoverBackgroundColor: TOKEN_HOVER.input,          stack: 'io',    yAxisID: 'y1' },
        { seriesKey: 'output',         label: t('series_output'),         hidden: hiddenSeries.daily.has('output'),         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         hoverBackgroundColor: TOKEN_HOVER.output,         stack: 'io',    yAxisID: 'y1' },
        { seriesKey: 'cache_read',     label: t('series_cache_read'),     hidden: hiddenSeries.daily.has('cache_read'),     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     hoverBackgroundColor: TOKEN_HOVER.cache_read,     stack: 'cache', yAxisID: 'y' },
        { seriesKey: 'cache_creation', label: t('series_cache_creation'), hidden: hiddenSeries.daily.has('cache_creation'), data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, hoverBackgroundColor: TOKEN_HOVER.cache_creation, stack: 'cache', yAxisID: 'y' },
        { type: 'line', seriesKey: 'cost', label: t('series_est_cost'), hidden: hiddenSeries.daily.has('cost'), data: daily.map(d => d.cost), borderColor: C.accent, backgroundColor: 'transparent', pointBackgroundColor: C.accent, pointRadius: 3, tension: 0.3, yAxisID: 'y2' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: { onClick: legendToggle('daily'), labels: { color: C.axis, boxWidth: 12 } },
        tooltip: { callbacks: {
          label: item => item.dataset.seriesKey === 'cost'
            ? t('tt_est_cost', { c: fmtCost(item.raw) })
            : t('tt_series', { label: item.dataset.label, v: fmt(item.raw) })
        }}
      },
      scales: {
        x:  { ticks: { color: C.axis, maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: C.border } },
        y:  { position: 'left',  ticks: { color: C.green,  callback: v => fmt(v) },         grid: { color: C.border },          title: { display: true, text: t('axis_cache'), color: C.green } },
        y1: { position: 'right', ticks: { color: C.blue,   callback: v => fmt(v) },         grid: { drawOnChartArea: false },    title: { display: true, text: t('axis_io'),    color: C.blue } },
        y2: { position: 'right', ticks: { color: C.accent, callback: v => '$' + v.toFixed(2) }, grid: { drawOnChartArea: false }, title: { display: true, text: t('series_est_cost'), color: C.accent }, offset: true },
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
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, hoverBackgroundColor: MODEL_COLORS, hoverOffset: 8, borderWidth: 2, borderColor: C.card, hoverBorderColor: C.card }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: C.axis, boxWidth: 12, font: { size: 11 } },
          onClick: (e, item, legend) => {
            const ci = legend.chart;
            ci.toggleDataVisibility(item.index);
            const label = ci.data.labels[item.index];
            if (!ci.getDataVisibility(item.index)) hiddenSeries.model.add(label); else hiddenSeries.model.delete(label);
            ci.update();
          },
        },
        tooltip: { callbacks: { label: ctx => t('tt_tokens', { label: ctx.label, v: fmt(ctx.raw) }) } }
      }
    }
  });
  // Reapply any slices the user toggled off in a previous render.
  byModel.forEach((m, i) => {
    if (hiddenSeries.model.has(m.model) && charts.model.getDataVisibility(i)) charts.model.toggleDataVisibility(i);
  });
  charts.model.update();
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
        { seriesKey: 'input',  label: t('series_input'),  hidden: hiddenSeries.project.has('input'),  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input,  hoverBackgroundColor: TOKEN_HOVER.input },
        { seriesKey: 'output', label: t('series_output'), hidden: hiddenSeries.project.has('output'), data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output, hoverBackgroundColor: TOKEN_HOVER.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: { legend: { onClick: legendToggle('project'), labels: { color: C.axis, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      }
    }
  });
}

function renderSubagentChart(byType) {
  const ctx = document.getElementById('chart-subagent').getContext('2d');
  if (charts.subagent) charts.subagent.destroy();
  if (!byType.length) { charts.subagent = null; return; }
  charts.subagent = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: byType.map(t => t.agent_type),
      datasets: [
        { seriesKey: 'input',          label: t('series_input'),          hidden: hiddenSeries.subagent.has('input'),          data: byType.map(t => t.input),          backgroundColor: TOKEN_COLORS.input,          hoverBackgroundColor: TOKEN_HOVER.input,          stack: 'tokens' },
        { seriesKey: 'output',         label: t('series_output'),         hidden: hiddenSeries.subagent.has('output'),         data: byType.map(t => t.output),         backgroundColor: TOKEN_COLORS.output,         hoverBackgroundColor: TOKEN_HOVER.output,         stack: 'tokens' },
        { seriesKey: 'cache_read',     label: t('series_cache_read'),     hidden: hiddenSeries.subagent.has('cache_read'),     data: byType.map(t => t.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     hoverBackgroundColor: TOKEN_HOVER.cache_read,     stack: 'tokens' },
        { seriesKey: 'cache_creation', label: t('series_cache_creation'), hidden: hiddenSeries.subagent.has('cache_creation'), data: byType.map(t => t.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, hoverBackgroundColor: TOKEN_HOVER.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: { onClick: legendToggle('subagent'), labels: { color: C.axis, boxWidth: 12 } },
        tooltip: { callbacks: {
          label: ctx => t('tt_series', { label: ctx.dataset.label, v: fmt(ctx.raw) }),
          footer: items => {
            const total = items.reduce((s, it) => s + it.raw, 0);
            const row = byType[items[0].dataIndex];
            return t('tt_total', { v: fmt(total), turns: row.turns });
          }
        } }
      },
      scales: {
        x: { stacked: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { stacked: true, ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      }
    }
  });
}

function renderTopDispatches(rows) {
  const body = document.getElementById('dispatches-body');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="11" class="muted" style="text-align:center;padding:24px">' + esc(t('no_dispatches')) + '</td></tr>';
    renderTableToggle('dispatches-foot', 0, dispatchesLimit, 'lessDispatchRows', 'moreDispatchRows', 'exportDispatchesCSV');
    return;
  }
  const shown = rows.slice(0, shownCount(dispatchesLimit, rows.length));
  body.innerHTML = shown.map(d => {
    const tokensTotal = d.input + d.output + d.cache_read + d.cache_creation;
    const cost = calcCost(d.model, d.input, d.output, d.cache_read, d.cache_creation);
    const costCell = isBillable(d.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const col = colorForAgentType(d.agent_type);
    const typeStyle = `background:${col}22;color:${col};border:1px solid ${col}44`;
    return `<tr>
      <td><span class="model-tag" style="${typeStyle}">${esc(d.agent_type)}</span></td>
      <td class="muted">${esc(d.start || '—')}</td>
      <td><span class="model-tag">${esc(d.model)}</span></td>
      <td class="num">${d.turns}</td>
      <td class="num">${d.tool_uses != null ? d.tool_uses : '—'}</td>
      <td class="muted">${fmtDuration(d.duration_ms)}</td>
      <td class="num">${fmt(d.input)}</td>
      <td class="num">${fmt(d.output)}</td>
      <td class="num">${fmt(d.cache_read)}</td>
      <td class="num"><strong>${fmt(tokensTotal)}</strong></td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('dispatches-foot', rows.length, dispatchesLimit, 'lessDispatchRows', 'moreDispatchRows', 'exportDispatchesCSV');
}

// Fills a table card's footer with the row-reveal control. Three states:
//   - more rows fit under the cap        -> "Show more" (plus "Show less" once expanded)
//   - cap reached but more records exist -> "Download CSV to see all (N)" + "Show less"
//   - every row is already visible       -> "Show less"
// "Show less" is hidden at the initial step (nothing to collapse yet). Renders
// nothing when the whole table fits in the first step. Carets: more = down (▾),
// less = up (▴).
function renderTableToggle(footId, total, limit, lessName, moreName, csvName) {
  const foot = document.getElementById(footId);
  if (!foot) return;
  if (total <= PAGINATE_THRESHOLD) { foot.innerHTML = ''; return; }
  const less = '<button class="show-more-btn" onclick="' + lessName + '()">' + esc(t('show_less')) + '</button>';
  const more = '<button class="show-more-btn" onclick="' + moreName + '()">' + esc(t('show_more')) + '</button>';
  let html;
  if (limit < total && limit < TABLE_MAX) {
    // more rows fit under the cap; Show less only once we're past the first step
    html = (limit > TABLE_STEPS[0] ? less : '') + more;
  } else if (limit < total) {           // cap reached, remaining rows only via CSV
    html = '<a class="show-more-link" href="#" onclick="' + csvName + '(); return false;">' + esc(t('csv_download_all', { n: total })) + '</a>' + less;
  } else {                              // everything already visible
    html = less;
  }
  foot.innerHTML = html;
}

// After collapsing a table, bring its top back into view — the user may have
// scrolled down through the expanded rows.
function scrollTableToTop(bodyId) {
  const card = document.getElementById(bodyId)?.closest('.table-card');
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// "Show more" advances one step (capped at TABLE_MAX); "Show less" resets to the
// first step and scrolls back to the top of that table.
function moreModelRows()   { modelLimit    = nextTableLimit(modelLimit,    lastByModel.length);        renderModelCostTable(lastByModel); }
function lessModelRows()   { modelLimit    = TABLE_STEPS[0]; renderModelCostTable(lastByModel);            scrollTableToTop('model-cost-body'); }
function moreSessionRows() { sessionsLimit = nextTableLimit(sessionsLimit, lastFilteredSessions.length); renderSessionsTable(lastFilteredSessions); }
function lessSessionRows() { sessionsLimit = TABLE_STEPS[0]; renderSessionsTable(lastFilteredSessions);    scrollTableToTop('sessions-body'); }
function moreProjectRows() { projectLimit  = nextTableLimit(projectLimit,  lastByProject.length);       renderProjectCostTable(lastByProject); }
function lessProjectRows() { projectLimit  = TABLE_STEPS[0]; renderProjectCostTable(lastByProject);        scrollTableToTop('project-cost-body'); }
function moreBranchRows()  { branchLimit   = nextTableLimit(branchLimit,   lastByProjectBranch.length); renderProjectBranchCostTable(lastByProjectBranch); }
function lessBranchRows()  { branchLimit   = TABLE_STEPS[0]; renderProjectBranchCostTable(lastByProjectBranch); scrollTableToTop('project-branch-cost-body'); }
function moreDispatchRows(){ dispatchesLimit = nextTableLimit(dispatchesLimit, lastFilteredDispatches.length); renderTopDispatches(lastFilteredDispatches); }
function lessDispatchRows(){ dispatchesLimit = TABLE_STEPS[0]; renderTopDispatches(lastFilteredDispatches);            scrollTableToTop('dispatches-body'); }

function renderSessionsTable(sessions) {
  const shown = sessions.slice(0, shownCount(sessionsLimit, sessions.length));
  document.getElementById('sessions-body').innerHTML = shown.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const titleCell = s.topic
      ? `<td class="topic-cell" title="${esc(s.topic)}">${esc(s.topic)}</td>`
      : `<td class="topic-cell"><span class="untitled">${esc(t('untitled'))}</span></td>`;
    return `<tr>
      <td class="muted" style="font-family:monospace">${esc(s.session_id.slice(0, 8))}&hellip;</td>
      <td>${esc(s.project)}</td>
      ${titleCell}
      <td class="muted">${esc(s.last)}</td>
      <td class="muted">${esc(s.duration_min)}${esc(t('duration_suffix'))}</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('sessions-foot', sessions.length, sessionsLimit, 'lessSessionRows', 'moreSessionRows', 'exportSessionsCSV');
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
  const sorted = sortModels(byModel);
  const shown = sorted.slice(0, shownCount(modelLimit, sorted.length));
  document.getElementById('model-cost-body').innerHTML = shown.map(m => {
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
  renderTableToggle('model-cost-foot', sorted.length, modelLimit, 'lessModelRows', 'moreModelRows', 'exportModelCSV');
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
  const sorted = sortProjects(byProject);
  const shown = sorted.slice(0, shownCount(projectLimit, sorted.length));
  document.getElementById('project-cost-body').innerHTML = shown.map(p => {
    return `<tr>
      <td>${esc(p.project)}</td>
      <td class="num">${p.sessions}</td>
      <td class="num">${fmt(p.turns)}</td>
      <td class="num">${fmt(p.input)}</td>
      <td class="num">${fmt(p.output)}</td>
      <td class="cost">${fmtCost(p.cost)}</td>
    </tr>`;
  }).join('');
  renderTableToggle('project-cost-foot', sorted.length, projectLimit, 'lessProjectRows', 'moreProjectRows', 'exportProjectsCSV');
}

// ── Project+Branch cost table sorting ────────────────────────────────────
function setProjectBranchSort(col) {
  if (branchSortCol === col) {
    branchSortDir = branchSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    branchSortCol = col;
    branchSortDir = 'desc';
  }
  updateProjectBranchSortIcons();
  applyFilter();
}

function updateProjectBranchSortIcons() {
  document.querySelectorAll('[id^="pbsort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('pbsort-' + branchSortCol);
  if (icon) icon.textContent = branchSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjectBranch(rows) {
  // Sort by the selected column (default: cost desc), consistent with the Cost by
  // Model / Cost by Project tables. Project name is only a stable tiebreaker when
  // the sorted column ties, so a project's branches stay grouped & deterministic
  // without overriding the primary order.
  return [...rows].sort((a, b) => {
    const av = a[branchSortCol] ?? 0;
    const bv = b[branchSortCol] ?? 0;
    if (av < bv) return branchSortDir === 'desc' ? 1 : -1;
    if (av > bv) return branchSortDir === 'desc' ? -1 : 1;
    const pa = (a.project || '').toLowerCase();
    const pb = (b.project || '').toLowerCase();
    return pa < pb ? -1 : pa > pb ? 1 : 0;
  });
}

function renderProjectBranchCostTable(rows) {
  const sorted = sortProjectBranch(rows);
  const shown = sorted.slice(0, shownCount(branchLimit, sorted.length));
  document.getElementById('project-branch-cost-body').innerHTML = shown.map(pb => {
    return `<tr>
      <td>${esc(pb.project)}</td>
      <td class="muted" style="font-family:monospace">${esc(pb.branch || '\u2014')}</td>
      <td class="num">${pb.sessions}</td>
      <td class="num">${fmt(pb.turns)}</td>
      <td class="num">${fmt(pb.input)}</td>
      <td class="num">${fmt(pb.output)}</td>
      <td class="cost">${fmtCost(pb.cost)}</td>
    </tr>`;
  }).join('');
  renderTableToggle('project-branch-cost-foot', sorted.length, branchLimit, 'lessBranchRows', 'moreBranchRows', 'exportProjectBranchCSV');
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

function exportModelCSV() {
  const header = ['Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = sortModels(lastByModel).map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    return [m.model, m.turns, m.input, m.output, m.cache_read, m.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('cost_by_model', header, rows);
}

function exportSessionsCSV() {
  const header = ['Session', 'Project', 'Title', 'Last Active', 'Duration (min)', 'Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastFilteredSessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    return [s.session_id, s.project, s.topic, s.last, s.duration_min, s.model, s.turns, s.input, s.output, s.cache_read, s.cache_creation, cost.toFixed(4)];
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

function exportProjectBranchCSV() {
  const header = ['Project', 'Branch', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProjectBranch.map(pb => {
    return [pb.project, pb.branch, pb.sessions, pb.turns, pb.input, pb.output, pb.cache_read, pb.cache_creation, pb.cost.toFixed(4)];
  });
  downloadCSV('projects_by_branch', header, rows);
}

function exportDispatchesCSV() {
  const header = ['Type', 'Agent ID', 'Started', 'Model', 'Turns', 'Tool Uses', 'Duration (ms)', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Total Tokens', 'Est. Cost', 'Status'];
  const rows = lastFilteredDispatches.map(d => {
    const total = d.input + d.output + d.cache_read + d.cache_creation;
    const cost = calcCost(d.model, d.input, d.output, d.cache_read, d.cache_creation);
    return [d.agent_type, d.agent_id, d.start, d.model, d.turns,
            d.tool_uses != null ? d.tool_uses : '', d.duration_ms != null ? d.duration_ms : '',
            d.input, d.output, d.cache_read, d.cache_creation, total, cost.toFixed(4), d.status || ''];
  });
  downloadCSV('subagent_dispatches', header, rows);
}

// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  const btn = document.getElementById('rescan-btn');
  btn.disabled = true;
  btn.textContent = t('rescan_scanning');
  try {
    const resp = await fetch('/api/rescan', { method: 'POST' });
    const d = await resp.json();
    btn.textContent = t('rescan_result', { new: d.new, updated: d.updated });
    await loadData();
  } catch(e) {
    btn.textContent = t('rescan_error');
    console.error(e);
  }
  setTimeout(() => { btn.textContent = t('rescan'); btn.disabled = false; }, 3000);
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      // The server binds and serves before the initial scan finishes, so on a
      // fresh start the DB may not exist yet. Show a non-destructive notice and
      // retry instead of nuking the page — once the background scan creates the
      // DB, the next poll renders normally.
      const meta = document.getElementById('meta');
      if (meta) meta.innerHTML = t('meta_retrying', { e: esc(d.error) });
      if (rawData === null) setTimeout(loadData, 3000);
      return;
    }
    const refreshNote = rangeIncludesToday(selectedRange) ? '<br>' + esc(t('meta_autorefresh')) : '';
    document.getElementById('meta').innerHTML = t('meta_updated', { t: esc(d.generated_at) }) + refreshNote;

    const isFirstLoad = rawData === null;
    rawData = d;

    if (isFirstLoad) {
      // Restore range from URL into the dropdown
      selectedRange = readURLRange();
      const rangeSel = document.getElementById('range-select');
      if (rangeSel) rangeSel.value = selectedRange;
      // Mark default TZ button active
      document.querySelectorAll('.tz-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.tz === hourlyTZ)
      );
      // Build model filter (reads URL for model selection too)
      buildFilterUI(d.all_models);
      updateSortIcons();
      updateModelSortIcons();
      updateProjectSortIcons();
      updateProjectBranchSortIcons();
    }

    applyFilter();
  } catch(e) {
    console.error(e);
  }
}

let autoRefreshTimer = null;
function scheduleAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  if (rangeIncludesToday(selectedRange)) {
    autoRefreshTimer = setInterval(loadData, 30000);
  }
}

// ── Footer meta: version, extension promo, update check ──────────────────────
// APP_CONFIG is injected server-side (see do_GET). { version, surface }.
const APP_CONFIG = window.APP_CONFIG || { version: '', surface: 'web' };
const REPO_URL = 'https://github.com/phuryn/claude-usage';
const MARKETPLACE_URL = 'https://marketplace.visualstudio.com/items?itemName=PawelHuryn.claude-usage-phuryn';
const UPDATE_CACHE_KEY = 'cu_update_check';
const UPDATE_CACHE_TTL = 24 * 60 * 60 * 1000;  // re-check GitHub at most once a day

// Compare dotted numeric versions ("1.3.0"); leading "v" tolerated. Returns
// true only when `latest` is strictly ahead of `current`.
function isNewer(latest, current) {
  const a = String(latest).replace(/^v/, '').split('.').map(n => parseInt(n, 10) || 0);
  const b = String(current).replace(/^v/, '').split('.').map(n => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] || 0, y = b[i] || 0;
    if (x > y) return true;
    if (x < y) return false;
  }
  return false;
}

function appendUpdateLink(latest) {
  const el = document.getElementById('footer-meta');
  if (!el || !el.innerHTML) return;
  const a = document.createElement('a');
  a.className = 'update-link';
  a.href = REPO_URL + '/releases/latest';
  a.target = '_blank';
  a.rel = 'noopener';
  a.textContent = t('footer_update', { v: latest });
  el.insertAdjacentHTML('beforeend', '&nbsp;&middot;&nbsp;');
  el.appendChild(a);
}

// Web only. Asks GitHub's public releases API whether a newer release exists and,
// if so, appends an "Update to vX.Y.Z" link. Cached in localStorage for 24h and
// fully fail-silent (offline / rate-limited / blocked -> no link, no error). No
// usage data is sent; this is a plain unauthenticated GET of release metadata.
function checkForUpdate(current) {
  let cached = null;
  try { cached = JSON.parse(localStorage.getItem(UPDATE_CACHE_KEY) || 'null'); } catch (e) {}
  if (cached && cached.latest && cached.ts && (Date.now() - cached.ts) < UPDATE_CACHE_TTL) {
    if (isNewer(cached.latest, current)) appendUpdateLink(cached.latest);
    return;
  }
  fetch('https://api.github.com/repos/phuryn/claude-usage/releases/latest', {
    headers: { 'Accept': 'application/vnd.github+json' }
  })
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data || !data.tag_name) return;
      const latest = String(data.tag_name).replace(/^v/, '');
      try { localStorage.setItem(UPDATE_CACHE_KEY, JSON.stringify({ ts: Date.now(), latest: latest })); } catch (e) {}
      if (isNewer(latest, current)) appendUpdateLink(latest);
    })
    .catch(() => {});  // fail-silent: never let a version check disrupt the dashboard
}

function initFooterMeta() {
  const el = document.getElementById('footer-meta');
  if (!el) return;
  const v = APP_CONFIG.version || '';
  const parts = [];
  if (v) {
    parts.push(esc(t('footer_version')) + ' <a href="' + REPO_URL + '/releases/tag/v' + esc(v) + '" target="_blank" rel="noopener">v' + esc(v) + '</a>');
  }
  // The web build promotes the extension; the embedded build is already in it.
  if (APP_CONFIG.surface !== 'vscode') {
    parts.push('<a href="' + MARKETPLACE_URL + '" target="_blank" rel="noopener">' + esc(t('footer_get_ext')) + '</a>');
  }
  el.innerHTML = parts.join('&nbsp;&middot;&nbsp;');
  // VS Code auto-updates the extension, so only the web build checks for updates.
  if (v && APP_CONFIG.surface !== 'vscode') checkForUpdate(v);
}

// ── Section nav + collapsible cards ─────────────────────────────────────────
// The dashboard is one long scroll. The sticky jump bar teleports between
// sections; collapsible cards fold away the ones you don't use. Collapse state
// persists per card in localStorage and is independent of in-table Show
// more/less (which only pages rows within a single table).
const COLLAPSE_KEY = 'cu_collapsed_cards';
const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function loadCollapsedSet() {
  try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveCollapsedSet(set) {
  try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...set])); } catch (e) {}
}

// Charts created while their card is collapsed (display:none) lay out at zero
// size; resize them once the card is shown again so Chart.js repaints to fit.
function resizeChartsIn(card) {
  card.querySelectorAll('canvas').forEach(cv => {
    const ch = Object.values(charts).find(c => c && c.canvas === cv);
    if (ch) ch.resize();
  });
}

function setCardCollapsed(card, collapsed) {
  card.classList.toggle('collapsed', collapsed);
  const title = card.querySelector('h2, .section-title');
  if (title) title.setAttribute('aria-expanded', String(!collapsed));
}

function toggleCard(card) {
  const collapsed = !card.classList.contains('collapsed');
  setCardCollapsed(card, collapsed);
  const set = loadCollapsedSet();
  if (collapsed) set.add(card.dataset.card); else set.delete(card.dataset.card);
  saveCollapsedSet(set);
  if (!collapsed) requestAnimationFrame(() => resizeChartsIn(card));
}

function jumpToSection(id) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.dataset.card && el.classList.contains('collapsed')) toggleCard(el);  // expand before scrolling
  el.scrollIntoView({ behavior: prefersReducedMotion ? 'auto' : 'smooth', block: 'start' });
}

function initSectionNav() {
  const bar = document.getElementById('jump-bar');
  const container = document.querySelector('.container');
  if (!container) return;

  // Keep --jump-h synced to the bar's real height so scroll-margin clears it
  // even when the links wrap to a second row on a narrow panel.
  const syncJumpHeight = () => {
    if (bar) document.documentElement.style.setProperty('--jump-h', bar.offsetHeight + 'px');
  };
  syncJumpHeight();
  window.addEventListener('resize', syncJumpHeight);

  // Restore persisted collapse state + make each title an accessible toggle.
  const collapsed = loadCollapsedSet();
  document.querySelectorAll('[data-card]').forEach(card => {
    const title = card.querySelector('h2, .section-title');
    if (title) {
      title.setAttribute('role', 'button');
      title.setAttribute('tabindex', '0');
      title.title = t('collapse_title');
    }
    setCardCollapsed(card, collapsed.has(card.dataset.card));
  });

  // Toggle a card from its title (caret included). Inner controls (CSV, TZ, sort
  // headers) sit outside the title selector, so they keep their own behaviour.
  const TITLE_SEL = '.chart-card > h2, .chart-header > h2, .table-card > .section-title, .section-header > .section-title';
  const onTitleActivate = (e) => {
    if (e.target.closest('.info-icon')) return;  // info tooltip, not a collapse toggle
    if (e.type === 'keydown') { if (e.key !== 'Enter' && e.key !== ' ') return; e.preventDefault(); }
    const title = e.target.closest(TITLE_SEL);
    const card = title && title.closest('[data-card]');
    if (card) toggleCard(card);
  };
  container.addEventListener('click', onTitleActivate);
  container.addEventListener('keydown', onTitleActivate);

  // Jump links teleport to a section (expanding it first if collapsed). Blur the
  // clicked item so the hover/focus dropdown it lives in closes after the jump.
  if (bar) bar.addEventListener('click', (e) => {
    const link = e.target.closest('.jump-link');
    if (link) { jumpToSection(link.dataset.target); link.blur(); }
  });

  // Mirror open/closed state on the menu triggers for assistive tech, and let
  // Escape close an open menu.
  document.querySelectorAll('.jump-menu').forEach(menu => {
    const trig = menu.querySelector('.jump-trigger');
    const sync = (open) => { if (trig) trig.setAttribute('aria-expanded', String(open)); };
    // A mouse click must not focus (and thus pin) the trigger — otherwise the
    // panel stays open after the pointer leaves and fights the next hover. Tab
    // focus still works (it doesn't go through mousedown), keeping it keyboard-open.
    if (trig) trig.addEventListener('mousedown', (e) => e.preventDefault());
    menu.addEventListener('mouseenter', () => sync(true));
    menu.addEventListener('mouseleave', () => sync(false));
    menu.addEventListener('focusin', () => sync(true));
    menu.addEventListener('focusout', () => sync(false));
    menu.addEventListener('keydown', (e) => { if (e.key === 'Escape' && document.activeElement) document.activeElement.blur(); });
  });

  // Scroll-spy: highlight the link for the topmost section under the bar, and
  // mark the parent Graphs/Tables trigger so the closed menu shows where you are.
  const links = [...document.querySelectorAll('.jump-link')];
  const menus = [...document.querySelectorAll('.jump-menu')];
  const targets = links.map(l => document.getElementById(l.dataset.target)).filter(Boolean)
    .sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
  let spyScheduled = false;
  const updateActive = () => {
    spyScheduled = false;
    const line = (bar ? bar.offsetHeight : 45) + 16;
    let activeId = targets.length ? targets[0].id : null;
    for (const t of targets) {
      if (t.getBoundingClientRect().top - line <= 1) activeId = t.id; else break;
    }
    // At the very bottom the last (often short) section may never reach the line.
    if (targets.length && (window.innerHeight + window.scrollY) >= document.body.scrollHeight - 4)
      activeId = targets[targets.length - 1].id;
    links.forEach(l => l.classList.toggle('active', l.dataset.target === activeId));
    menus.forEach(menu => {
      const trig = menu.querySelector('.jump-trigger');
      if (trig) trig.classList.toggle('active', !!menu.querySelector('.jump-link.active'));
    });
  };
  window.addEventListener('scroll', () => {
    if (!spyScheduled) { spyScheduled = true; requestAnimationFrame(updateActive); }
  }, { passive: true });
  updateActive();
}

initSectionNav();
applyLang();  // translate static chrome + footer for the persisted/detected language (also runs initFooterMeta)
loadData();
scheduleAutoRefresh();
</script>
</body>
</html>
"""


def find_icon_file():
    """Locate the extension's icon.svg across both run contexts.

    - Bundled in the .vsix: this file lives at ``python/dashboard.py`` and the
      icon is a sibling-of-parent at ``../resources/icon.svg``.
    - Standalone repo (``python cli.py dashboard``): this file is the repo-root
      ``dashboard.py`` and the icon is at ``vscode-extension/resources/icon.svg``.

    Returns the first existing path, or ``None`` so the /icon.svg route can 404
    gracefully (the header ``<img>`` then just renders empty alt text).
    """
    here = Path(__file__).resolve().parent
    for candidate in (
        here.parent / "resources" / "icon.svg",
        here / "vscode-extension" / "resources" / "icon.svg",
    ):
        if candidate.is_file():
            return candidate
    return None


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        # self.path includes the query string, but every URL the UI emits has
        # one (e.g. "/?range=all"); compare the bare path so bookmarkable
        # URLs don't fall through to 404.
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            # Inject runtime config (version + surface) the page can't know at
            # author time. json.dumps produces a valid JS object literal for the
            # `window.APP_CONFIG = __APP_CONFIG_JSON__;` placeholder in the head.
            config = json.dumps({"version": VERSION, "surface": SURFACE})
            html = HTML_TEMPLATE.replace("__APP_CONFIG_JSON__", config)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/data":
            # Pass DB_PATH explicitly: get_dashboard_data's default arg is frozen
            # to the original module global at def time, so a bare call would ignore
            # a monkey-patched dashboard.DB_PATH (same contract as /api/rescan). This
            # also keeps the dashboard reading the configured DB rather than a stale
            # path captured at import.
            data = get_dashboard_data(DB_PATH)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/icon.svg":
            icon = find_icon_file()
            if icon is None:
                self.send_response(404)
                self.end_headers()
                return
            body = icon.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/rescan":
            # Incremental scan: ingest new/changed JSONL without touching
            # existing rows. The DB is append-only and the only durable store
            # of history once Claude Code prunes old transcripts, so we must
            # never delete it here — scan() dedupes via the message_id index.
            # Pass DB_PATH / DEFAULT_PROJECTS_DIRS explicitly so tests that
            # patch the module globals are honored (scan's defaults are
            # frozen at def time and would otherwise target the real paths).
            import scanner
            db_path = DB_PATH
            result = scanner.scan(
                db_path=db_path,
                projects_dirs=scanner.DEFAULT_PROJECTS_DIRS,
                verbose=False,
            )
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def serve(host=None, port=None, surface=None):
    global SURFACE
    if surface:
        SURFACE = surface
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
