"""
cli.py - Command-line interface for the Claude Code usage dashboard.

Commands:
  scan      - Scan JSONL files and update the database
  today     - Print today's usage summary
  stats     - Print all-time usage statistics
  dashboard - Scan + open browser + start dashboard server
"""

import os
import sys
import sqlite3
from pathlib import Path
from datetime import datetime, date

DB_PATH = Path.home() / ".claude" / "usage.db"

PRICING = {
    "claude-opus-4-6":   {"input":  5.00, "output": 25.00},
    "claude-opus-4-5":   {"input":  5.00, "output": 25.00},
    "claude-sonnet-4-6": {"input":  3.00, "output": 15.00},
    "claude-sonnet-4-5": {"input":  3.00, "output": 15.00},
    "claude-haiku-4-5":  {"input":  1.00, "output":  5.00},
    "claude-haiku-4-6":  {"input":  1.00, "output":  5.00},
}

def get_pricing(model):
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if model.startswith(key):
            return PRICING[key]
    # Substring fallback: match model family by keyword
    m = model.lower()
    if "opus" in m:
        return PRICING["claude-opus-4-6"]
    if "sonnet" in m:
        return PRICING["claude-sonnet-4-6"]
    if "haiku" in m:
        return PRICING["claude-haiku-4-5"]
    return None

def calc_cost(model, inp, out, cache_read, cache_creation):
    p = get_pricing(model)
    if not p:
        return 0.0
    return (
        inp          * p["input"]  / 1_000_000 +
        out          * p["output"] / 1_000_000 +
        cache_read   * p["input"]  * 0.10 / 1_000_000 +
        cache_creation * p["input"] * 1.25 / 1_000_000
    )

def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(c):
    return f"${c:.4f}"

def hr(char="-", width=60):
    print(char * width)

def require_db():
    if not DB_PATH.exists():
        print("Database not found. Run: python cli.py scan")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(projects_dir=None):
    from scanner import scan
    scan(projects_dir=Path(projects_dir) if projects_dir else None)


