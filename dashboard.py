"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import mimetypes
import os
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

DB_PATH = Path.home() / ".claude" / "usage.db"


def get_dashboard_data(db_path=None):
    db_path = db_path or DB_PATH
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

    # ── Per-day project aggregates (client filters by range + model) ─────────
    # Grouped by session_id as well so the client can count distinct sessions
    # after applying range/model filters without shipping raw turn rows.
    project_usage_rows = conn.execute("""
        SELECT
            substr(t.timestamp, 1, 10)                   as day,
            COALESCE(t.model, 'unknown')                 as model,
            COALESCE(NULLIF(s.project_name, ''), 'unknown') as project,
            COALESCE(s.git_branch, '')                   as branch,
            t.session_id                                 as session_id,
            SUM(t.input_tokens)                          as input,
            SUM(t.output_tokens)                         as output,
            SUM(t.cache_read_tokens)                     as cache_read,
            SUM(t.cache_creation_tokens)                 as cache_creation,
            COUNT(*)                                     as turns
        FROM turns t
        LEFT JOIN sessions s ON s.session_id = t.session_id
        WHERE t.timestamp IS NOT NULL AND length(t.timestamp) >= 10
        GROUP BY 1, 2, 3, 4, 5
        ORDER BY 1, 2, 3, 4
    """).fetchall()

    project_usage_by_day = [{
        "day":            r["day"],
        "model":          r["model"],
        "project":        r["project"],
        "branch":         r["branch"] or "",
        "session_id":     r["session_id"] or "",
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in project_usage_rows]

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
            "session_id":    r["session_id"],
            "session_label": r["session_id"][:8],
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
        "project_usage_by_day": project_usage_by_day,
        "sessions_all":    sessions_all,
        "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "dist"

MISSING_BUILD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Claude Code Analytics</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #070b10; color: #e7edf6; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    main { width: min(560px, calc(100vw - 32px)); border: 1px solid #253241; border-radius: 8px; padding: 24px; background: #111923; }
    h1 { margin: 0 0 8px; font-size: 20px; }
    p { margin: 0 0 14px; color: #a8b5c7; line-height: 1.5; }
    code { border: 1px solid #253241; border-radius: 5px; padding: 2px 6px; background: #0d151e; color: #49c7d7; }
  </style>
</head>
<body>
  <main>
    <h1>Dashboard build missing</h1>
    <p>The Python API server is running, but the React dashboard has not been built yet.</p>
    <p>Run <code>npm install</code> and <code>npm run build</code>, then reload this page.</p>
  </main>
</body>
</html>
"""


def _safe_static_path(request_path, static_dir=None):
    static_dir = static_dir or STATIC_DIR
    clean_path = request_path.lstrip("/")
    if clean_path in ("", "index.html"):
        candidate = static_dir / "index.html"
    else:
        candidate = static_dir / clean_path

    try:
        resolved = candidate.resolve()
        root = static_dir.resolve()
    except OSError:
        return None

    if resolved == root or root not in resolved.parents:
        return None
    return resolved


def _content_type(path):
    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed == "text/javascript":
        return "application/javascript"
    if guessed:
        if guessed.startswith("text/"):
            return f"{guessed}; charset=utf-8"
        return guessed
    return "application/octet-stream"


def _read_index_html(static_dir=None):
    static_dir = static_dir or STATIC_DIR
    index_file = static_dir / "index.html"
    if index_file.exists():
        return index_file.read_bytes()
    return MISSING_BUILD_HTML.encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/data":
            data = get_dashboard_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ("/", "/index.html"):
            body = _read_index_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        static_path = _safe_static_path(path)
        if static_path is None or not static_path.is_file():
            self.send_response(404)
            self.end_headers()
            return

        body = static_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _content_type(static_path))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/rescan":
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
