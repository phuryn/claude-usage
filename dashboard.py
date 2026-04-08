"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime
from scanner import scan

DB_PATH = Path.home() / ".claude" / "usage.db"


def get_dashboard_data(db_path=DB_PATH):
    # Scan for new JSONL files before querying
    scan(verbose=False)

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
            s.session_id, s.project_name, s.first_timestamp, s.last_timestamp,
            s.total_input_tokens, s.total_output_tokens,
            s.total_cache_read, s.total_cache_creation, s.model, s.turn_count,
            (SELECT t.cwd FROM turns t WHERE t.session_id = s.session_id LIMIT 1) as cwd
        FROM sessions s
        ORDER BY s.last_timestamp DESC
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
            "session_id":    r["session_id"],
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
            "cwd":           r["cwd"] or "",
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
  th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(79,142,247,0.15); color: var(--blue); }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; overflow-x: auto; }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 8px; }
  .footer-content { max-width: 1400px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }

  /* Cost modal */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 1000; justify-content: center; align-items: center; }
  .modal-overlay.open { display: flex; }
  .modal { background: var(--card); border: 1px solid var(--border); border-radius: 12px; width: 90%; max-width: 720px; max-height: 80vh; overflow: hidden; display: flex; flex-direction: column; }
  .modal-header { display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; border-bottom: 1px solid var(--border); }
  .modal-header h2 { font-size: 16px; font-weight: 600; color: var(--text); }
  .modal-close { background: none; border: none; color: var(--muted); font-size: 20px; cursor: pointer; padding: 4px 8px; border-radius: 4px; }
  .modal-close:hover { color: var(--text); background: rgba(255,255,255,0.06); }
  .modal-tabs { display: flex; border-bottom: 1px solid var(--border); padding: 0 20px; }
  .modal-tab { padding: 10px 16px; font-size: 13px; color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent; transition: color 0.15s, border-color 0.15s; }
  .modal-tab:hover { color: var(--text); }
  .modal-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .modal-body { padding: 20px; overflow-y: auto; flex: 1; }
  .cost-bar { display: flex; height: 8px; border-radius: 4px; overflow: hidden; margin: 6px 0 2px; }
  .cost-bar-seg { height: 100%; min-width: 2px; }
  .cost-row { display: flex; justify-content: space-between; align-items: baseline; padding: 10px 0; border-bottom: 1px solid var(--border); }
  .cost-row:last-child { border-bottom: none; }
  .cost-row .name { font-size: 13px; }
  .cost-row .amount { font-size: 15px; font-weight: 600; font-family: monospace; color: var(--green); }
  .cost-row .pct { font-size: 11px; color: var(--muted); margin-left: 8px; }
  .cost-row .detail { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .cost-total { display: flex; justify-content: space-between; padding: 12px 0; border-top: 2px solid var(--border); margin-top: 8px; font-weight: 700; }
  .cost-total .amount { font-size: 18px; color: var(--green); font-family: monospace; }
  .clickable { cursor: pointer; transition: border-color 0.15s, box-shadow 0.15s; }
  .clickable:hover { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }

  /* Sortable sessions table */
  .table-scroll { max-height: 600px; overflow-y: auto; }
  th.sortable { cursor: pointer; user-select: none; white-space: nowrap; }
  th.sortable:hover { color: var(--text); }
  th.sortable .sort-arrow { font-size: 10px; margin-left: 3px; opacity: 0.3; }
  th.sortable.asc .sort-arrow, th.sortable.desc .sort-arrow { opacity: 1; color: var(--accent); }
  tr.session-row { cursor: pointer; }
  tr.session-row:hover td { background: rgba(217,119,87,0.06); }
  .sessions-count { font-size: 12px; color: var(--muted); font-weight: 400; margin-left: 8px; }

  /* Session detail modal */
  .detail-header { margin-bottom: 16px; }
  .detail-header .detail-title { font-size: 15px; font-weight: 600; margin-bottom: 8px; }
  .detail-meta { display: flex; flex-wrap: wrap; gap: 16px; font-size: 12px; color: var(--muted); }
  .detail-meta span { display: flex; align-items: center; gap: 4px; }
  .detail-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin-bottom: 16px; }
  .detail-stat { background: var(--bg); border-radius: 6px; padding: 10px; }
  .detail-stat .label { font-size: 10px; text-transform: uppercase; color: var(--muted); margin-bottom: 2px; }
  .detail-stat .value { font-size: 16px; font-weight: 600; }
  .tool-tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; background: rgba(167,139,250,0.15); color: #a78bfa; }
  .turn-list { max-height: 400px; overflow-y: auto; }
  .turn-list table { font-size: 12px; }
  .turn-list td { padding: 6px 10px; }
  .turn-list th { padding: 6px 10px; position: sticky; top: 0; background: var(--card); }

  /* Activity timeline */
  .activity-log { max-height: 400px; overflow-y: auto; margin-bottom: 16px; border: 1px solid var(--border); border-radius: 6px; }
  .activity-entry { padding: 8px 12px; border-bottom: 1px solid var(--border); font-size: 12px; line-height: 1.5; }
  .activity-entry:last-child { border-bottom: none; }
  .activity-entry.user { background: rgba(79,142,247,0.06); }
  .activity-entry.assistant { background: transparent; }
  .activity-entry.tool { background: rgba(167,139,250,0.04); color: var(--muted); font-family: monospace; font-size: 11px; }
  .activity-role { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 2px; }
  .activity-role.user { color: var(--blue); }
  .activity-role.assistant { color: var(--accent); }
  .activity-role.tool { color: #a78bfa; }
  .activity-text { white-space: pre-wrap; word-break: break-word; }
  .detail-tabs { display: flex; border-bottom: 1px solid var(--border); margin-bottom: 12px; }
  .detail-tab { padding: 8px 14px; font-size: 12px; color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent; }
  .detail-tab:hover { color: var(--text); }
  .detail-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .detail-tab-content { display: none; }
  .detail-tab-content.active { display: block; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } }
