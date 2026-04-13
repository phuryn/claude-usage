"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import os
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

DB_PATH    = Path.home() / ".claude" / "usage.db"
THEMES_DIR = Path.home() / ".claude" / "claude-usage" / "themes"

# ── Bundled themes ─────────────────────────────────────────────────────────────
BUNDLED_THEMES = [
    {
        "id": "apple", "name": "Apple", "category": "Enterprise & Consumer",
        "dark": False, "bundled": True,
        "preview": {"bg": "#f5f5f7", "card": "#ffffff", "text": "#1d1d1f", "accent": "#0071e3", "muted": "rgba(0,0,0,0.10)"},
        "css": """:root {
  --bg: #f5f5f7; --card: #ffffff; --border: rgba(0,0,0,0.08);
  --text: #1d1d1f; --muted: rgba(0,0,0,0.48); --accent: #0071e3;
  --green: #1c7a3a; --shadow: 0px 2px 12px rgba(0,0,0,0.08);
  --card-radius: 14px; --card-border: none;
  --chart-label: rgba(0,0,0,0.48); --chart-grid: rgba(0,0,0,0.06);
  --chart-1: rgba(0,113,227,0.8); --chart-2: rgba(88,86,214,0.8);
  --chart-3: rgba(52,199,89,0.8); --chart-4: rgba(255,159,10,0.75);
}"""
    },
    {
        "id": "linear", "name": "Linear", "category": "Developer Tools",
        "dark": True, "bundled": True,
        "preview": {"bg": "#0f0f10", "card": "#1a1a1b", "text": "#e8e8e8", "accent": "#5e6ad2", "muted": "rgba(255,255,255,0.12)"},
        "css": """:root {
  --bg: #0f0f10; --card: #1a1a1b; --border: rgba(255,255,255,0.08);
  --text: #e8e8e8; --muted: rgba(255,255,255,0.4); --accent: #5e6ad2;
  --green: #4ade80; --shadow: none;
  --card-radius: 8px; --card-border: 1px solid rgba(255,255,255,0.08);
  --chart-label: rgba(255,255,255,0.4); --chart-grid: rgba(255,255,255,0.07);
  --chart-1: rgba(94,106,210,0.9); --chart-2: rgba(139,92,246,0.85);
  --chart-3: rgba(74,222,128,0.85); --chart-4: rgba(251,191,36,0.85);
}"""
    },
    {
        "id": "vercel", "name": "Vercel", "category": "Developer Tools",
        "dark": True, "bundled": True,
        "preview": {"bg": "#000000", "card": "#111111", "text": "#ffffff", "accent": "#ffffff", "muted": "rgba(255,255,255,0.12)"},
        "css": """:root {
  --bg: #000000; --card: #111111; --border: rgba(255,255,255,0.1);
  --text: #ffffff; --muted: rgba(255,255,255,0.4); --accent: #ffffff;
  --green: #50e3c2; --shadow: none;
  --card-radius: 4px; --card-border: 1px solid rgba(255,255,255,0.12);
  --chart-label: rgba(255,255,255,0.4); --chart-grid: rgba(255,255,255,0.08);
  --chart-1: rgba(255,255,255,0.85); --chart-2: rgba(160,160,160,0.75);
  --chart-3: rgba(80,227,194,0.85); --chart-4: rgba(200,200,200,0.6);
}"""
    },
    {
        "id": "notion", "name": "Notion", "category": "Design & Productivity",
        "dark": False, "bundled": True,
        "preview": {"bg": "#ffffff", "card": "#f7f7f5", "text": "#37352f", "accent": "#2eaadc", "muted": "rgba(55,53,47,0.10)"},
        "css": """:root {
  --bg: #ffffff; --card: #f7f7f5; --border: rgba(55,53,47,0.09);
  --text: #37352f; --muted: rgba(55,53,47,0.5); --accent: #2eaadc;
  --green: #0f7b6c; --shadow: none;
  --card-radius: 6px; --card-border: 1px solid rgba(55,53,47,0.12);
  --chart-label: rgba(55,53,47,0.5); --chart-grid: rgba(55,53,47,0.08);
  --chart-1: rgba(46,170,220,0.85); --chart-2: rgba(103,195,140,0.85);
  --chart-3: rgba(15,123,108,0.85); --chart-4: rgba(235,168,69,0.85);
}"""
    },
    {
        "id": "stripe", "name": "Stripe", "category": "Infrastructure & Cloud",
        "dark": False, "bundled": True,
        "preview": {"bg": "#f6f9fc", "card": "#ffffff", "text": "#0a2540", "accent": "#635bff", "muted": "rgba(10,37,64,0.10)"},
        "css": """:root {
  --bg: #f6f9fc; --card: #ffffff; --border: rgba(0,0,0,0.1);
  --text: #0a2540; --muted: rgba(10,37,64,0.5); --accent: #635bff;
  --green: #09825d; --shadow: 0px 2px 5px rgba(0,0,0,0.08), 0px 1px 1px rgba(0,0,0,0.05);
  --card-radius: 8px; --card-border: 1px solid rgba(10,37,64,0.1);
  --chart-label: rgba(10,37,64,0.5); --chart-grid: rgba(10,37,64,0.07);
  --chart-1: rgba(99,91,255,0.85); --chart-2: rgba(0,122,255,0.8);
  --chart-3: rgba(9,130,93,0.85); --chart-4: rgba(255,149,0,0.8);
}"""
    },
]

