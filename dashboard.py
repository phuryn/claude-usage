"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import os
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

DB_PATH = Path.home() / ".claude" / "usage.db"


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── All models (for filter UI) ────────────────────────────────────────────
    model_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model
        FROM turns
        GROUP BY model
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns
        FROM turns
        GROUP BY day, model
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
            COALESCE(model, 'unknown')                as model,
            SUM(output_tokens)                        as output,
            COUNT(*)                                  as turns
        FROM turns
        WHERE timestamp IS NOT NULL AND length(timestamp) >= 13
        GROUP BY day, hour, model
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
            git_branch
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
            "session_id":    r["session_id"][:8],
            "project":       r["project_name"] or "unknown",
            "branch":        r["git_branch"] or "",
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

    conn.close()

    return {
        "all_models":      all_models,
        "daily_by_model":  daily_by_model,
        "hourly_by_model": hourly_by_model,
        "sessions_all":    sessions_all,
        "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--accent); }
  header .meta { color: var(--muted); font-size: 12px; }
  .header-actions { display: flex; align-items: center; gap: 8px; }
  #rescan-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; margin-top: 4px; }
  #rescan-btn:hover { color: var(--text); border-color: var(--accent); }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  .lang-picker { position: relative; margin-top: 4px; }
  #lang-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; display: inline-flex; align-items: center; gap: 6px; }
  #lang-btn:hover { color: var(--text); border-color: var(--accent); }
  #lang-btn .caret { font-size: 9px; opacity: 0.7; }
  .lang-menu { position: absolute; top: calc(100% + 6px); right: 0; min-width: 160px; background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 6px; box-shadow: 0 8px 24px rgba(0,0,0,0.4); z-index: 50; display: none; }
  .lang-menu.open { display: block; }
  .lang-menu .lang-menu-title { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); padding: 4px 10px 6px; }
  .lang-menu button { display: flex; align-items: center; justify-content: space-between; width: 100%; background: transparent; border: none; color: var(--text); padding: 6px 10px; border-radius: 4px; cursor: pointer; font-size: 13px; text-align: left; }
  .lang-menu button:hover { background: rgba(255,255,255,0.04); }
  .lang-menu button.active { color: var(--accent); }
  .lang-menu button .check { color: var(--accent); opacity: 0; font-size: 12px; }
  .lang-menu button.active .check { opacity: 1; }

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

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
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
  .tz-btn:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .tz-btn.active { background: rgba(217,119,87,0.15); color: var(--accent); }
  .peak-legend { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
  .peak-swatch { width: 10px; height: 10px; background: rgba(248,113,113,0.8); border-radius: 2px; display: inline-block; }

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
</style>
</head>
<body>
<header>
  <h1 data-i18n="header.title">Claude Code Usage Dashboard</h1>
  <div class="meta" id="meta" data-i18n="header.meta_loading">Loading...</div>
  <div class="header-actions">
    <div class="lang-picker">
      <button id="lang-btn" type="button" onclick="toggleLangMenu(event)" data-i18n-title="lang_picker.button_tooltip" aria-haspopup="true" aria-expanded="false">
        <span aria-hidden="true">&#x1F310;</span>
        <span id="lang-btn-label">English</span>
        <span class="caret" aria-hidden="true">&#x25BE;</span>
      </button>
      <div class="lang-menu" id="lang-menu" role="menu" aria-labelledby="lang-btn">
        <div class="lang-menu-title" data-i18n="lang_picker.title">Language</div>
        <div id="lang-menu-items"></div>
      </div>
    </div>
    <button id="rescan-btn" onclick="triggerRescan()" data-i18n="header.rescan" data-i18n-title="header.rescan_tooltip" title="Rebuild the database from scratch by re-scanning all JSONL files. Use if data looks stale or costs seem wrong.">&#x21bb; Rescan</button>
  </div>
</header>

<div id="filter-bar">
  <div class="filter-label" data-i18n="filter.models" data-i18n-title="filter.models_tooltip">Models</div>
  <div id="model-checkboxes"></div>
  <button class="filter-btn" onclick="selectAllModels()" data-i18n="filter.all" data-i18n-title="filter.all_tooltip">All</button>
  <button class="filter-btn" onclick="clearAllModels()" data-i18n="filter.none" data-i18n-title="filter.none_tooltip">None</button>
  <div class="filter-sep"></div>
  <div class="filter-label" data-i18n="filter.range" data-i18n-title="filter.range_tooltip">Range</div>
  <div class="range-group">
    <button class="range-btn" data-range="week" onclick="setRange('week')" data-i18n="range.week" data-i18n-title="range.week_tooltip">This Week</button>
    <button class="range-btn" data-range="month" onclick="setRange('month')" data-i18n="range.month" data-i18n-title="range.month_tooltip">This Month</button>
    <button class="range-btn" data-range="prev-month" onclick="setRange('prev-month')" data-i18n="range.prev_month" data-i18n-title="range.prev_month_tooltip">Prev Month</button>
    <button class="range-btn" data-range="7d"  onclick="setRange('7d')" data-i18n="range.7d" data-i18n-title="range.7d_tooltip">7d</button>
    <button class="range-btn" data-range="30d" onclick="setRange('30d')" data-i18n="range.30d" data-i18n-title="range.30d_tooltip">30d</button>
    <button class="range-btn" data-range="90d" onclick="setRange('90d')" data-i18n="range.90d" data-i18n-title="range.90d_tooltip">90d</button>
    <button class="range-btn" data-range="all" onclick="setRange('all')" data-i18n="range.all" data-i18n-title="range.all_tooltip">All</button>
  </div>
</div>

<div class="container">
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide">
      <h2 id="daily-chart-title" data-i18n="chart.daily_title" data-i18n-title="chart.daily_title_tooltip">Daily Token Usage</h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card wide">
      <div class="chart-header">
        <h2 id="hourly-chart-title" data-i18n="chart.hourly_title" data-i18n-title="chart.hourly_title_tooltip">Average Hourly Distribution</h2>
        <div class="chart-header-right">
          <span class="peak-legend" data-i18n-title="chart.peak_legend_tooltip" title="Mon–Fri 05:00–11:00 PT — Anthropic peak-hour throttling window"><span class="peak-swatch"></span><span data-i18n="chart.peak_legend">Peak hours (PT)</span></span>
          <span class="chart-day-count" id="hourly-day-count"></span>
          <div class="tz-group">
            <button class="tz-btn" data-tz="local" onclick="setHourlyTZ('local')" data-i18n="chart.tz_local">Local</button>
            <button class="tz-btn" data-tz="utc"   onclick="setHourlyTZ('utc')" data-i18n="chart.tz_utc">UTC</button>
          </div>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="chart-hourly"></canvas></div>
    </div>
    <div class="chart-card">
      <h2 data-i18n="chart.model_title" data-i18n-title="chart.model_title_tooltip">By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card">
      <h2 data-i18n="chart.project_title" data-i18n-title="chart.project_title_tooltip">Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
  </div>
  <div class="table-card">
    <div class="section-title" data-i18n="table.cost_by_model">Cost by Model</div>
    <table>
      <thead><tr>
        <th><span data-i18n="th.model">Model</span></th>
        <th class="sortable" onclick="setModelSort('turns')"><span data-i18n="th.turns">Turns</span> <span class="sort-icon" id="msort-turns"></span></th>
        <th class="sortable" onclick="setModelSort('input')"><span data-i18n="th.input">Input</span> <span class="sort-icon" id="msort-input"></span></th>
        <th class="sortable" onclick="setModelSort('output')"><span data-i18n="th.output">Output</span> <span class="sort-icon" id="msort-output"></span></th>
        <th class="sortable" onclick="setModelSort('cache_read')"><span data-i18n="th.cache_read">Cache Read</span> <span class="sort-icon" id="msort-cache_read"></span></th>
        <th class="sortable" onclick="setModelSort('cache_creation')"><span data-i18n="th.cache_creation">Cache Creation</span> <span class="sort-icon" id="msort-cache_creation"></span></th>
        <th class="sortable" onclick="setModelSort('cost')"><span data-i18n="th.est_cost">Est. Cost</span> <span class="sort-icon" id="msort-cost"></span></th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title" data-i18n="table.recent_sessions">Recent Sessions</div><button class="export-btn" onclick="exportSessionsCSV()" data-i18n="table.csv_export" data-i18n-title="table.csv_export_sessions_tooltip" title="Export all filtered sessions to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th><span data-i18n="th.session">Session</span></th>
        <th><span data-i18n="th.project">Project</span></th>
        <th class="sortable" onclick="setSessionSort('last')"><span data-i18n="th.last_active">Last Active</span> <span class="sort-icon" id="sort-icon-last"></span></th>
        <th class="sortable" onclick="setSessionSort('duration_min')"><span data-i18n="th.duration">Duration</span> <span class="sort-icon" id="sort-icon-duration_min"></span></th>
        <th><span data-i18n="th.model">Model</span></th>
        <th class="sortable" onclick="setSessionSort('turns')"><span data-i18n="th.turns">Turns</span> <span class="sort-icon" id="sort-icon-turns"></span></th>
        <th class="sortable" onclick="setSessionSort('input')"><span data-i18n="th.input">Input</span> <span class="sort-icon" id="sort-icon-input"></span></th>
        <th class="sortable" onclick="setSessionSort('output')"><span data-i18n="th.output">Output</span> <span class="sort-icon" id="sort-icon-output"></span></th>
        <th class="sortable" onclick="setSessionSort('cost')"><span data-i18n="th.est_cost">Est. Cost</span> <span class="sort-icon" id="sort-icon-cost"></span></th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title" data-i18n="table.cost_by_project">Cost by Project</div><button class="export-btn" onclick="exportProjectsCSV()" data-i18n="table.csv_export" data-i18n-title="table.csv_export_projects_tooltip" title="Export all projects to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th><span data-i18n="th.project">Project</span></th>
        <th class="sortable" onclick="setProjectSort('sessions')"><span data-i18n="th.sessions">Sessions</span> <span class="sort-icon" id="psort-sessions"></span></th>
        <th class="sortable" onclick="setProjectSort('turns')"><span data-i18n="th.turns">Turns</span> <span class="sort-icon" id="psort-turns"></span></th>
        <th class="sortable" onclick="setProjectSort('input')"><span data-i18n="th.input">Input</span> <span class="sort-icon" id="psort-input"></span></th>
        <th class="sortable" onclick="setProjectSort('output')"><span data-i18n="th.output">Output</span> <span class="sort-icon" id="psort-output"></span></th>
        <th class="sortable" onclick="setProjectSort('cost')"><span data-i18n="th.est_cost">Est. Cost</span> <span class="sort-icon" id="psort-cost"></span></th>
      </tr></thead>
      <tbody id="project-cost-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title" data-i18n="table.cost_by_project_branch">Cost by Project &amp; Branch</div><button class="export-btn" onclick="exportProjectBranchCSV()" data-i18n="table.csv_export" data-i18n-title="table.csv_export_project_branch_tooltip" title="Export project+branch breakdown to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th><span data-i18n="th.project">Project</span></th>
        <th><span data-i18n="th.branch">Branch</span></th>
        <th class="sortable" onclick="setProjectBranchSort('sessions')"><span data-i18n="th.sessions">Sessions</span> <span class="sort-icon" id="pbsort-sessions"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('turns')"><span data-i18n="th.turns">Turns</span> <span class="sort-icon" id="pbsort-turns"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('input')"><span data-i18n="th.input">Input</span> <span class="sort-icon" id="pbsort-input"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('output')"><span data-i18n="th.output">Output</span> <span class="sort-icon" id="pbsort-output"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('cost')"><span data-i18n="th.est_cost">Est. Cost</span> <span class="sort-icon" id="pbsort-cost"></span></th>
      </tr></thead>
      <tbody id="project-branch-cost-body"></tbody>
    </table>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p data-i18n-html="footer.cost_disclaimer_html">Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of April 2026. Only models containing <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> in the name are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.</p>
    <p>
      <span data-i18n="footer.github_label">GitHub:</span> <a href="https://github.com/phuryn/claude-usage" target="_blank">https://github.com/phuryn/claude-usage</a>
      &nbsp;&middot;&nbsp;
      <span data-i18n="footer.created_by_label">Created by:</span> <a href="https://www.productcompass.pm" target="_blank" data-i18n="footer.created_by_name">The Product Compass Newsletter</a>
      &nbsp;&middot;&nbsp;
      <span data-i18n="footer.license_label">License:</span> <span data-i18n="footer.license_value">MIT</span>
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

// ── i18n ──────────────────────────────────────────────────────────────────
// English is the source of truth. To add a language, copy the `en` block,
// translate each value, and add the new locale code to `LOCALES` below.
// Keys missing in a locale fall back to English with a console warning.
const MESSAGES = {
  en: {
    'header.title': 'Claude Code Usage Dashboard',
    'header.meta_loading': 'Loading...',
    'header.meta_updated': 'Updated: {date}',
    'header.meta_refresh_note': ' · Auto-refresh in 30s',
    'header.rescan': '↻ Rescan',
    'header.rescan_tooltip': 'Rebuild the database from scratch by re-scanning all JSONL files. Use if data looks stale or costs seem wrong.',
    'header.rescan_scanning': '↻ Scanning...',
    'header.rescan_done': '↻ Rescan ({new} new, {updated} updated)',
    'header.rescan_error': '↻ Rescan (error)',

    'filter.models': 'Models',
    'filter.models_tooltip': 'Select Claude models to include in the aggregation. Only selected models appear in charts and tables.',
    'filter.all': 'All',
    'filter.all_tooltip': 'Select all models',
    'filter.none': 'None',
    'filter.none_tooltip': 'Clear all models',
    'filter.range': 'Range',
    'filter.range_tooltip': 'Select the aggregation period.',

    'range.week': 'This Week',
    'range.week_tooltip': 'This week (starting Monday)',
    'range.month': 'This Month',
    'range.month_tooltip': 'From the 1st of this month through today',
    'range.prev_month': 'Prev Month',
    'range.prev_month_tooltip': 'The full previous month',
    'range.7d': '7d',
    'range.7d_tooltip': 'Last 7 days',
    'range.30d': '30d',
    'range.30d_tooltip': 'Last 30 days',
    'range.90d': '90d',
    'range.90d_tooltip': 'Last 90 days',
    'range.all': 'All',
    'range.all_tooltip': 'All recorded history',

    'range_label.week': 'This Week',
    'range_label.month': 'This Month',
    'range_label.prev-month': 'Previous Month',
    'range_label.7d': 'Last 7 Days',
    'range_label.30d': 'Last 30 Days',
    'range_label.90d': 'Last 90 Days',
    'range_label.all': 'All Time',

    'stats.sessions.label': 'Sessions',
    'stats.sessions.tooltip': 'Number of distinct chat sessions in the selected period.',
    'stats.turns.label': 'Turns',
    'stats.turns.tooltip': 'Total assistant turns. Each tool-call cycle counts as one turn.',
    'stats.input_tokens.label': 'Input Tokens',
    'stats.input_tokens.tooltip': 'Raw prompt tokens you sent to the model. Usually a small portion of total cost.',
    'stats.output_tokens.label': 'Output Tokens',
    'stats.output_tokens.tooltip': 'Tokens generated by Claude. Typically the most expensive component of cost.',
    'stats.cache_read.label': 'Cache Read',
    'stats.cache_read.tooltip': 'Tokens served from prompt cache — about 90% cheaper than fresh input tokens.',
    'stats.cache_read.sub': 'from prompt cache',
    'stats.cache_creation.label': 'Cache Creation',
    'stats.cache_creation.tooltip': 'Tokens written to prompt cache — a 25% premium over input, but later cache reads are far cheaper.',
    'stats.cache_creation.sub': 'writes to prompt cache',
    'stats.est_cost.label': 'Est. Cost',
    'stats.est_cost.tooltip': 'Estimated API cost based on Anthropic API pricing as of April 2026. Max/Pro subscribers have a flat subscription cost instead.',
    'stats.est_cost.sub': 'API pricing, Apr 2026',

    'chart.daily_title': 'Daily Token Usage',
    'chart.daily_title_with_range': 'Daily Token Usage — {range}',
    'chart.daily_title_tooltip': 'Stacked daily token usage by category. Cache tokens use the left axis; raw input/output use the right axis.',
    'chart.hourly_title': 'Average Hourly Distribution',
    'chart.hourly_title_with_range': 'Average Hourly Distribution — {range}',
    'chart.hourly_title_tooltip': 'Average tokens and turns by hour of day across the selected range.',
    'chart.peak_legend': 'Peak hours (PT)',
    'chart.peak_legend_tooltip': 'Mon–Fri 05:00–11:00 PT — Anthropic peak-hour throttling window.',
    'chart.tz_local': 'Local',
    'chart.tz_utc': 'UTC',
    'chart.day_count_singular': '{n} day averaged · {tz}',
    'chart.day_count_plural': '{n} days averaged · {tz}',
    'chart.day_count_empty': 'No data · {tz}',
    'chart.peak_tooltip_suffix': ' · Peak — Anthropic US hours',
    'chart.avg_turns_label': 'Avg turns / hour',
    'chart.avg_output_label': 'Avg output tokens / hour',
    'chart.avg_turns_tooltip': ' Avg turns: {n}',
    'chart.avg_output_tooltip': ' Avg output: {n}',
    'chart.daily.input': 'Input',
    'chart.daily.output': 'Output',
    'chart.daily.cache_read': 'Cache Read',
    'chart.daily.cache_creation': 'Cache Creation',
    'chart.daily.y_left': 'Cache',
    'chart.daily.y_right': 'Input / Output',
    'chart.model_title': 'By Model',
    'chart.model_title_tooltip': 'Token share by model in the selected period.',
    'chart.model_tooltip_label': ' {model}: {tokens} tokens',
    'chart.project_title': 'Top Projects by Tokens',
    'chart.project_title_tooltip': 'Top 10 projects by total tokens in the selected period.',

    'table.cost_by_model': 'Cost by Model',
    'table.recent_sessions': 'Recent Sessions',
    'table.cost_by_project': 'Cost by Project',
    'table.cost_by_project_branch': 'Cost by Project & Branch',
    'table.csv_export': '⤓ CSV',
    'table.csv_export_sessions_tooltip': 'Export all filtered sessions to CSV',
    'table.csv_export_projects_tooltip': 'Export all projects to CSV',
    'table.csv_export_project_branch_tooltip': 'Export project + branch breakdown to CSV',

    'th.session': 'Session',
    'th.project': 'Project',
    'th.branch': 'Branch',
    'th.last_active': 'Last Active',
    'th.duration': 'Duration',
    'th.model': 'Model',
    'th.turns': 'Turns',
    'th.input': 'Input',
    'th.output': 'Output',
    'th.cache_read': 'Cache Read',
    'th.cache_creation': 'Cache Creation',
    'th.est_cost': 'Est. Cost',
    'th.sessions': 'Sessions',
    'th.cost_na': 'n/a',
    'th.duration_min_suffix': 'm',

    'footer.cost_disclaimer_html': 'Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of April 2026. Only models containing <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> in the name are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.',
    'footer.github_label': 'GitHub:',
    'footer.created_by_label': 'Created by:',
    'footer.created_by_name': 'The Product Compass Newsletter',
    'footer.license_label': 'License:',
    'footer.license_value': 'MIT',

    'lang_picker.button_tooltip': 'Select language',
    'lang_picker.title': 'Language',
  },
};

// Display name shown in the language picker for each locale.
// To add a new language, append a new entry here and a matching block in MESSAGES.
const LOCALES = {
  en: 'English',
};

const LANG_STORAGE_KEY = 'claudeUsageLang';
const DEFAULT_LANG = 'en';
let currentLang = DEFAULT_LANG;

function getInitialLang() {
  try {
    const url = new URL(window.location.href);
    const fromUrl = url.searchParams.get('lang');
    if (fromUrl && LOCALES[fromUrl]) return fromUrl;
  } catch(e) {}
  try {
    const stored = localStorage.getItem(LANG_STORAGE_KEY);
    if (stored && LOCALES[stored]) return stored;
  } catch(e) {}
  try {
    const navLangs = navigator.languages || [navigator.language];
    for (const raw of navLangs) {
      if (!raw) continue;
      const short = String(raw).toLowerCase().split('-')[0];
      if (LOCALES[short]) return short;
    }
  } catch(e) {}
  return DEFAULT_LANG;
}

function tr(key, vars) {
  const dict = MESSAGES[currentLang] || MESSAGES[DEFAULT_LANG];
  let msg = dict[key];
  if (msg === undefined) {
    if (currentLang !== DEFAULT_LANG) {
      console.warn('[i18n] missing key for ' + currentLang + ': ' + key);
    }
    msg = MESSAGES[DEFAULT_LANG][key];
  }
  if (msg === undefined) {
    console.warn('[i18n] unknown key: ' + key);
    return key;
  }
  if (vars) {
    return msg.replace(/\{(\w+)\}/g, (_, name) =>
      vars[name] !== undefined ? String(vars[name]) : '{' + name + '}'
    );
  }
  return msg;
}

// Apply translations to all elements with data-i18n / data-i18n-title attributes.
// data-i18n="key"        → sets textContent
// data-i18n-html="key"   → sets innerHTML (use only for trusted message bundles)
// data-i18n-title="key"  → sets the title attribute (hover tooltip)
function applyTranslations() {
  document.documentElement.lang = currentLang;
  document.title = tr('header.title');
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = tr(el.getAttribute('data-i18n'));
  });
  document.querySelectorAll('[data-i18n-html]').forEach(el => {
    el.innerHTML = tr(el.getAttribute('data-i18n-html'));
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    el.setAttribute('title', tr(el.getAttribute('data-i18n-title')));
  });
}