</style>
</head>
<body>
<header>
  <h1>Claude Code Usage Dashboard</h1>
  <div class="meta" id="meta">Loading...</div>
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
    <div class="section-title">Sessions<span class="sessions-count" id="sessions-count"></span></div>
    <div class="table-scroll">
    <table>
      <thead><tr>
        <th class="sortable" data-key="session_id" onclick="sortSessions('session_id')">Session <span class="sort-arrow">&#9650;</span></th>
        <th class="sortable" data-key="project" onclick="sortSessions('project')">Project <span class="sort-arrow">&#9650;</span></th>
        <th class="sortable desc" data-key="last" onclick="sortSessions('last')">Last Active <span class="sort-arrow">&#9660;</span></th>
        <th class="sortable" data-key="duration_min" onclick="sortSessions('duration_min')">Duration <span class="sort-arrow">&#9650;</span></th>
        <th class="sortable" data-key="model" onclick="sortSessions('model')">Model <span class="sort-arrow">&#9650;</span></th>
        <th class="sortable" data-key="turns" onclick="sortSessions('turns')">Turns <span class="sort-arrow">&#9650;</span></th>
        <th class="sortable" data-key="input" onclick="sortSessions('input')">Input <span class="sort-arrow">&#9650;</span></th>
        <th class="sortable" data-key="output" onclick="sortSessions('output')">Output <span class="sort-arrow">&#9650;</span></th>
        <th class="sortable" data-key="cost" onclick="sortSessions('cost')">Est. Cost <span class="sort-arrow">&#9650;</span></th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
    </div>
  </div>
  <div class="table-card">
    <div class="section-title">Cost by Model</div>
    <table>
      <thead><tr>
        <th>Model</th><th>Turns</th><th>Input</th><th>Output</th>
        <th>Cache Read</th><th>Cache Creation</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
  </div>
</div>

<!-- Session Detail Modal -->
<div class="modal-overlay" id="session-modal" onclick="if(event.target===this)closeSessionModal()">
  <div class="modal" style="max-width:860px">
    <div class="modal-header">
      <h2 id="session-modal-title">Session Detail</h2>
      <button class="modal-close" onclick="closeSessionModal()">&times;</button>
    </div>
    <div class="modal-body" id="session-modal-body" style="padding:20px">Loading...</div>
  </div>
</div>

