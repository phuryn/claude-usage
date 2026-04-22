"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import os
import sqlite3
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
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

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count
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
        "all_models":     all_models,
        "daily_by_model": daily_by_model,
        "sessions_all":   sessions_all,
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@9/marked.min.js"></script>
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
  header .header-btns { display: flex; gap: 8px; align-items: center; }
  #rescan-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  #rescan-btn:hover { color: var(--text); border-color: var(--accent); }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  #analyze-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  #analyze-btn:hover:not(:disabled) { color: var(--text); border-color: var(--accent); }
  #analyze-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  #analyze-btn.ready { border-color: var(--accent); color: var(--accent); }

  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 100; align-items: center; justify-content: center; }
  .modal-overlay.open { display: flex; }
  .modal-box { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 28px; max-width: 500px; width: 90%; }
  .modal-box h3 { font-size: 15px; font-weight: 600; color: var(--text); margin-bottom: 12px; }
  .modal-box p { font-size: 13px; color: var(--muted); margin-bottom: 10px; line-height: 1.5; }
  .modal-box ul { font-size: 13px; color: var(--muted); padding-left: 18px; margin-bottom: 14px; line-height: 1.8; }
  .modal-box pre { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px; font-size: 11px; color: var(--muted); max-height: 160px; overflow-y: auto; white-space: pre-wrap; margin-bottom: 14px; }
  .modal-btns { display: flex; gap: 8px; justify-content: flex-end; align-items: center; flex-wrap: wrap; }
  .modal-btns .btn-primary { background: var(--accent); color: white; border: none; padding: 7px 18px; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600; }
  .modal-btns .btn-secondary { background: transparent; color: var(--muted); border: 1px solid var(--border); padding: 7px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .modal-btns .btn-secondary:hover { color: var(--text); }
  .modal-btns label { font-size: 11px; color: var(--muted); display: flex; align-items: center; gap: 5px; cursor: pointer; flex-grow: 1; }

  #analyzer-panel { display: none; position: fixed; top: 0; right: 0; height: 100vh; width: 440px; background: var(--card); border-left: 1px solid var(--border); z-index: 90; flex-direction: column; box-shadow: -4px 0 24px rgba(0,0,0,0.5); }
  #analyzer-panel.open { display: flex; }
  .panel-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
  .panel-header h3 { font-size: 14px; font-weight: 600; color: var(--text); }
  .panel-close { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 18px; padding: 0 4px; line-height: 1; }
  .panel-close:hover { color: var(--text); }
  .panel-status { padding: 10px 20px; border-bottom: 1px solid var(--border); font-size: 11px; color: var(--muted); flex-shrink: 0; min-height: 36px; }
  .panel-body { flex: 1; overflow-y: auto; padding: 16px 20px; }
  .panel-body .suggestions { font-size: 13px; color: var(--text); line-height: 1.6; }
  .panel-body .suggestions h2 { font-size: 13px; font-weight: 700; color: var(--accent); margin: 18px 0 6px; border: none; padding: 0; }
  .panel-body .suggestions h2:first-child { margin-top: 0; }
  .panel-body .suggestions p { margin: 0 0 6px; }
  .panel-body .suggestions code { background: var(--bg); border: 1px solid var(--border); border-radius: 3px; padding: 1px 5px; font-size: 11px; color: var(--green); font-family: monospace; }
  .panel-body .suggestions pre { background: var(--bg); border: 1px solid var(--border); border-radius: 5px; padding: 8px 10px; overflow-x: auto; margin: 6px 0; }
  .panel-body .suggestions pre code { background: none; border: none; padding: 0; font-size: 11px; }
  .panel-body .suggestions hr { border: none; border-top: 1px solid var(--border); margin: 14px 0; }
  .panel-body .suggestions strong { color: var(--text); font-weight: 600; }
  .panel-footer { padding: 12px 20px; border-top: 1px solid var(--border); flex-shrink: 0; display: flex; gap: 8px; }
  #deep-dive-btn { flex: 1; background: transparent; border: 1px solid var(--border); color: var(--muted); padding: 7px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  #deep-dive-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  #deep-dive-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  #rerun-btn { background: var(--accent); color: white; border: none; padding: 7px 14px; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 600; }
  #rerun-btn:disabled { opacity: 0.5; cursor: not-allowed; }

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
  <h1>Claude Code Usage Dashboard</h1>
  <div class="meta" id="meta">Loading...</div>
  <div class="header-btns">
    <button id="analyze-btn" onclick="analyzeClick()" disabled title="Checking for claude CLI...">&#x2728; Analyze Usage</button>
    <button id="rescan-btn" onclick="triggerRescan()" title="Rebuild the database from scratch by re-scanning all JSONL files. Use if data looks stale or costs seem wrong.">&#x21bb; Rescan</button>
  </div>
</header>

<!-- Analyzer disclosure modal -->
<div class="modal-overlay" id="analyzer-modal">
  <div class="modal-box">
    <h3>&#x2728; Analyze Usage</h3>
    <p>This runs your local <code>claude</code> CLI:</p>
    <ul>
      <li>Uses your existing Claude auth &amp; plan</li>
      <li>Tokens count toward your own usage</li>
      <li>Estimated cost: ~$0.05&ndash;0.20 per analysis</li>
    </ul>
    <p style="font-size:12px;color:var(--muted);margin-bottom:6px">Data sent (scrubbed snapshot &mdash; project paths hashed):</p>
    <pre id="modal-snapshot-preview">Loading...</pre>
    <div class="modal-btns">
      <label><input type="checkbox" id="modal-dont-show"> Don&rsquo;t show again</label>
      <button class="btn-secondary" onclick="closeModal()">Cancel</button>
      <button class="btn-primary" onclick="runAnalysis()">Run Analysis</button>
    </div>
  </div>
</div>

<!-- Analyzer results panel -->
<div id="analyzer-panel">
  <div class="panel-header">
    <h3>&#x2728; Usage Analysis</h3>
    <button class="panel-close" onclick="closePanel()">&#x2715;</button>
  </div>
  <div class="panel-status" id="panel-status">Ready.</div>
  <div class="panel-body"><div class="suggestions" id="panel-suggestions"></div></div>
  <div class="panel-footer">
    <button id="deep-dive-btn" onclick="launchDeepDive()" disabled>&#x1F50D; Open Deep Dive</button>
    <button id="rerun-btn" onclick="analyzeClick()">&#x21bb; Re-run</button>
  </div>
</div>

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
</div>

<div class="container">
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide">
      <h2 id="daily-chart-title">Daily Token Usage</h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
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

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time' };
const RANGE_TICKS  = { '7d': 7, '30d': 15, '90d': 13, 'all': 12 };

function getRangeCutoff(range) {
  if (range === 'all') return null;
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return ['7d', '30d', '90d', 'all'].includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
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

  const cutoff = getRangeCutoff(selectedRange);

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!cutoff || r.day >= cutoff)
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
    selectedModels.has(s.model) && (!cutoff || s.last_date >= cutoff)
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
  document.getElementById('daily-chart-title').textContent = 'Daily Token Usage \u2014 ' + RANGE_LABELS[selectedRange];

  renderStats(totals);
  renderDailyChart(daily);
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
  const rangeLabel = RANGE_LABELS[selectedRange].toLowerCase();
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
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: '#2a2d3a' } },
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
    return `<tr>
      <td class="muted" style="font-family:monospace">${esc(s.session_id)}&hellip;</td>
      <td>${esc(s.project)}</td>
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

    if (isFirstLoad) {
      // Restore range from URL, mark active button
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
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

// ── Analyzer ──────────────────────────────────────────────────────────────
let analyzerReady = false;
let activeSSE = null;

async function initAnalyzer() {
  try {
    const r = await fetch('/api/analyzer/preflight');
    const d = await r.json();
    analyzerReady = d.cli_available;
    const btn = document.getElementById('analyze-btn');
    if (analyzerReady) {
      btn.disabled = false;
      btn.classList.add('ready');
      btn.title = 'Analyze usage with your local claude CLI (' + (d.version || 'found') + ')';
    } else {
      btn.title = 'Requires Claude Code CLI. Install: https://claude.com/claude-code';
    }
  } catch(e) { /* non-fatal */ }
}

async function analyzeClick() {
  if (!analyzerReady) return;
  if (localStorage.getItem('analyzer_skip_modal') === '1') { runAnalysis(); return; }
  try {
    const r = await fetch('/api/analyzer/snapshot');
    const d = await r.json();
    const snap = d.snapshot || {};
    const lines = [
      'Cache hit rate: ' + ((snap.cache_hit_rate*100)||0).toFixed(1) + '%',
      'Cost (30d): $' + ((snap.total_cost_30d)||0).toFixed(2),
      'Sessions: ' + ((snap.session_patterns||{}).count||0),
      'Models: ' + (snap.model_distribution||[]).map(function(m){return m.model;}).join(', '),
      'Top tools: ' + (snap.top_tools||[]).slice(0,5).map(function(t){return t.tool;}).join(', '),
    ];
    document.getElementById('modal-snapshot-preview').textContent = lines.join('\n');
  } catch(e) {
    document.getElementById('modal-snapshot-preview').textContent = '(preview unavailable)';
  }
  document.getElementById('analyzer-modal').classList.add('open');
}

function closeModal() { document.getElementById('analyzer-modal').classList.remove('open'); }

function runAnalysis() {
  var cb = document.getElementById('modal-dont-show');
  if (cb && cb.checked) localStorage.setItem('analyzer_skip_modal', '1');
  closeModal();
  openPanel();
  startSSE();
}

function openPanel() {
  document.getElementById('analyzer-panel').classList.add('open');
  document.getElementById('panel-suggestions').innerHTML = '';
  document.getElementById('deep-dive-btn').disabled = true;
  setStatus('Starting analysis...');
}

function closePanel() {
  document.getElementById('analyzer-panel').classList.remove('open');
  if (activeSSE) { activeSSE.close(); activeSSE = null; }
}

function setStatus(msg) { document.getElementById('panel-status').textContent = msg; }

function startSSE() {
  if (activeSSE) activeSSE.close();
  document.getElementById('rerun-btn').disabled = true;
  var sugEl = document.getElementById('panel-suggestions');
  sugEl.innerHTML = '';
  var es = new EventSource('/api/analyzer/stream');
  activeSSE = es;
  var rawText = '';
  var renderTimer = null;

  function renderMd() {
    if (typeof marked !== 'undefined') {
      sugEl.innerHTML = marked.parse(rawText);
    } else {
      sugEl.textContent = rawText;
    }
  }

  es.onmessage = function(e) {
    try {
      var d = JSON.parse(e.data);
      if (d.type === 'chunk') {
        rawText += d.text;
        // Throttle re-renders to every 200ms while streaming
        if (!renderTimer) renderTimer = setTimeout(function() { renderMd(); renderTimer = null; }, 200);
      } else if (d.type === 'done') {
        if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
        renderMd();
        var inp = d.input_tokens || 0, out = d.output_tokens || 0;
        var est = ((inp * 3 + out * 15) / 1000000).toFixed(4);
        setStatus('Done · ' + inp.toLocaleString() + ' in / ' + out.toLocaleString() + ' out · est $' + est);
        document.getElementById('deep-dive-btn').disabled = false;
        document.getElementById('rerun-btn').disabled = false;
        es.close(); activeSSE = null;
      } else if (d.type === 'error') {
        setStatus('Error: ' + d.message);
        document.getElementById('rerun-btn').disabled = false;
        es.close(); activeSSE = null;
      }
    } catch(ex) {}
  };
  es.onerror = function() {
    if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
    var has = rawText.length > 0;
    if (has) renderMd();
    setStatus(has ? 'Stream closed.' : 'Connection error.');
    document.getElementById('rerun-btn').disabled = false;
    if (has) document.getElementById('deep-dive-btn').disabled = false;
    es.close(); activeSSE = null;
  };
  setStatus('Running: claude --print --output-format stream-json ...');
}

async function launchDeepDive() {
  var btn = document.getElementById('deep-dive-btn');
  btn.disabled = true; btn.textContent = 'Launching...';
  try {
    var r = await fetch('/api/analyzer/launch-deep-dive', {method: 'POST'});
    var d = await r.json();
    setStatus(d.ok ? 'Deep dive launched in terminal.' : 'Launch failed: ' + d.detail);
  } catch(e) {
    setStatus('Launch error: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = '🔍 Open Deep Dive';
  }
}

initAnalyzer();
</script>
</body>
</html>
"""


# ── Analyzer state ────────────────────────────────────────────────────────────

_preflight_cache = None
_snapshot_cache  = None
_analyzer_lock   = threading.Lock()
_SNAPSHOT_TTL    = 600  # seconds


def _run_preflight():
    """Run `claude --version` and cache result. Returns (available, version_str)."""
    global _preflight_cache
    import subprocess, shutil
    if not shutil.which("claude"):
        _preflight_cache = (False, None)
        return False, None
    try:
        r = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
        ver = r.stdout.strip() or r.stderr.strip() or "unknown"
        _preflight_cache = (True, ver)
        return True, ver
    except Exception:
        _preflight_cache = (False, None)
        return False, None


def _get_analyzer_snapshot():
    """Return cached snapshot or rebuild (10-min TTL)."""
    global _snapshot_cache
    now = time.time()
    if _snapshot_cache and (now - _snapshot_cache[1]) < _SNAPSHOT_TTL:
        return _snapshot_cache[0]
    if not DB_PATH.exists():
        return None
    try:
        from scanner import get_db, init_db
        from analyzer import build_snapshot
        conn = get_db(DB_PATH)
        init_db(conn)
        snap = build_snapshot(conn)
        conn.close()
        _snapshot_cache = (snap, now)
        return snap
    except Exception:
        return None


def _extract_text_delta(event):
    """Extract text from a claude stream-json event.

    Actual format: {"type":"stream_event","event":{"type":"content_block_delta",
    "delta":{"type":"text_delta","text":"..."}}}
    """
    # Primary: stream_event wrapper (confirmed format from claude v2.1.118)
    if event.get("type") == "stream_event":
        inner = event.get("event") or {}
        if inner.get("type") == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta":
                return delta.get("text", "")
        return None
    # Fallback: bare content_block_delta
    if event.get("type") == "content_block_delta":
        delta = event.get("delta") or {}
        return delta.get("text", "")
    return None


def _stream_analyzer(snapshot, wfile):
    """Run claude --print streaming, forward text deltas as SSE events."""
    import subprocess
    from analyzer import build_prompt, scrub, estimate_waste

    prompt = build_prompt(snapshot)

    def sse(data):
        line = f"data: {json.dumps(data)}\n\n"
        try:
            wfile.write(line.encode("utf-8"))
            wfile.flush()
        except Exception:
            pass

    try:
        proc = subprocess.Popen(
            ["claude", "--print", "--output-format", "stream-json",
             "--verbose", "--include-partial-messages"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
        )
        proc.stdin.write(prompt)
        proc.stdin.close()

        suggestions = ""
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            text = _extract_text_delta(ev)
            if text:
                suggestions += text
                sse({"type": "chunk", "text": text})
            # result event: {"type":"result","subtype":"success","usage":{...},"duration_ms":N}
            if ev.get("type") == "result" and ev.get("subtype") == "success":
                usage = ev.get("usage") or {}
                sse({"type": "done",
                     "input_tokens":  usage.get("input_tokens", 0),
                     "output_tokens": usage.get("output_tokens", 0),
                     "duration_ms":   ev.get("duration_ms", 0)})

        proc.wait(timeout=5)

        if suggestions and DB_PATH.exists():
            try:
                from scanner import get_db, init_db
                conn = get_db(DB_PATH)
                init_db(conn)
                waste = estimate_waste(snapshot)
                conn.execute(
                    "INSERT INTO analyses(timestamp,snapshot_json,suggestions_md,cache_rate,est_monthly_waste) "
                    "VALUES (?,?,?,?,?)",
                    (datetime.now().isoformat(), json.dumps(scrub(snapshot)),
                     suggestions, snapshot["cache_hit_rate"], waste["cache_savings"])
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

    except FileNotFoundError:
        sse({"type": "error", "message": "claude CLI not found"})
    except Exception as e:
        sse({"type": "error", "message": str(e)})


def _launch_deep_dive(snapshot):
    """Write launch script and spawn terminal. Returns (ok, detail_str)."""
    import sys as _sys
    import subprocess
    from analyzer import build_prompt

    tmp_dir = Path.home() / ".claude" / "analyzer-tmp"
    tmp_dir.mkdir(exist_ok=True)

    ts       = datetime.now().strftime("%Y%m%d-%H%M%S")
    ctx_file = tmp_dir / f"analyzer-context-{ts}.md"
    ctx_file.write_text(build_prompt(snapshot), encoding="utf-8")

    platform = _sys.platform
    try:
        if platform == "win32":
            bat = tmp_dir / f"launch-{ts}.bat"
            bat.write_text(
                f'@echo off\r\n'
                f'echo === Claude Usage Analyzer - Deep Dive ===\r\n'
                f'echo Session uses your claude CLI + auth.\r\n'
                f'echo Tokens billed to your plan.\r\n'
                f'echo.\r\n'
                f'type "{ctx_file}" | claude\r\n'
                f'del "{ctx_file}"\r\n'
                f'pause\r\n',
                encoding="utf-8"
            )
            os.startfile(str(bat))
            return True, str(bat)
        elif platform == "darwin":
            sh = tmp_dir / f"launch-{ts}.command"
            sh.write_text(
                f'#!/bin/bash\necho "=== Claude Usage Analyzer - Deep Dive ==="\n'
                f'cat "{ctx_file}" | claude\nrm -f "{ctx_file}" "$0"\n',
                encoding="utf-8"
            )
            sh.chmod(0o755)
            subprocess.Popen(["open", "-a", "Terminal", str(sh)])
            return True, str(sh)
        else:
            sh = tmp_dir / f"launch-{ts}.sh"
            sh.write_text(
                f'#!/bin/bash\necho "=== Claude Usage Analyzer - Deep Dive ==="\n'
                f'cat "{ctx_file}" | claude\nrm -f "{ctx_file}"\n',
                encoding="utf-8"
            )
            sh.chmod(0o755)
            import shutil
            for term in ["x-terminal-emulator", "gnome-terminal", "konsole", "xterm"]:
                if shutil.which(term):
                    subprocess.Popen([term, "-e", str(sh)])
                    return True, str(sh)
            return False, f"No terminal detected. Run manually:\n  bash {sh}"
    except Exception as e:
        return False, str(e)


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

        elif self.path == "/api/analyzer/preflight":
            avail, ver = _run_preflight()
            body = json.dumps({"cli_available": avail, "version": ver}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/api/analyzer/snapshot":
            snap = _get_analyzer_snapshot()
            if snap is None:
                body = json.dumps({"error": "No database found"}).encode("utf-8")
                self.send_response(404)
            else:
                from analyzer import scrub, estimate_waste
                body = json.dumps({"snapshot": scrub(snap), "waste": estimate_waste(snap)}).encode("utf-8")
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/api/analyzer/stream":
            if not _analyzer_lock.acquire(blocking=False):
                self.send_response(409)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"type":"error","message":"Analysis already running"}\n\n')
                return
            try:
                snap = _get_analyzer_snapshot()
                if snap is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                _stream_analyzer(snap, self.wfile)
            finally:
                _analyzer_lock.release()

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

        elif self.path == "/api/analyzer/launch-deep-dive":
            snap = _get_analyzer_snapshot()
            if snap is None:
                resp = json.dumps({"ok": False, "error": "No database found"}).encode("utf-8")
                self.send_response(404)
            else:
                ok, detail = _launch_deep_dive(snap)
                resp = json.dumps({"ok": ok, "detail": detail}).encode("utf-8")
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        else:
            self.send_response(404)
            self.end_headers()


def serve(host=None, port=None):
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), DashboardHandler)

    threading.Thread(target=_run_preflight, daemon=True, name="analyzer-preflight").start()

    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