def cmd_today():
    conn = require_db()
    conn.row_factory = sqlite3.Row
    today = date.today().isoformat()

    rows = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (today,)).fetchall()

    sessions = conn.execute("""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
    """, (today,)).fetchone()

    print()
    hr()
    print(f"  Today's Usage  ({today})")
    hr()

    if not rows:
        print("  No usage recorded today.")
        print()
        return

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0

    for r in rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"  {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"  {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions today:   {sessions['cnt']}")
    print(f"  Cache read:       {fmt(total_cr)}")
    print(f"  Cache creation:   {fmt(total_cc)}")
    hr()
    print()
    conn.close()


def cmd_stats():
    conn = require_db()
    conn.row_factory = sqlite3.Row

    # Session-level info (count, date range)
    session_info = conn.execute("""
        SELECT
            COUNT(*)                  as sessions,
            MIN(first_timestamp)      as first,
            MAX(last_timestamp)       as last
        FROM sessions
    """).fetchone()

    # All-time totals from turns (more accurate — per-turn model attribution)
    totals = conn.execute("""
        SELECT
            SUM(input_tokens)             as inp,
            SUM(output_tokens)            as out,
            SUM(cache_read_tokens)        as cr,
            SUM(cache_creation_tokens)    as cc,
            COUNT(*)                      as turns
        FROM turns
    """).fetchone()

    # By model from turns (each turn has the actual model used)
    by_model = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns,
            COUNT(DISTINCT session_id) as sessions
        FROM turns
        GROUP BY model
        ORDER BY inp + out DESC
    """).fetchall()

    # Top 5 projects from turns (join with sessions for project name)
    top_projects = conn.execute("""
        SELECT
            COALESCE(s.project_name, 'unknown') as project_name,
            SUM(t.input_tokens)  as inp,
            SUM(t.output_tokens) as out,
            COUNT(*)             as turns,
            COUNT(DISTINCT t.session_id) as sessions
        FROM turns t
        LEFT JOIN sessions s ON t.session_id = s.session_id
        GROUP BY s.project_name
        ORDER BY inp + out DESC
        LIMIT 5
    """).fetchall()

    # Daily average (last 30 days)
    daily_avg = conn.execute("""
        SELECT
            AVG(daily_inp) as avg_inp,
            AVG(daily_out) as avg_out,
            AVG(daily_cost) as avg_cost
        FROM (
            SELECT
                substr(timestamp, 1, 10) as day,
                SUM(input_tokens) as daily_inp,
                SUM(output_tokens) as daily_out,
                0.0 as daily_cost
            FROM turns
            WHERE timestamp >= datetime('now', '-30 days')
            GROUP BY day
        )
    """).fetchone()

    # Build total cost across all models
    total_cost = sum(
        calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        for r in by_model
    )

    print()
    hr("=")
    print("  Claude Code Usage - All-Time Statistics")
    hr("=")

    first_date = (session_info["first"] or "")[:10]
    last_date = (session_info["last"] or "")[:10]
    print(f"  Period:           {first_date} to {last_date}")
    print(f"  Total sessions:   {session_info['sessions'] or 0:,}")
    print(f"  Total turns:      {fmt(totals['turns'] or 0)}")
    print()
    print(f"  Input tokens:     {fmt(totals['inp'] or 0):<12}  (raw prompt tokens)")
    print(f"  Output tokens:    {fmt(totals['out'] or 0):<12}  (generated tokens)")
    print(f"  Cache read:       {fmt(totals['cr'] or 0):<12}  (90% cheaper than input)")
    print(f"  Cache creation:   {fmt(totals['cc'] or 0):<12}  (25% premium on input)")
    print()
    print(f"  Est. total cost:  ${total_cost:.4f}")
    hr()

    print("  By Model:")
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        print(f"    {r['model']:<30}  sessions={r['sessions']:<4}  turns={fmt(r['turns'] or 0):<6}  "
              f"in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print("  Top Projects:")
    for r in top_projects:
        print(f"    {(r['project_name'] or 'unknown'):<40}  sessions={r['sessions']:<3}  "
              f"turns={fmt(r['turns'] or 0):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")

    if daily_avg["avg_inp"]:
        hr()
        print("  Daily Average (last 30 days):")
        print(f"    Input:   {fmt(int(daily_avg['avg_inp'] or 0))}")
        print(f"    Output:  {fmt(int(daily_avg['avg_out'] or 0))}")

    hr("=")
    print()
    conn.close()


def cmd_dashboard(projects_dir=None):
    import webbrowser
    import threading
    import time

    print("Running scan first...")
    cmd_scan(projects_dir=projects_dir)

    print("\nStarting dashboard server...")
    from dashboard import serve

    host = os.environ.get("HOST", "localhost")
    port = int(os.environ.get("PORT", "8080"))

    def open_browser():
        time.sleep(1.0)
        webbrowser.open(f"http://{host}:{port}")

    t = threading.Thread(target=open_browser, daemon=True)
    t.start()
    serve(host=host, port=port)


# ── Theme command ──────────────────────────────────────────────────────────────

def cmd_theme():
    import urllib.request
    import urllib.error
    from dashboard import BUNDLED_THEMES, AWESOME_CATALOG, THEMES_DIR

    sub = sys.argv[2] if len(sys.argv) > 2 else None

    if sub == "list":
        installed = {t["id"] for t in BUNDLED_THEMES}
        THEMES_DIR.mkdir(parents=True, exist_ok=True)
        for f in THEMES_DIR.glob("*.json"):
            try:
                t = json.load(open(f))
                if "id" in t:
                    installed.add(t["id"])
            except Exception:
                pass
        all_ids = {c["id"] for c in AWESOME_CATALOG} | installed
        print(f"\n{'ID':<20} {'NAME':<22} {'CATEGORY':<28} STATUS")
        print("-" * 80)
        for entry in sorted(AWESOME_CATALOG + [t for t in BUNDLED_THEMES if t["id"] not in {c["id"] for c in AWESOME_CATALOG}], key=lambda x: x["name"]):
            status = "installed" if entry["id"] in installed else "available"
            print(f"{entry['id']:<20} {entry['name']:<22} {entry['category']:<28} {status}")
        print()

    elif sub == "add":
        theme_id = sys.argv[3] if len(sys.argv) > 3 else None
        if not theme_id:
            print("Usage: python cli.py theme add <id>")
            print("Run 'python cli.py theme list' to see available theme IDs.")
            sys.exit(1)

        # Check it's in the catalog
        catalog_entry = next((c for c in AWESOME_CATALOG if c["id"] == theme_id), None)
        if not catalog_entry:
            print(f"Unknown theme '{theme_id}'. Run 'python cli.py theme list' to see valid IDs.")
            sys.exit(1)

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("ANTHROPIC_API_KEY environment variable is required to generate themes.")
            print("Export it and re-run: export ANTHROPIC_API_KEY=sk-ant-...")
            sys.exit(1)

        # Fetch the DESIGN.md from awesome-design-md
        design_url = f"https://raw.githubusercontent.com/VoltAgent/awesome-design-md/main/design-md/{theme_id}/README.md"
        print(f"Fetching design system for '{theme_id}'...")
        try:
            with urllib.request.urlopen(design_url, timeout=15) as r:
                design_md = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            print(f"Could not fetch design file (HTTP {e.code}). The theme may have a different path in the repo.")
            sys.exit(1)
        except Exception as e:
            print(f"Network error: {e}")
            sys.exit(1)

        print("Generating CSS with Claude API...")
        prompt = f"""You are converting a design system description into CSS custom properties for a data dashboard.

Given the DESIGN.md below, produce a CSS :root {{ }} block with EXACTLY these variables:
  --bg            page background color
  --card          card / surface background
  --border        border color (prefer rgba with low opacity)
  --text          primary text color
  --muted         secondary / muted text color (prefer rgba)
  --accent        primary interactive / accent color
  --green         positive number color (for financial figures)
  --shadow        box-shadow value for cards
  --chart-label   color for chart axis labels (must be legible on --bg)
  --chart-grid    color for chart grid lines (subtle, low opacity)

Output ONLY the :root {{ }} block — no explanation, no markdown fences, no other text.

DESIGN.md:
{design_md[:8000]}"""

        payload = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}]
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                result = json.loads(r.read())
        except Exception as e:
            print(f"API error: {e}")
            sys.exit(1)

        css = result["content"][0]["text"].strip()
        if not css.startswith(":root"):
            # Try to extract :root block if wrapped in markdown
            import re
            m = re.search(r":root\s*\{[^}]+\}", css, re.DOTALL)
            css = m.group(0) if m else css

        # Extract preview colors from the CSS
        import re
        def extract_var(css_text, var):
            m = re.search(rf"--{var}\s*:\s*([^;]+);", css_text)
            return m.group(1).strip() if m else "#888888"

        preview = {
            "bg":     extract_var(css, "bg"),
            "card":   extract_var(css, "card"),
            "text":   extract_var(css, "text"),
            "accent": extract_var(css, "accent"),
            "muted":  extract_var(css, "border"),
        }

        theme = {
            "id":       theme_id,
            "name":     catalog_entry["name"],
            "category": catalog_entry["category"],
            "dark":     False,
            "bundled":  False,
            "preview":  preview,
            "css":      css,
        }

        THEMES_DIR.mkdir(parents=True, exist_ok=True)
        out = THEMES_DIR / f"{theme_id}.json"
        out.write_text(json.dumps(theme, indent=2))
        print(f"✓ Theme '{catalog_entry['name']}' installed to {out}")
        print("  Reload the dashboard to see it in Appearance.")

    elif sub == "remove":
        theme_id = sys.argv[3] if len(sys.argv) > 3 else None
        if not theme_id:
            print("Usage: python cli.py theme remove <id>")
            sys.exit(1)
        f = THEMES_DIR / f"{theme_id}.json"
        if f.exists():
            f.unlink()
            print(f"Removed theme '{theme_id}'.")
        else:
            print(f"Theme '{theme_id}' is not installed (or is a bundled theme and cannot be removed).")

    else:
        print("""
Theme management:

  python cli.py theme list               List all installed and available themes
  python cli.py theme add <id>           Generate and install a theme (requires ANTHROPIC_API_KEY)
  python cli.py theme remove <id>        Remove a user-installed theme

Example:
  python cli.py theme add spotify
  python cli.py theme add tesla
""")


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
Claude Code Usage Dashboard

Usage:
  python cli.py scan [--projects-dir PATH]       Scan JSONL files and update database
  python cli.py today                            Show today's usage summary
  python cli.py stats                            Show all-time statistics
  python cli.py dashboard [--projects-dir PATH]  Scan + start dashboard
  python cli.py theme <list|add|remove>          Manage UI themes
"""

COMMANDS = {
    "scan": cmd_scan,
    "today": cmd_today,
    "stats": cmd_stats,
    "dashboard": cmd_dashboard,
    "theme": cmd_theme,
}

def parse_projects_dir(args):
    """Extract --projects-dir value from argument list."""
    for i, arg in enumerate(args):
        if arg == "--projects-dir" and i + 1 < len(args):
            return args[i + 1]
    return None

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(USAGE)
        sys.exit(0)

    command = sys.argv[1]

    if command == "theme":
        cmd_theme()
    else:
        projects_dir = parse_projects_dir(sys.argv[2:])
        if command in ("scan", "dashboard") and projects_dir:
            COMMANDS[command](projects_dir=projects_dir)
        else:
            COMMANDS[command]()