<!-- Cost Breakdown Modal -->
<div class="modal-overlay" id="cost-modal" onclick="if(event.target===this)closeCostModal()">
  <div class="modal">
    <div class="modal-header">
      <h2>Cost Breakdown</h2>
      <button class="modal-close" onclick="closeCostModal()">&times;</button>
    </div>
    <div class="modal-tabs">
      <div class="modal-tab active" data-tab="model" onclick="setCostTab('model')">By Model</div>
      <div class="modal-tab" data-tab="token" onclick="setCostTab('token')">By Token Type</div>
      <div class="modal-tab" data-tab="project" onclick="setCostTab('project')">By Project</div>
    </div>
    <div class="modal-body" id="cost-modal-body"></div>
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
// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let selectedRange = '30d';
let charts = {};

// ── Pricing (Anthropic API, April 2026) ────────────────────────────────────
const PRICING = {
  'claude-opus-4-6':   { input: 5.00,  output: 25.00, cache_write: 6.25, cache_read: 0.50 },
  'claude-opus-4-5':   { input: 5.00,  output: 25.00, cache_write: 6.25, cache_read: 0.50 },
  'claude-sonnet-4-6': { input: 3.00,  output: 15.00, cache_write: 3.75, cache_read: 0.30 },
  'claude-sonnet-4-5': { input: 3.00,  output: 15.00, cache_write: 3.75, cache_read: 0.30 },
  'claude-haiku-4-5':  { input: 1.00,  output:  5.00, cache_write: 1.25, cache_read: 0.10 },
  'claude-haiku-4-6':  { input: 1.00,  output:  5.00, cache_write: 1.25, cache_read: 0.10 },
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
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${m}">
      <input type="checkbox" value="${m}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      ${m}
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
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, turns: 0 };
    projMap[s.project].input  += s.input;
    projMap[s.project].output += s.output;
    projMap[s.project].turns  += s.turns;
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

  // Update cost modal data
  costModalData = { byModel, byProject, totals };

  renderStats(totals);
  renderDailyChart(daily);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  renderSessionsTable(filteredSessions);
  renderModelCostTable(byModel);
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
    { label: 'Est. Cost',      value: fmtCostBig(t.cost),          sub: 'click for breakdown', color: '#4ade80', click: 'openCostModal()' },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card${s.click ? ' clickable' : ''}" ${s.click ? `onclick="${s.click}"` : ''}>
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${s.value}</div>
      ${s.sub ? `<div class="sub">${s.sub}</div>` : ''}
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

// ── Sessions: sort + render + detail ──────────────────────────────────────
let sessionSortKey = 'last';
let sessionSortAsc = false;
let currentFilteredSessions = [];

function sortSessions(key) {
  if (sessionSortKey === key) { sessionSortAsc = !sessionSortAsc; }
  else { sessionSortKey = key; sessionSortAsc = key === 'session_id' || key === 'project' || key === 'model'; }
  // Update header arrows
  document.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('asc', 'desc');
    th.querySelector('.sort-arrow').innerHTML = '&#9650;';
  });
  const activeTh = document.querySelector(`th[data-key="${key}"]`);
  if (activeTh) {
    activeTh.classList.add(sessionSortAsc ? 'asc' : 'desc');
    activeTh.querySelector('.sort-arrow').innerHTML = sessionSortAsc ? '&#9650;' : '&#9660;';
  }
  renderSessionsTable(currentFilteredSessions);
}

function renderSessionsTable(sessions) {
  currentFilteredSessions = sessions;
  document.getElementById('sessions-count').textContent = `(${sessions.length})`;

  // Sort
  const sorted = [...sessions].sort((a, b) => {
    let va = a[sessionSortKey], vb = b[sessionSortKey];
    if (sessionSortKey === 'cost') {
      va = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation);
      vb = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation);
    }
    if (typeof va === 'string') { const c = va.localeCompare(vb); return sessionSortAsc ? c : -c; }
    return sessionSortAsc ? va - vb : vb - va;
  });

  document.getElementById('sessions-body').innerHTML = sorted.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr class="session-row" onclick="openSessionDetail('${s.session_id}')">
      <td class="muted" style="font-family:monospace">${s.session_id.slice(0,8)}&hellip;</td>
      <td>${s.project}</td>
      <td class="muted">${s.last}</td>
      <td class="muted">${s.duration_min}m</td>
      <td><span class="model-tag">${s.model}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