# ── Catalog of all themes available from awesome-design-md ─────────────────────
AWESOME_CATALOG = [
    # AI & ML
    {"id": "claude",      "name": "Claude",       "category": "AI & ML"},
    {"id": "cohere",      "name": "Cohere",        "category": "AI & ML"},
    {"id": "elevenlabs",  "name": "ElevenLabs",    "category": "AI & ML"},
    {"id": "minimax",     "name": "Minimax",       "category": "AI & ML"},
    {"id": "mistral",     "name": "Mistral AI",    "category": "AI & ML"},
    {"id": "ollama",      "name": "Ollama",        "category": "AI & ML"},
    {"id": "replicate",   "name": "Replicate",     "category": "AI & ML"},
    {"id": "runwayml",    "name": "RunwayML",      "category": "AI & ML"},
    {"id": "together",    "name": "Together AI",   "category": "AI & ML"},
    # Developer Tools
    {"id": "cursor",      "name": "Cursor",        "category": "Developer Tools"},
    {"id": "expo",        "name": "Expo",          "category": "Developer Tools"},
    {"id": "lovable",     "name": "Lovable",       "category": "Developer Tools"},
    {"id": "mintlify",    "name": "Mintlify",      "category": "Developer Tools"},
    {"id": "posthog",     "name": "PostHog",       "category": "Developer Tools"},
    {"id": "raycast",     "name": "Raycast",       "category": "Developer Tools"},
    {"id": "resend",      "name": "Resend",        "category": "Developer Tools"},
    {"id": "sentry",      "name": "Sentry",        "category": "Developer Tools"},
    {"id": "supabase",    "name": "Supabase",      "category": "Developer Tools"},
    {"id": "superhuman",  "name": "Superhuman",    "category": "Developer Tools"},
    {"id": "warp",        "name": "Warp",          "category": "Developer Tools"},
    {"id": "zapier",      "name": "Zapier",        "category": "Developer Tools"},
    # Infrastructure & Cloud
    {"id": "clickhouse",  "name": "ClickHouse",    "category": "Infrastructure & Cloud"},
    {"id": "composio",    "name": "Composio",      "category": "Infrastructure & Cloud"},
    {"id": "hashicorp",   "name": "HashiCorp",     "category": "Infrastructure & Cloud"},
    {"id": "mongodb",     "name": "MongoDB",       "category": "Infrastructure & Cloud"},
    {"id": "sanity",      "name": "Sanity",        "category": "Infrastructure & Cloud"},
    # Design & Productivity
    {"id": "airtable",    "name": "Airtable",      "category": "Design & Productivity"},
    {"id": "cal",         "name": "Cal.com",       "category": "Design & Productivity"},
    {"id": "clay",        "name": "Clay",          "category": "Design & Productivity"},
    {"id": "figma",       "name": "Figma",         "category": "Design & Productivity"},
    {"id": "framer",      "name": "Framer",        "category": "Design & Productivity"},
    {"id": "intercom",    "name": "Intercom",      "category": "Design & Productivity"},
    {"id": "miro",        "name": "Miro",          "category": "Design & Productivity"},
    {"id": "pinterest",   "name": "Pinterest",     "category": "Design & Productivity"},
    {"id": "webflow",     "name": "Webflow",       "category": "Design & Productivity"},
    # Fintech & Crypto
    {"id": "coinbase",    "name": "Coinbase",      "category": "Fintech & Crypto"},
    {"id": "kraken",      "name": "Kraken",        "category": "Fintech & Crypto"},
    {"id": "revolut",     "name": "Revolut",       "category": "Fintech & Crypto"},
    {"id": "wise",        "name": "Wise",          "category": "Fintech & Crypto"},
    # Enterprise & Consumer
    {"id": "airbnb",      "name": "Airbnb",        "category": "Enterprise & Consumer"},
    {"id": "ibm",         "name": "IBM",           "category": "Enterprise & Consumer"},
    {"id": "nvidia",      "name": "NVIDIA",        "category": "Enterprise & Consumer"},
    {"id": "spacex",      "name": "SpaceX",        "category": "Enterprise & Consumer"},
    {"id": "spotify",     "name": "Spotify",       "category": "Enterprise & Consumer"},
    {"id": "uber",        "name": "Uber",          "category": "Enterprise & Consumer"},
    # Car Brands
    {"id": "bmw",         "name": "BMW",           "category": "Car Brands"},
    {"id": "ferrari",     "name": "Ferrari",       "category": "Car Brands"},
    {"id": "lamborghini", "name": "Lamborghini",   "category": "Car Brands"},
    {"id": "renault",     "name": "Renault",       "category": "Car Brands"},
    {"id": "tesla",       "name": "Tesla",         "category": "Car Brands"},
]