function setLang(lang) {
  if (!LOCALES[lang]) return;
  currentLang = lang;
  try { localStorage.setItem(LANG_STORAGE_KEY, lang); } catch(e) {}
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('lang', lang);
    history.replaceState(null, '', url.toString());
  } catch(e) {}
  applyTranslations();
  if (typeof rerenderAfterLangChange === 'function') rerenderAfterLangChange();
}

// Re-render every JS-driven surface after a language change.
// Static markup is handled by applyTranslations(); this covers the chart
// titles, stat cards, chart datasets, hourly day-count text, etc.
function rerenderAfterLangChange() {
  updateLangButton();
  buildLangMenu();
  if (rawData) applyFilter();
}

// ── Language picker UI ────────────────────────────────────────────────────
function updateLangButton() {
  const labelEl = document.getElementById('lang-btn-label');
  if (labelEl) labelEl.textContent = LOCALES[currentLang] || currentLang;
}

function buildLangMenu() {
  const container = document.getElementById('lang-menu-items');
  if (!container) return;
  const codes = Object.keys(LOCALES);
  container.innerHTML = codes.map(code => {
    const active = code === currentLang ? ' active' : '';
    const aria = code === currentLang ? ' aria-current="true"' : '';
    return '<button type="button" class="lang-option' + active + '"' + aria +
      ' data-lang="' + esc(code) + '" onclick="onLangSelect(\'' + esc(code) + '\')">' +
      '<span>' + esc(LOCALES[code]) + '</span><span class="check" aria-hidden="true">✓</span>' +
      '</button>';
  }).join('');
}