async function openSessionDetail(sid) {
  const modal = document.getElementById('session-modal');
  const body = document.getElementById('session-modal-body');
  modal.classList.add('open');
  body.innerHTML = '<div style="color:var(--muted);padding:20px">Loading session details...</div>';
  try {
    const resp = await fetch('/api/session/' + sid);
    const d = await resp.json();
    if (d.error) { body.innerHTML = '<div style="color:#f87171">' + d.error + '</div>'; return; }
    renderSessionDetail(d);
  } catch(e) {
    body.innerHTML = '<div style="color:#f87171">Failed to load session</div>';
  }
}

function closeSessionModal() {
  document.getElementById('session-modal').classList.remove('open');
}

function renderSessionDetail(d) {
  const body = document.getElementById('session-modal-body');
  // Escape HTML in activity text
  function esc(s) { const el = document.createElement('div'); el.textContent = s; return el.innerHTML; }

  const modalTitle = d.title ? d.title : 'Session ' + d.session_id.slice(0,8) + '\u2026';
  document.getElementById('session-modal-title').textContent = modalTitle;

  const totalIn = d.turns.reduce((s,t) => s + t.input, 0);
  const totalOut = d.turns.reduce((s,t) => s + t.output, 0);
  const totalCR = d.turns.reduce((s,t) => s + t.cache_read, 0);
  const totalCC = d.turns.reduce((s,t) => s + t.cache_creation, 0);
  const cost = calcCost(d.model, totalIn, totalOut, totalCR, totalCC);

  // Tool usage summary
  const toolCounts = {};
  for (const t of d.turns) { if (t.tool) toolCounts[t.tool] = (toolCounts[t.tool] || 0) + 1; }
  const topTools = Object.entries(toolCounts).sort((a,b) => b[1] - a[1]).slice(0, 10);

  const firstTs = (d.first || '').slice(0,16).replace('T',' ');
  const lastTs = (d.last || '').slice(0,16).replace('T',' ');

  // Build activity log HTML
  const activityHTML = (d.activity && d.activity.length)
    ? d.activity.map(a => {
        const roleClass = a.role === 'user' ? 'user' : a.role === 'assistant' ? 'assistant' : 'tool';
        const roleLabel = a.role === 'user' ? 'You' : a.role === 'assistant' ? 'Claude' : 'Tools';
        return `<div class="activity-entry ${roleClass}">
          <div class="activity-role ${roleClass}">${roleLabel}</div>
          <div class="activity-text">${esc(a.text)}</div>
        </div>`;
      }).join('')
    : '<div style="padding:20px;color:var(--muted)">Transcript not available (JSONL file may have been removed)</div>';

  body.innerHTML = `
    <div class="detail-header">
      <div class="detail-meta">
        <span><strong>Project:</strong> ${esc(d.project)}</span>
        <span><strong>Model:</strong> <span class="model-tag">${esc(d.model)}</span></span>
        <span><strong>Started:</strong> ${firstTs}</span>
        <span><strong>Last:</strong> ${lastTs}</span>
      </div>
      ${d.cwd ? `<div style="margin-top:6px;font-size:11px;color:var(--muted);font-family:monospace">${esc(d.cwd)}</div>` : ''}
    </div>
    <div class="detail-stats">
      <div class="detail-stat"><div class="label">Turns</div><div class="value">${d.turns.length}</div></div>
      <div class="detail-stat"><div class="label">Input</div><div class="value">${fmt(totalIn)}</div></div>
      <div class="detail-stat"><div class="label">Output</div><div class="value">${fmt(totalOut)}</div></div>
      <div class="detail-stat"><div class="label">Cache Read</div><div class="value">${fmt(totalCR)}</div></div>
      <div class="detail-stat"><div class="label">Cache Create</div><div class="value">${fmt(totalCC)}</div></div>
      <div class="detail-stat"><div class="label">Est. Cost</div><div class="value" style="color:var(--green)">${isBillable(d.model) ? fmtCost(cost) : 'n/a'}</div></div>
    </div>
    ${topTools.length ? '<div style="margin-bottom:16px"><div class="label" style="color:var(--muted);font-size:11px;text-transform:uppercase;margin-bottom:6px">Tools Used</div><div style="display:flex;flex-wrap:wrap;gap:4px">' + topTools.map(([t,c]) => `<span class="tool-tag">${esc(t)} (${c})</span>`).join('') + '</div></div>' : ''}
    <div class="detail-tabs">
      <div class="detail-tab active" onclick="switchDetailTab(this,'activity')">Activity</div>
      <div class="detail-tab" onclick="switchDetailTab(this,'tokens')">Token Details</div>
    </div>
    <div class="detail-tab-content active" id="tab-activity">
      <div class="activity-log">${activityHTML}</div>
    </div>
    <div class="detail-tab-content" id="tab-tokens">
      <div class="turn-list">
      <table>
        <thead><tr><th>#</th><th>Time</th><th>Tool</th><th>Input</th><th>Output</th><th>Cache R</th><th>Cache W</th><th>Cost</th></tr></thead>
        <tbody>${d.turns.map((t, i) => {
          const tc = calcCost(d.model, t.input, t.output, t.cache_read, t.cache_creation);
          return `<tr>
            <td class="muted">${i+1}</td>
            <td class="muted">${(t.timestamp||'').slice(11,19)}</td>
            <td>${t.tool ? '<span class="tool-tag">'+esc(t.tool)+'</span>' : '<span class="muted">-</span>'}</td>
            <td class="num">${fmt(t.input)}</td>
            <td class="num">${fmt(t.output)}</td>
            <td class="num">${fmt(t.cache_read)}</td>
            <td class="num">${fmt(t.cache_creation)}</td>
            <td class="cost">${isBillable(d.model) ? fmtCost(tc) : ''}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>
      </div>
    </div>
  `;
}

function switchDetailTab(el, tabId) {
  el.closest('.detail-tabs').querySelectorAll('.detail-tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  el.closest('.modal-body').querySelectorAll('.detail-tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-' + tabId).classList.add('active');
}

function renderModelCostTable(byModel) {
  document.getElementById('model-cost-body').innerHTML = byModel.map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    const costCell = isBillable(m.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td><span class="model-tag">${m.model}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(m.input)}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

// ── Cost Modal ────────────────────────────────────────────────────────────
let costModalTab = 'model';
let costModalData = { byModel: [], byProject: [], totals: {} };

function openCostModal() {
  document.getElementById('cost-modal').classList.add('open');
  renderCostTab();
}
function closeCostModal() {
  document.getElementById('cost-modal').classList.remove('open');
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeCostModal(); closeSessionModal(); } });
function setCostTab(tab) {
  costModalTab = tab;
  document.querySelectorAll('.modal-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  renderCostTab();
}

function renderCostTab() {
  const body = document.getElementById('cost-modal-body');
  const { byModel, byProject, totals } = costModalData;
  const totalCost = totals.cost || 0;

  if (costModalTab === 'model') {
    const rows = byModel.filter(m => isBillable(m.model)).map(m => {
      const c = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
      const pct = totalCost > 0 ? (c / totalCost * 100) : 0;
      return { name: m.model, cost: c, pct, tokens: m.input + m.output + m.cache_read + m.cache_creation, turns: m.turns };
    }).sort((a, b) => b.cost - a.cost);

    body.innerHTML = renderCostBar(rows, MODEL_COLORS) + rows.map(r => `
      <div class="cost-row">
        <div>
          <div class="name"><span class="model-tag">${r.name}</span></div>
          <div class="detail">${fmt(r.tokens)} tokens &middot; ${fmt(r.turns)} turns</div>
        </div>
        <div style="text-align:right">
          <span class="amount">${fmtCost(r.cost)}</span><span class="pct">${r.pct.toFixed(1)}%</span>
        </div>
      </div>
    `).join('') + renderCostTotal(totalCost);

  } else if (costModalTab === 'token') {
    const parts = [];
    for (const m of byModel) {
      if (!isBillable(m.model)) continue;
      const p = getPricing(m.model);
      if (!p) continue;
      parts.push({
        input:    m.input * p.input / 1e6,
        output:   m.output * p.output / 1e6,
        cache_read: m.cache_read * p.cache_read / 1e6,
        cache_creation: m.cache_creation * p.cache_write / 1e6,
      });
    }
    const agg = { input: 0, output: 0, cache_read: 0, cache_creation: 0 };
    for (const p of parts) { agg.input += p.input; agg.output += p.output; agg.cache_read += p.cache_read; agg.cache_creation += p.cache_creation; }

    const tokenRows = [
      { name: 'Input',          cost: agg.input,          color: TOKEN_COLORS.input,          tokens: byModel.reduce((s,m) => s+m.input, 0) },
      { name: 'Output',         cost: agg.output,         color: TOKEN_COLORS.output,         tokens: byModel.reduce((s,m) => s+m.output, 0) },
      { name: 'Cache Read',     cost: agg.cache_read,     color: TOKEN_COLORS.cache_read,     tokens: byModel.reduce((s,m) => s+m.cache_read, 0) },
      { name: 'Cache Creation', cost: agg.cache_creation, color: TOKEN_COLORS.cache_creation, tokens: byModel.reduce((s,m) => s+m.cache_creation, 0) },
    ].sort((a, b) => b.cost - a.cost);

    const barColors = tokenRows.map(r => r.color);
    body.innerHTML = renderCostBar(tokenRows, barColors) + tokenRows.map(r => {
      const pct = totalCost > 0 ? (r.cost / totalCost * 100) : 0;
      return `<div class="cost-row">
        <div>
          <div class="name">${r.name}</div>
          <div class="detail">${fmt(r.tokens)} tokens</div>
        </div>
        <div style="text-align:right">
          <span class="amount">${fmtCost(r.cost)}</span><span class="pct">${pct.toFixed(1)}%</span>
        </div>
      </div>`;
    }).join('') + renderCostTotal(totalCost);

  } else if (costModalTab === 'project') {
    const cutoff = getRangeCutoff(selectedRange);
    const filteredSessions = rawData.sessions_all.filter(s =>
      selectedModels.has(s.model) && (!cutoff || s.last_date >= cutoff)
    );
    const projMap = {};
    for (const s of filteredSessions) {
      if (!projMap[s.project]) projMap[s.project] = { project: s.project, cost: 0, tokens: 0, sessions: 0 };
      projMap[s.project].cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
      projMap[s.project].tokens += s.input + s.output + s.cache_read + s.cache_creation;
      projMap[s.project].sessions++;
    }
    const projRows = Object.values(projMap).sort((a, b) => b.cost - a.cost).slice(0, 15);

    body.innerHTML = renderCostBar(projRows, MODEL_COLORS) + projRows.map(r => {
      const pct = totalCost > 0 ? (r.cost / totalCost * 100) : 0;
      const displayName = r.project.length > 35 ? '\u2026' + r.project.slice(-33) : r.project;
      return `<div class="cost-row">
        <div>
          <div class="name">${displayName}</div>
          <div class="detail">${r.sessions} sessions &middot; ${fmt(r.tokens)} tokens</div>
        </div>
        <div style="text-align:right">
          <span class="amount">${fmtCost(r.cost)}</span><span class="pct">${pct.toFixed(1)}%</span>
        </div>
      </div>`;
    }).join('') + renderCostTotal(totalCost);
  }
}

function renderCostBar(rows, colors) {
  const total = rows.reduce((s, r) => s + r.cost, 0);
  if (total <= 0) return '';
  const segs = rows.map((r, i) => {
    const w = (r.cost / total * 100);
    const color = colors[i % colors.length];
    return `<div class="cost-bar-seg" style="width:${w}%;background:${color}"></div>`;
  }).join('');
  return `<div class="cost-bar">${segs}</div><div style="height:12px"></div>`;
}

function renderCostTotal(total) {
  return `<div class="cost-total"><span>Total</span><span class="amount">${fmtCostBig(total)}</span></div>`;
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      document.body.innerHTML = '<div style="padding:40px;color:#f87171">' + d.error + '</div>';
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


def get_session_detail(session_id, db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    turns = conn.execute("""
        SELECT timestamp, model, input_tokens, output_tokens,
               cache_read_tokens, cache_creation_tokens, tool_name, cwd
        FROM turns WHERE session_id = ?
        ORDER BY timestamp
    """, (session_id,)).fetchall()
    session = conn.execute("""
        SELECT session_id, project_name, first_timestamp, last_timestamp,
               total_input_tokens, total_output_tokens,
               total_cache_read, total_cache_creation, model, turn_count
        FROM sessions WHERE session_id = ?
    """, (session_id,)).fetchone()
    conn.close()
    if not session:
        return {"error": "Session not found"}

    activity = _extract_activity(session_id)

    # Derive title from first user message
    title = ""
    for a in activity:
        if a["role"] == "user":
            title = a["text"][:120]
            if len(a["text"]) > 120:
                title += "..."
            break

    # Get cwd from first turn
    cwd = ""
    if turns:
        cwd = turns[0]["cwd"] or ""

    return {
        "session_id": session["session_id"],
        "project": session["project_name"] or "unknown",
        "model": session["model"] or "unknown",
        "first": session["first_timestamp"],
        "last": session["last_timestamp"],
        "cwd": cwd,
        "title": title,
        "turns": [{
            "timestamp": r["timestamp"],
            "model": r["model"] or "unknown",
            "input": r["input_tokens"] or 0,
            "output": r["output_tokens"] or 0,
            "cache_read": r["cache_read_tokens"] or 0,
            "cache_creation": r["cache_creation_tokens"] or 0,
            "tool": r["tool_name"] or "",
            "cwd": r["cwd"] or "",
        } for r in turns],
        "activity": activity,
    }


def _extract_activity(session_id):
    """Extract human-readable activity log from the JSONL transcript."""
    projects_dir = Path.home() / ".claude" / "projects"
    # Find the JSONL file — filename is session_id.jsonl under any project dir
    matches = list(projects_dir.glob(f"*/{session_id}.jsonl"))
    if not matches:
        return []
    transcript = matches[0]
    activity = []
    try:
        with open(transcript) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                entry = _parse_activity_line(d)
                if entry:
                    activity.append(entry)
    except OSError:
        pass
    # Collapse consecutive tool entries into one
    collapsed = []
    for entry in activity:
        if entry["role"] == "tool" and collapsed and collapsed[-1]["role"] == "tool":
            collapsed[-1]["text"] += ", " + entry["text"]
        else:
            collapsed.append(entry)
    return collapsed


def _parse_activity_line(d):
    """Parse a single JSONL record into an activity entry, or None."""
    t = d.get("type")

    if t in ("human", "user"):
        text = _extract_user_text(d)
        if text:
            return {"role": "user", "text": text}

    elif t == "assistant":
        msg = d.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            return None
        # Collect text and tool_use from this message
        texts = []
        tools = []
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                txt = c.get("text", "").strip()
                if txt and len(txt) > 5:
                    texts.append(txt)
            elif c.get("type") == "tool_use":
                name = c.get("name", "")
                inp = c.get("input", {})
                tools.append(_summarize_tool(name, inp))
        if texts:
            return {"role": "assistant", "text": texts[0][:500]}
        if tools:
            return {"role": "tool", "text": ", ".join(tools)}

    return None


def _extract_user_text(d):
    """Pull the user's actual message text, filtering out system noise."""
    msg = d.get("message", {})
    content = msg.get("content", [])
    if isinstance(content, str):
        # Skip system/scheduled/command messages
        stripped = content.strip()
        if any(stripped.startswith(p) for p in ("<system", "<scheduled", "<local-command", "<command-name", "<task-notification")):
            return None
        if "<system-reminder>" in stripped or "<command-name>" in stripped:
            return None
        if len(stripped) > 500:
            return stripped[:500] + "..."
        return stripped if stripped else None
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                txt = c.get("text", "").strip()
                if not txt or any(txt.startswith(p) for p in ("<system", "<local-command", "<command-name", "<task-notification")):
                    continue
                if "<system-reminder>" in txt:
                    # Try to extract just the user part before/after system tags
                    import re
                    cleaned = re.sub(r"<system-reminder>.*?</system-reminder>", "", txt, flags=re.DOTALL).strip()
                    if cleaned and not cleaned.startswith("<"):
                        return cleaned[:500] if len(cleaned) > 500 else cleaned
                    continue
                if len(txt) > 500:
                    return txt[:500] + "..."
                return txt
    return None


def _summarize_tool(name, inp):
    """One-line summary of a tool call."""
    if name in ("Read", "Grep", "Glob"):
        path = inp.get("file_path") or inp.get("path") or inp.get("pattern", "")
        return f"{name}({_short_path(path)})"
    if name == "Edit":
        path = inp.get("file_path", "")
        return f"Edit({_short_path(path)})"
    if name == "Write":
        path = inp.get("file_path", "")
        return f"Write({_short_path(path)})"
    if name == "Bash":
        cmd = inp.get("command", "")
        return f"Bash({cmd[:60]}{'...' if len(cmd) > 60 else ''})"
    if name == "Agent":
        desc = inp.get("description", "")
        return f"Agent({desc[:50]})"
    return name


def _short_path(path):
    """Shorten a file path to last 2 components."""
    if not path:
        return ""
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else path


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
            data = get_session_detail(sid)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()


def serve(port=8080):
    server = HTTPServer(("localhost", port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