def get_themes():
    """Return installed themes: bundled first, then user-generated from THEMES_DIR."""
    themes = {t["id"]: dict(t) for t in BUNDLED_THEMES}
    THEMES_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(THEMES_DIR.glob("*.json")):
        try:
            t = json.loads(f.read_text())
            if "id" in t and "css" in t:
                t.setdefault("bundled", False)
                themes[t["id"]] = t
        except Exception:
            pass
    return list(themes.values())


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


GALLERY_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Appearance — Claude Usage Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #f5f5f7; font-family: -apple-system, "SF Pro Text", "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 14px; color: #1d1d1f; }

  .g-header { position: sticky; top: 0; z-index: 100; background: rgba(255,255,255,0.85); backdrop-filter: saturate(180%) blur(20px); -webkit-backdrop-filter: saturate(180%) blur(20px); border-bottom: 1px solid rgba(0,0,0,0.08); padding: 0 32px; height: 52px; display: flex; align-items: center; gap: 16px; }
  .g-back { background: none; border: none; cursor: pointer; font-size: 17px; color: #0071e3; padding: 0 8px 0 0; letter-spacing: -0.374px; }
  .g-back:hover { text-decoration: underline; }
  .g-title { font-size: 17px; font-weight: 600; letter-spacing: -0.374px; flex: 1; }
  .g-search { background: rgba(0,0,0,0.06); border: none; border-radius: 8px; padding: 6px 12px; font-size: 13px; width: 220px; color: #1d1d1f; outline: none; letter-spacing: -0.12px; }
  .g-search:focus { background: rgba(0,0,0,0.09); }

  .g-modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 1000; align-items: center; justify-content: center; }
  .g-modal-overlay.visible { display: flex; }
  .g-modal { background: #fff; border-radius: 16px; padding: 32px 40px; text-align: center; box-shadow: 0 20px 60px rgba(0,0,0,0.2); min-width: 280px; }
  .g-modal-icon { font-size: 40px; margin-bottom: 12px; }
  .g-modal-title { font-size: 17px; font-weight: 600; letter-spacing: -0.374px; margin-bottom: 8px; }
  .g-modal-sub { font-size: 13px; color: rgba(0,0,0,0.48); letter-spacing: -0.12px; }

  .g-body { max-width: 1200px; margin: 0 auto; padding: 40px 32px 64px; }
  .g-section-header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 20px; }
  .g-section-title { font-size: 21px; font-weight: 600; letter-spacing: -0.28px; }
  .g-section-hint { font-size: 13px; color: rgba(0,0,0,0.48); letter-spacing: -0.12px; }
  .g-divider { margin: 48px 0 32px; border: none; border-top: 1px solid rgba(0,0,0,0.08); }

  .g-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 20px; }

  .theme-card { background: #ffffff; border-radius: 14px; overflow: hidden; box-shadow: 0px 2px 12px rgba(0,0,0,0.08); transition: transform 0.18s, box-shadow 0.18s; display: flex; flex-direction: column; }
  .theme-card:hover { transform: translateY(-3px); box-shadow: 0px 8px 28px rgba(0,0,0,0.13); }
  .theme-card.is-active { box-shadow: 0px 0px 0px 2px #0071e3, 0px 8px 28px rgba(0,113,227,0.18); }

  .theme-preview { height: 152px; padding: 10px; overflow: hidden; position: relative; }
  .theme-preview.unavailable { filter: grayscale(1); opacity: 0.45; }
  .prev-shell { height: 100%; border-radius: 8px; overflow: hidden; display: flex; flex-direction: column; }
  .prev-topbar { height: 18px; display: flex; align-items: center; padding: 0 8px; gap: 5px; flex-shrink: 0; }
  .prev-topbar-title { height: 5px; width: 48px; border-radius: 3px; opacity: 0.5; }
  .prev-topbar-dot { width: 12px; height: 12px; border-radius: 50%; margin-left: auto; }
  .prev-filterbar { height: 14px; display: flex; align-items: center; padding: 0 8px; gap: 3px; flex-shrink: 0; }
  .prev-pill { height: 7px; border-radius: 4px; }
  .prev-body { flex: 1; padding: 6px 8px; display: flex; flex-direction: column; gap: 5px; overflow: hidden; }
  .prev-stats { display: flex; gap: 4px; }
  .prev-stat { flex: 1; border-radius: 5px; height: 22px; }
  .prev-chart { flex: 1; border-radius: 5px; padding: 5px 6px; display: flex; align-items: flex-end; gap: 2px; }
  .prev-bar { flex: 1; border-radius: 2px; }

  .theme-info { padding: 14px 16px 16px; border-top: 1px solid rgba(0,0,0,0.06); display: flex; flex-direction: column; gap: 8px; }
  .theme-name { font-size: 14px; font-weight: 600; letter-spacing: -0.224px; }
  .theme-meta { display: flex; align-items: center; gap: 6px; }
  .theme-category { font-size: 11px; color: rgba(0,0,0,0.48); letter-spacing: -0.08px; }
  .theme-badge { font-size: 10px; padding: 1px 7px; border-radius: 980px; letter-spacing: -0.08px; }
  .badge-active { background: rgba(0,113,227,0.1); color: #0071e3; }
  .badge-bundled { background: rgba(0,0,0,0.06); color: rgba(0,0,0,0.48); }
  .btn-apply { background: #0071e3; color: #fff; border: none; border-radius: 6px; padding: 6px 16px; font-size: 13px; font-weight: 500; cursor: pointer; letter-spacing: -0.12px; transition: background 0.15s; }
  .btn-apply:hover { background: #0077ed; }
  .btn-applied { background: rgba(0,113,227,0.1); color: #0071e3; border: none; border-radius: 6px; padding: 6px 16px; font-size: 13px; font-weight: 500; cursor: default; letter-spacing: -0.12px; }
  .btn-generate { background: transparent; color: rgba(0,0,0,0.4); border: 1px solid rgba(0,0,0,0.15); border-radius: 6px; padding: 6px 16px; font-size: 13px; cursor: default; letter-spacing: -0.12px; }
  .theme-cmd { font-family: "SF Mono", ui-monospace, monospace; font-size: 10px; color: rgba(0,0,0,0.35); margin-top: 2px; }

  .g-empty { color: rgba(0,0,0,0.4); font-size: 14px; padding: 24px 0; }

  @media (max-width: 600px) { .g-body { padding: 24px 16px; } .g-header { padding: 0 16px; } }
</style>
</head>
<body>
<div class="g-header">
  <button class="g-back" onclick="window.close()">← Back</button>
  <div class="g-title">Appearance</div>
  <input class="g-search" type="search" placeholder="Search themes…" oninput="filterThemes(this.value)">
</div>
<div class="g-modal-overlay" id="applied-modal">
  <div class="g-modal">
    <div class="g-modal-icon">✓</div>
    <div class="g-modal-title" id="modal-theme-name">Theme applied</div>
    <div class="g-modal-sub" id="modal-countdown">Closing in 3…</div>
  </div>
</div>
<div class="g-body">
  <div class="g-section-header">
    <div class="g-section-title">Installed</div>
  </div>
  <div class="g-grid" id="installed-grid"><div class="g-empty">Loading…</div></div>

  <hr class="g-divider">

  <div class="g-section-header">
    <div class="g-section-title">Available</div>
    <div class="g-section-hint">Run <code>python cli.py theme add &lt;id&gt;</code> to generate and install</div>
  </div>
  <div class="g-grid" id="available-grid"></div>
</div>

<script>
const CATALOG = __CATALOG_JSON__;
let allInstalled = [];
let activeThemeId = localStorage.getItem('dashboard-theme-id') || 'apple';

async function init() {
  const resp = await fetch('/api/themes');
  allInstalled = await resp.json();
  render('');
}

function render(query) {
  const q = query.toLowerCase();
  const installedIds = new Set(allInstalled.map(t => t.id));

  const matchInstalled = allInstalled.filter(t =>
    !q || t.name.toLowerCase().includes(q) || t.category.toLowerCase().includes(q)
  );
  const matchAvailable = CATALOG.filter(t =>
    !installedIds.has(t.id) &&
    (!q || t.name.toLowerCase().includes(q) || t.category.toLowerCase().includes(q))
  );

  document.getElementById('installed-grid').innerHTML =
    matchInstalled.length ? matchInstalled.map(t => cardHTML(t, true)).join('') : '<div class="g-empty">No installed themes match.</div>';
  document.getElementById('available-grid').innerHTML =
    matchAvailable.length ? matchAvailable.map(t => cardHTML(t, false)).join('') : '<div class="g-empty">No available themes match.</div>';
}

function filterThemes(q) { render(q); }

function cardHTML(t, installed) {
  const isActive = t.id === activeThemeId;
  const preview = t.preview ? previewHTML(t) : unavailablePreviewHTML(t);
  const badge = isActive
    ? '<span class="theme-badge badge-active">Active</span>'
    : (t.bundled ? '<span class="theme-badge badge-bundled">Bundled</span>' : '');
  const btn = !installed
    ? `<button class="btn-generate" disabled>Generate</button><div class="theme-cmd">python cli.py theme add ${t.id}</div>`
    : isActive
    ? `<button class="btn-applied">Applied ✓</button>`
    : `<button class="btn-apply" onclick="applyTheme('${t.id}')">Apply</button>`;

  return `<div class="theme-card${isActive ? ' is-active' : ''}" id="card-${t.id}">
    <div class="theme-preview${!installed ? ' unavailable' : ''}">${preview}</div>
    <div class="theme-info">
      <div>
        <div class="theme-name">${t.name}</div>
        <div class="theme-meta"><span class="theme-category">${t.category}</span>${badge}</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:4px">${btn}</div>
    </div>
  </div>`;
}

function previewHTML(t) {
  const p = t.preview;
  const bars = [40, 70, 55, 85, 60, 90, 75].map(h =>
    `<div class="prev-bar" style="height:${h}%;background:${p.accent};opacity:0.75"></div>`
  ).join('');
  return `<div class="prev-shell" style="background:${p.bg}">
    <div class="prev-topbar" style="background:${p.card}">
      <div class="prev-topbar-title" style="background:${p.text}"></div>
      <div class="prev-topbar-dot" style="background:${p.accent}"></div>
    </div>
    <div class="prev-filterbar" style="background:${p.card};border-bottom:1px solid ${p.muted}">
      <div class="prev-pill" style="width:28px;background:${p.accent}"></div>
      <div class="prev-pill" style="width:20px;background:${p.muted}"></div>
      <div class="prev-pill" style="width:20px;background:${p.muted}"></div>
    </div>
    <div class="prev-body" style="background:${p.bg}">
      <div class="prev-stats">
        <div class="prev-stat" style="background:${p.card}"></div>
        <div class="prev-stat" style="background:${p.card}"></div>
        <div class="prev-stat" style="background:${p.card}"></div>
      </div>
      <div class="prev-chart" style="background:${p.card}">${bars}</div>
    </div>
  </div>`;
}

function unavailablePreviewHTML(t) {
  return `<div class="prev-shell" style="background:#e8e8e8">
    <div class="prev-topbar" style="background:#d0d0d0"></div>
    <div class="prev-body" style="background:#e8e8e8">
      <div class="prev-stats">
        <div class="prev-stat" style="background:#d0d0d0"></div>
        <div class="prev-stat" style="background:#d0d0d0"></div>
        <div class="prev-stat" style="background:#d0d0d0"></div>
      </div>
      <div class="prev-chart" style="background:#d0d0d0;height:60px"></div>
    </div>
  </div>`;
}

function applyTheme(id) {
  const t = allInstalled.find(x => x.id === id);
  if (!t) return;
  localStorage.setItem('dashboard-theme-id', id);
  localStorage.setItem('dashboard-theme-css', t.css);
  activeThemeId = id;
  if (window.opener && !window.opener.closed) {
    try { window.opener.setTheme(t.css, id); } catch(e) {}
  }
  render(document.querySelector('.g-search').value);

  // Show confirmation modal then auto-close
  const modal = document.getElementById('applied-modal');
  const countdownEl = document.getElementById('modal-countdown');
  document.getElementById('modal-theme-name').textContent = t.name + ' applied';
  modal.classList.add('visible');
  let secs = 3;
  countdownEl.textContent = 'Closing in ' + secs + '\u2026';
  const iv = setInterval(() => {
    secs--;
    if (secs > 0) {
      countdownEl.textContent = 'Closing in ' + secs + '\u2026';
    } else {
      clearInterval(iv);
      window.close();
    }
  }, 1000);
}

init();
</script>
</body>
</html>
"""

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style id="active-theme">:root {
    --bg: #f5f5f7; --card: #ffffff; --border: rgba(0,0,0,0.08);
    --text: #1d1d1f; --muted: rgba(0,0,0,0.48); --accent: #0071e3;
    --green: #1c7a3a; --shadow: 0px 2px 12px rgba(0,0,0,0.08);
    --card-radius: 14px; --card-border: none;
    --chart-label: rgba(0,0,0,0.48); --chart-grid: rgba(0,0,0,0.06);
    --chart-1: rgba(0,113,227,0.8); --chart-2: rgba(88,86,214,0.8);
    --chart-3: rgba(52,199,89,0.8); --chart-4: rgba(255,159,10,0.75);
  }</style>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, "SF Pro Text", "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 14px; letter-spacing: -0.224px; }

  header { position: sticky; top: 0; z-index: 100; background: rgba(255,255,255,0.85); backdrop-filter: saturate(180%) blur(20px); -webkit-backdrop-filter: saturate(180%) blur(20px); border-bottom: 1px solid var(--border); padding: 0 24px; height: 48px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 17px; font-weight: 600; color: var(--text); letter-spacing: -0.374px; }
  header .meta { color: var(--muted); font-size: 12px; letter-spacing: -0.12px; }
  .appearance-btn { background: transparent; border: 1px solid var(--border); border-radius: 6px; color: var(--muted); font-size: 12px; padding: 4px 12px; cursor: pointer; letter-spacing: -0.12px; transition: all 0.15s; white-space: nowrap; }
  .appearance-btn:hover { border-color: var(--accent); color: var(--accent); }
  #rescan-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  #rescan-btn:hover { color: var(--text); border-color: var(--accent); }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  #filter-bar { background: rgba(255,255,255,0.85); backdrop-filter: saturate(180%) blur(20px); -webkit-backdrop-filter: saturate(180%) blur(20px); border-bottom: 1px solid var(--border); padding: 10px 24px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .filter-label { font-size: 12px; font-weight: 600; letter-spacing: -0.12px; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: rgba(0,0,0,0.12); flex-shrink: 0; }
  #model-checkboxes { display: flex; flex-wrap: wrap; gap: 6px; }
  .model-cb-label { display: flex; align-items: center; gap: 5px; padding: 4px 12px; border-radius: 980px; border: 1px solid rgba(0,0,0,0.12); cursor: pointer; font-size: 12px; color: var(--muted); letter-spacing: -0.12px; transition: all 0.15s; user-select: none; }
  .model-cb-label:hover { border-color: var(--accent); color: var(--accent); }
  .model-cb-label.checked { background: rgba(0,113,227,0.08); border-color: var(--accent); color: var(--accent); font-weight: 500; }
  .model-cb-label input { display: none; }
  .filter-btn { padding: 4px 12px; border-radius: 980px; border: 1px solid rgba(0,0,0,0.12); background: transparent; color: var(--muted); font-size: 12px; cursor: pointer; white-space: nowrap; letter-spacing: -0.12px; transition: all 0.15s; }
  .filter-btn:hover { border-color: var(--accent); color: var(--accent); }
  .range-group { display: flex; border: 1px solid rgba(0,0,0,0.12); border-radius: 8px; overflow: hidden; flex-shrink: 0; background: var(--card); }
  .range-btn { padding: 5px 14px; background: transparent; border: none; border-right: 1px solid rgba(0,0,0,0.08); color: var(--muted); font-size: 12px; cursor: pointer; letter-spacing: -0.12px; transition: background 0.15s, color 0.15s; }
  .range-btn:last-child { border-right: none; }
  .range-btn:hover { background: rgba(0,113,227,0.05); color: var(--text); }
  .range-btn.active { background: var(--accent); color: #ffffff; font-weight: 500; }

  .container { max-width: 1200px; margin: 0 auto; padding: 32px 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border-radius: var(--card-radius); border: var(--card-border); padding: 20px; box-shadow: var(--shadow); }
  .stat-card .label { color: var(--muted); font-size: 12px; letter-spacing: -0.12px; margin-bottom: 8px; font-weight: 500; }
  .stat-card .value { font-size: 24px; font-weight: 600; letter-spacing: -0.28px; color: var(--text); }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; letter-spacing: -0.08px; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  .chart-card { background: var(--card); border-radius: var(--card-radius); border: var(--card-border); padding: 20px; box-shadow: var(--shadow); }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 13px; font-weight: 600; color: var(--text); letter-spacing: -0.12px; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 240px; }
  .chart-wrap.tall { height: 300px; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  .sort-icon { font-size: 9px; opacity: 0.8; }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(0,113,227,0.03); }
  .model-tag { display: inline-block; padding: 2px 8px; border-radius: 980px; font-size: 11px; background: rgba(0,113,227,0.08); color: var(--accent); letter-spacing: -0.08px; }
  .cost { color: var(--green); font-family: "SF Mono", ui-monospace, monospace; font-size: 12px; }
  .cost-na { color: var(--muted); font-family: "SF Mono", ui-monospace, monospace; font-size: 11px; }
  .num { font-family: "SF Mono", ui-monospace, monospace; font-size: 12px; }
  .muted { color: var(--muted); }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .section-header .section-title { margin-bottom: 0; }
  .export-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 3px 10px; border-radius: 5px; cursor: pointer; font-size: 11px; }
  .export-btn:hover { color: var(--text); border-color: var(--accent); }
  .table-card { background: var(--card); border-radius: var(--card-radius); border: var(--card-border); padding: 20px; margin-bottom: 24px; overflow-x: auto; box-shadow: var(--shadow); }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 8px; }
  .footer-content { max-width: 1200px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; letter-spacing: -0.12px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--accent); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } }
</style>
</head>
<body>
<header>
  <h1>Claude Code Usage Dashboard</h1>
  <div style="display:flex;align-items:center;gap:12px">
    <div class="meta" id="meta">Loading...</div>
    <button id="rescan-btn" onclick="triggerRescan()" title="Rebuild the database from scratch by re-scanning all JSONL files. Use if data looks stale or costs seem wrong.">&#x21bb; Rescan</button>
    <button class="appearance-btn" onclick="window.open('/themes','_blank')">Appearance</button>
  </div>
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
// ── Theme switching ────────────────────────────────────────────────────────
function setTheme(css, id) {
  document.getElementById('active-theme').textContent = css;
  if (id)  localStorage.setItem('dashboard-theme-id',  id);
  if (css) localStorage.setItem('dashboard-theme-css', css);
  if (rawData) applyFilter();
}

(function restoreTheme() {
  const css = localStorage.getItem('dashboard-theme-css');
  if (css) document.getElementById('active-theme').textContent = css;
})();

function chartColors() {
  const s = getComputedStyle(document.documentElement);
  return {
    label: s.getPropertyValue('--chart-label').trim() || 'rgba(0,0,0,0.48)',
    grid:  s.getPropertyValue('--chart-grid').trim()  || 'rgba(0,0,0,0.06)',
    c1:    s.getPropertyValue('--chart-1').trim()     || 'rgba(0,113,227,0.8)',
    c2:    s.getPropertyValue('--chart-2').trim()     || 'rgba(88,86,214,0.8)',
    c3:    s.getPropertyValue('--chart-3').trim()     || 'rgba(52,199,89,0.8)',
    c4:    s.getPropertyValue('--chart-4').trim()     || 'rgba(255,159,10,0.75)',
  };
}

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
function tokenColors() {
  const c = chartColors();
  return { input: c.c1, output: c.c2, cache_read: c.c3, cache_creation: c.c4 };
}
function modelColors() {
  const c = chartColors();
  return [c.c1, c.c2, c.c3, c.c4, '#ff3b30', '#ff2d55', '#64d2ff', '#30d158'];
}

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
        { label: 'Input',          data: daily.map(d => d.input),          backgroundColor: tokenColors().input,          stack: 'tokens' },
        { label: 'Output',         data: daily.map(d => d.output),         backgroundColor: tokenColors().output,         stack: 'tokens' },
        { label: 'Cache Read',     data: daily.map(d => d.cache_read),     backgroundColor: tokenColors().cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', data: daily.map(d => d.cache_creation), backgroundColor: tokenColors().cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: chartColors().label, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: chartColors().label, maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: chartColors().grid } },
        y: { ticks: { color: chartColors().label, callback: v => fmt(v) }, grid: { color: chartColors().grid } },
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
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: modelColors(), borderWidth: 2, borderColor: '#ffffff' }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: chartColors().label, boxWidth: 12, font: { size: 11 } } },
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
        { label: 'Input',  data: top.map(p => p.input),  backgroundColor: tokenColors().input },
        { label: 'Output', data: top.map(p => p.output), backgroundColor: tokenColors().output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: chartColors().label, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: chartColors().label, callback: v => fmt(v) }, grid: { color: chartColors().grid } },
        y: { ticks: { color: chartColors().label, font: { size: 11 } }, grid: { color: chartColors().grid } },
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

        elif self.path == "/api/themes":
            body = json.dumps(get_themes()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/themes":
            catalog_json = json.dumps(AWESOME_CATALOG)
            html = GALLERY_TEMPLATE.replace("__CATALOG_JSON__", catalog_json)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
    server = HTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