function setLangMenuOpen(open) {
  const menu = document.getElementById('lang-menu');
  const btn = document.getElementById('lang-btn');
  if (!menu || !btn) return;
  menu.classList.toggle('open', open);
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function toggleLangMenu(e) {
  if (e) { e.stopPropagation(); }
  const menu = document.getElementById('lang-menu');
  if (!menu) return;
  setLangMenuOpen(!menu.classList.contains('open'));
}

function onLangSelect(code) {
  setLangMenuOpen(false);
  setLang(code);
}

document.addEventListener('click', (e) => {
  const picker = e.target.closest('.lang-picker');
  if (!picker) setLangMenuOpen(false);
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') setLangMenuOpen(false);
});

currentLang = getInitialLang();

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
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
let lastByProject = [];
let lastByProjectBranch = [];
let sessionSortDir = 'desc';
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
  if (tzMode === 'utc') return tr('chart.tz_utc');
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || tr('chart.tz_local');
  } catch(e) {
    return tr('chart.tz_local');
  }
}

// ── Pricing (Anthropic API, April 2026) ────────────────────────────────────
const PRICING = {
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
  return m.includes('opus') || m.includes('sonnet') || m.includes('haiku');
}

function getPricing(model) {
  if (!model) return null;
  if (PRICING[model]) return PRICING[model];
  for (const key of Object.keys(PRICING)) {
    if (model.startsWith(key)) return PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('opus'))   return PRICING['claude-opus-4-7'];
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

// ── Time range ─────────────────────────────────────────────────────────────
// Range identifiers. Display labels live in MESSAGES under 'range_label.*'
// (e.g. tr('range_label.7d')); they are looked up at render time so the
// language picker can swap them without re-creating the dashboard.
const VALID_RANGES = ['week', 'month', 'prev-month', '7d', '30d', '90d', 'all'];
const RANGE_TICKS  = { 'week': 7, 'month': 15, 'prev-month': 15, '7d': 7, '30d': 15, '90d': 13, 'all': 12 };
function rangeLabel(range) { return tr('range_label.' + range); }

function rangeIncludesToday(range) {
  if (range === 'all') return true;
  const { start, end } = getRangeBounds(range);
  const today = new Date().toISOString().slice(0, 10);
  if (start && today < start) return false;
  if (end && today > end) return false;
  return true;
}

function getRangeBounds(range) {
  if (range === 'all') return { start: null, end: null };
  const today = new Date();
  const iso = d => d.toISOString().slice(0, 10);
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
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
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
  };

  // Hourly aggregation (filtered by model + range, then bucketed by UTC hour)
  const hourlySrc = (rawData.hourly_by_model || []).filter(r =>
    selectedModels.has(r.model) && (!cutoff || r.day >= cutoff)
  );
  const hourlyAgg = aggregateHourly(hourlySrc, hourlyTZ);

  // Update daily chart title
  document.getElementById('daily-chart-title').textContent = tr('chart.daily_title_with_range', { range: rangeLabel(selectedRange) });
  document.getElementById('hourly-chart-title').textContent = tr('chart.hourly_title_with_range', { range: rangeLabel(selectedRange) });

  renderStats(totals);
  renderDailyChart(daily);
  renderHourlyChart(hourlyAgg);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  lastFilteredSessions = sortSessions(filteredSessions);
  lastByProject = sortProjects(byProject);
  lastByProjectBranch = sortProjectBranch(byProjectBranch);
  renderSessionsTable(lastFilteredSessions.slice(0, 20));
  renderModelCostTable(byModel);
  renderProjectCostTable(lastByProject.slice(0, 20));
  renderProjectBranchCostTable(lastByProjectBranch.slice(0, 20));
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeSub = rangeLabel(selectedRange).toLowerCase();
  const stats = [
    { key: 'sessions',       label: tr('stats.sessions.label'),       tooltip: tr('stats.sessions.tooltip'),       value: t.sessions.toLocaleString(), sub: rangeSub },
    { key: 'turns',          label: tr('stats.turns.label'),          tooltip: tr('stats.turns.tooltip'),          value: fmt(t.turns),                sub: rangeSub },
    { key: 'input_tokens',   label: tr('stats.input_tokens.label'),   tooltip: tr('stats.input_tokens.tooltip'),   value: fmt(t.input),                sub: rangeSub },
    { key: 'output_tokens',  label: tr('stats.output_tokens.label'),  tooltip: tr('stats.output_tokens.tooltip'),  value: fmt(t.output),               sub: rangeSub },
    { key: 'cache_read',     label: tr('stats.cache_read.label'),     tooltip: tr('stats.cache_read.tooltip'),     value: fmt(t.cache_read),           sub: tr('stats.cache_read.sub') },
    { key: 'cache_creation', label: tr('stats.cache_creation.label'), tooltip: tr('stats.cache_creation.tooltip'), value: fmt(t.cache_creation),       sub: tr('stats.cache_creation.sub') },
    { key: 'est_cost',       label: tr('stats.est_cost.label'),       tooltip: tr('stats.est_cost.tooltip'),       value: fmtCostBig(t.cost),          sub: tr('stats.est_cost.sub'), color: '#4ade80' },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card" title="${esc(s.tooltip)}">
      <div class="label">${esc(s.label)}</div>
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
  if (!agg.dayCount) {
    dayCountEl.textContent = tr('chart.day_count_empty', { tz: tzDisplayName(hourlyTZ) });
  } else {
    const key = agg.dayCount === 1 ? 'chart.day_count_singular' : 'chart.day_count_plural';
    dayCountEl.textContent = tr(key, { n: agg.dayCount, tz: tzDisplayName(hourlyTZ) });
  }

  const ctx = document.getElementById('chart-hourly').getContext('2d');
  if (charts.hourly) charts.hourly.destroy();

  const labels = agg.hours.map(h => (h.peak ? '⚡ ' : '') + formatHourLabel(h.hour));
  const turns  = agg.hours.map(h => h.avgTurns);
  const output = agg.hours.map(h => h.avgOutput);
  const barColors = agg.hours.map(h => h.peak ? 'rgba(248,113,113,0.8)' : TOKEN_COLORS.input);

  const turnsLabel  = tr('chart.avg_turns_label');
  const outputLabel = tr('chart.avg_output_label');

  charts.hourly = new Chart(ctx, {
    data: {
      labels: labels,
      datasets: [
        {
          type: 'bar',
          label: turnsLabel,
          data: turns,
          backgroundColor: barColors,
          yAxisID: 'y',
          order: 2,
        },
        {
          type: 'line',
          label: outputLabel,
          data: output,
          borderColor: TOKEN_COLORS.output,
          backgroundColor: 'rgba(167,139,250,0.15)',
          borderWidth: 2,
          pointRadius: 2,
          tension: 0.3,
          yAxisID: 'y1',
          order: 1,
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#8892a4', boxWidth: 12 } },
        tooltip: {
          callbacks: {
            title: (items) => {
              if (!items.length) return '';
              const idx = items[0].dataIndex;
              const h = agg.hours[idx];
              const base = formatHourLabel(h.hour) + ' ' + tzDisplayName(hourlyTZ);
              return h.peak ? base + tr('chart.peak_tooltip_suffix') : base;
            },
            // Dataset 0 is the turns bar, dataset 1 is the output line.
            // Index-based dispatch keeps tooltip output stable across locales.
            label: (item) => {
              if (item.datasetIndex === 0) {
                return tr('chart.avg_turns_tooltip', { n: item.parsed.y.toFixed(2) });
              }
              return tr('chart.avg_output_tooltip', { n: fmt(item.parsed.y) });
            },
          }
        },
      },
      scales: {
        x: { ticks: { color: '#8892a4', maxRotation: 0, autoSkip: false, font: { size: 10 } }, grid: { color: '#2a2d3a' } },
        y:  { position: 'left',  beginAtZero: true, ticks: { color: '#8892a4', callback: v => v.toFixed(1) },     grid: { color: '#2a2d3a' }, title: { display: true, text: turnsLabel,  color: '#8892a4', font: { size: 11 } } },
        y1: { position: 'right', beginAtZero: true, ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { drawOnChartArea: false },   title: { display: true, text: outputLabel, color: '#8892a4', font: { size: 11 } } },
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
        { label: tr('chart.daily.input'),          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'io',    yAxisID: 'y1' },
        { label: tr('chart.daily.output'),         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'io',    yAxisID: 'y1' },
        { label: tr('chart.daily.cache_read'),     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'cache', yAxisID: 'y' },
        { label: tr('chart.daily.cache_creation'), data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'cache', yAxisID: 'y' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: '#2a2d3a' } },
        y:  { position: 'left',  ticks: { color: '#74de80', callback: v => fmt(v) }, grid: { color: '#2a2d3a' }, title: { display: true, text: tr('chart.daily.y_left'),  color: '#74de80' } },
        y1: { position: 'right', ticks: { color: '#4f8ef7', callback: v => fmt(v) }, grid: { drawOnChartArea: false },    title: { display: true, text: tr('chart.daily.y_right'), color: '#4f8ef7' } },
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
        tooltip: { callbacks: { label: ctx => tr('chart.model_tooltip_label', { model: ctx.label, tokens: fmt(ctx.raw) }) } }
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
        { label: tr('chart.daily.input'),  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input },
        { label: tr('chart.daily.output'), data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output },
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
  const naLabel = tr('th.cost_na');
  const minSuffix = tr('th.duration_min_suffix');
  document.getElementById('sessions-body').innerHTML = sessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">${esc(naLabel)}</td>`;
    return `<tr>
      <td class="muted" style="font-family:monospace">${esc(s.session_id)}&hellip;</td>
      <td>${esc(s.project)}</td>
      <td class="muted">${esc(s.last)}</td>
      <td class="muted">${esc(s.duration_min)}${esc(minSuffix)}</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
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
  return [...rows].sort((a, b) => {
    const pa = (a.project || '').toLowerCase();
    const pb = (b.project || '').toLowerCase();
    if (pa < pb) return -1;
    if (pa > pb) return 1;
    const av = a[branchSortCol] ?? 0;
    const bv = b[branchSortCol] ?? 0;
    if (av < bv) return branchSortDir === 'desc' ? 1 : -1;
    if (av > bv) return branchSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectBranchCostTable(rows) {
  document.getElementById('project-branch-cost-body').innerHTML = sortProjectBranch(rows).map(pb => {
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
  const header = ['Session', 'Project', 'Last Active', 'Duration (min)', 'Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastFilteredSessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    return [s.session_id, s.project, s.last, s.duration_min, s.model, s.turns, s.input, s.output, s.cache_read, s.cache_creation, cost.toFixed(4)];
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

// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  const btn = document.getElementById('rescan-btn');
  btn.disabled = true;
  btn.textContent = tr('header.rescan_scanning');
  try {
    const resp = await fetch('/api/rescan', { method: 'POST' });
    const d = await resp.json();
    btn.textContent = tr('header.rescan_done', { new: d.new, updated: d.updated });
    await loadData();
  } catch(e) {
    btn.textContent = tr('header.rescan_error');
    console.error(e);
  }
  setTimeout(() => { btn.textContent = tr('header.rescan'); btn.disabled = false; }, 3000);
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
    const refreshNote = rangeIncludesToday(selectedRange) ? tr('header.meta_refresh_note') : '';
    document.getElementById('meta').textContent = tr('header.meta_updated', { date: d.generated_at }) + refreshNote;

    const isFirstLoad = rawData === null;
    rawData = d;

    if (isFirstLoad) {
      // Restore range from URL, mark active button
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
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

applyTranslations();
updateLangButton();
buildLangMenu();
loadData();
scheduleAutoRefresh();
</script>
</body>
</html>
"""


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

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/rescan":
            # Full rebuild: delete DB and rescan from scratch.
            # Pass DB_PATH / DEFAULT_PROJECTS_DIRS explicitly so tests that
            # patch the module globals are honored (scan's defaults are
            # frozen at def time and would otherwise target the real paths).
            import scanner
            db_path = DB_PATH
            if db_path.exists():
                db_path.unlink()
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


def serve(host=None, port=None):
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8080"))
    server = HTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
