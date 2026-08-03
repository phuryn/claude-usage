"""Tests for dashboard.py - API endpoint and data retrieval."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from scanner import get_db, init_db, upsert_sessions, insert_turns
from dashboard import get_dashboard_data, DashboardHandler, HTML_TEMPLATE

try:
    from http.server import HTTPServer
except ImportError:
    HTTPServer = None


class TestGetDashboardData(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        # Insert sample data
        sessions = [{
            "session_id": "sess-abc123", "project_name": "user/myproject",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 5000, "total_output_tokens": 2000,
            "total_cache_read": 500, "total_cache_creation": 200,
            "turn_count": 10,
        }]
        upsert_sessions(conn, sessions)
        turns = [
            {
                "session_id": "sess-abc123", "timestamp": "2026-04-08T09:30:00Z",
                "model": "claude-sonnet-4-6", "input_tokens": 500,
                "output_tokens": 200, "cache_read_tokens": 50,
                "cache_creation_tokens": 20, "tool_name": None, "cwd": "/tmp",
            },
            {
                "session_id": "sess-abc123", "timestamp": "2026-04-08T14:15:00Z",
                "model": "claude-sonnet-4-6", "input_tokens": 300,
                "output_tokens": 150, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
            },
        ]
        insert_turns(conn, turns)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_returns_valid_structure(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("all_models", data)
        self.assertIn("daily_by_model", data)
        self.assertIn("sessions_all", data)
        self.assertIn("generated_at", data)

    def test_models_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("claude-sonnet-4-6", data["all_models"])

    def test_sessions_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertEqual(len(data["sessions_all"]), 1)
        session = data["sessions_all"][0]
        self.assertEqual(session["project"], "user/myproject")
        self.assertEqual(session["model"], "claude-sonnet-4-6")
        self.assertEqual(session["input"], 5000)

    def test_daily_by_model_populated(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertGreater(len(data["daily_by_model"]), 0)
        day = data["daily_by_model"][0]
        self.assertIn("day", day)
        self.assertIn("model", day)
        self.assertIn("input", day)

    def test_missing_db_returns_error(self):
        data = get_dashboard_data(db_path=Path("/nonexistent/path/usage.db"))
        self.assertIn("error", data)

    def test_session_id_sent_in_full(self):
        # The API returns the full session id; the table truncates it for
        # display client-side, but the CSV export needs the whole value.
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(session["session_id"], "sess-abc123")

    def test_session_duration_calculated(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        # 1 hour = 60 minutes
        self.assertEqual(session["duration_min"], 60.0)

    def test_hourly_by_model_present(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("hourly_by_model", data)
        self.assertIsInstance(data["hourly_by_model"], list)

    def test_hourly_by_model_buckets_by_utc_hour(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["hourly_by_model"]
        # Two turns at UTC 09:30 and 14:15 → two hour buckets
        by_hour = {r["hour"]: r for r in rows}
        self.assertIn(9, by_hour)
        self.assertIn(14, by_hour)
        self.assertEqual(by_hour[9]["turns"], 1)
        self.assertEqual(by_hour[9]["output"], 200)
        self.assertEqual(by_hour[14]["turns"], 1)
        self.assertEqual(by_hour[14]["output"], 150)

    def test_hourly_by_model_carries_day_and_model(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["hourly_by_model"]
        self.assertTrue(all("day" in r and "model" in r for r in rows))
        self.assertTrue(all(r["model"] == "claude-sonnet-4-6" for r in rows))
        self.assertTrue(all(r["day"] == "2026-04-08" for r in rows))


class TestEmptyStringModelNormalization(unittest.TestCase):
    """Regression: turns with model='' (empty string) must group as 'unknown'.
    COALESCE(model, 'unknown') alone returns '' because empty string isn't NULL;
    NULLIF(model, '') is needed first."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-empty", "project_name": "u/p",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T09:05:00Z",
            "git_branch": "", "model": "",
            "total_input_tokens": 100, "total_output_tokens": 50,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "sess-empty", "timestamp": "2026-04-08T09:05:00Z",
            "model": "", "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": "/tmp",
        }])
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_all_models_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        self.assertIn("unknown", data["all_models"])
        self.assertNotIn("", data["all_models"])

    def test_daily_by_model_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        models = {r["model"] for r in data["daily_by_model"]}
        self.assertIn("unknown", models)
        self.assertNotIn("", models)

    def test_hourly_by_model_contains_unknown_not_empty(self):
        data = get_dashboard_data(db_path=self.db_path)
        models = {r["model"] for r in data["hourly_by_model"]}
        self.assertIn("unknown", models)
        self.assertNotIn("", models)


class TestMixedNullAndEmptyModel(unittest.TestCase):
    """Regression: a mix of model=NULL and model='' rows must collapse into a
    SINGLE 'unknown' group across all aggregations. Without `GROUP BY
    COALESCE(NULLIF(model, ''), 'unknown')` (matching the SELECT expression),
    SQLite groups by raw value and emits two distinct 'unknown' rows."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-mix", "project_name": "u/p",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "", "model": "",
            "total_input_tokens": 200, "total_output_tokens": 100,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 2,
        }])
        # Insert one turn with model='' and one with model=NULL on the same day.
        # Use raw INSERT for the NULL row because insert_turns() requires the
        # model key to exist (would error on missing key, not on None).
        insert_turns(conn, [{
            "session_id": "sess-mix", "timestamp": "2026-04-08T09:00:00Z",
            "model": "", "input_tokens": 100, "output_tokens": 50,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": "/tmp",
        }])
        conn.execute("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens,
                tool_name, cwd)
            VALUES ('sess-mix', '2026-04-08T09:30:00Z', NULL, 100, 50, 0, 0, NULL, '/tmp')
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_all_models_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        unknowns = [m for m in data["all_models"] if m == "unknown"]
        self.assertEqual(len(unknowns), 1, f"got duplicate 'unknown' rows: {data['all_models']}")

    def test_daily_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        unknown_rows = [r for r in data["daily_by_model"] if r["model"] == "unknown"]
        # One day, one model bucket
        self.assertEqual(len(unknown_rows), 1, f"got {unknown_rows}")
        self.assertEqual(unknown_rows[0]["turns"], 2)
        self.assertEqual(unknown_rows[0]["input"], 200)

    def test_hourly_collapses_to_single_unknown(self):
        data = get_dashboard_data(db_path=self.db_path)
        # Both turns are in UTC hour 9 — must be one row, not two
        hour9 = [r for r in data["hourly_by_model"]
                 if r["hour"] == 9 and r["model"] == "unknown"]
        self.assertEqual(len(hour9), 1, f"got {hour9}")
        self.assertEqual(hour9[0]["turns"], 2)


class TestNonBillableModelFallback(unittest.TestCase):
    """Regression: when the user has only non-billable models (e.g. gemma, glm,
    local LLMs) — or all turns lack a model field — the default model selection
    must fall back to ALL models so the dashboard isn't blank."""

    def test_readurlmodels_fallback_in_html_template(self):
        # The fallback logic is JS; we assert the source contains the guard so
        # a future refactor doesn't silently remove it.
        self.assertIn("billable.length ? billable : allModels", HTML_TEMPLATE)


class TestDashboardHTTP(unittest.TestCase):
    """Integration test: start server and make HTTP requests."""

    @classmethod
    def setUpClass(cls):
        # Redirect DB_PATH + projects dirs to a tempdir so /api/rescan
        # writes to a throwaway DB and scans a throwaway transcript dir
        # instead of the user's real ~/.claude/usage.db and transcripts.
        import dashboard as _d
        import scanner as _s
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        tmp_projects = tmp / "projects"
        tmp_projects.mkdir()
        cls._patches = {
            (_d, "DB_PATH"):                (_d.DB_PATH,                tmp / "usage.db"),
            (_s, "DB_PATH"):                (_s.DB_PATH,                tmp / "usage.db"),
            (_s, "PROJECTS_DIR"):           (_s.PROJECTS_DIR,           tmp_projects),
            (_s, "DEFAULT_PROJECTS_DIRS"):  (_s.DEFAULT_PROJECTS_DIRS,  [tmp_projects]),
        }
        for (mod, name), (_orig, new) in cls._patches.items():
            setattr(mod, name, new)

        cls.server = HTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        for (mod, name), (orig, _new) in cls._patches.items():
            setattr(mod, name, orig)
        cls._tmpdir.cleanup()

    def test_index_returns_html(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_index_with_query_string_returns_html(self):
        # Regression: ?range=... and ?models=... must not 404. The dashboard
        # itself rewrites the URL with these params via history.replaceState,
        # so anything that reloads or bookmarks the page hits this path.
        for qs in ("?range=all", "?range=30d&models=claude-opus-4-7"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/{qs}") as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn(b"Claude Code Usage", resp.read())

    def test_api_data_with_query_string(self):
        # /api/data is fetched without query parameters today, but the route
        # should be tolerant if any are tacked on (e.g. cache-busting).
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/data?_=cachebust"
        ) as resp:
            self.assertEqual(resp.status, 200)

    def test_api_data_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/data"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            # Should have expected keys (or error if no DB)
            self.assertTrue("all_models" in data or "error" in data)

    def test_api_rescan_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/rescan"
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertIn("new", data)
            self.assertIn("updated", data)
            self.assertIn("skipped", data)

    def test_api_rescan_is_non_destructive(self):
        # Regression (#138): /api/rescan must NOT wipe the DB. usage.db is the
        # only durable store of history once Claude Code prunes old transcripts
        # (cleanupPeriodDays), so a rescan with nothing left on disk must keep
        # the existing rows. Seed history that has no corresponding JSONL file
        # (the projects dir is empty), rescan, and assert it survives.
        import dashboard as _d
        db_path = _d.DB_PATH
        conn = get_db(db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "pruned-sess", "project_name": "user/oldproject",
            "first_timestamp": "2026-01-01T09:00:00Z",
            "last_timestamp": "2026-01-01T10:00:00Z",
            "git_branch": "main", "model": "claude-opus-4-8",
            "total_input_tokens": 1000, "total_output_tokens": 400,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "pruned-sess", "timestamp": "2026-01-01T09:30:00Z",
            "model": "claude-opus-4-8", "input_tokens": 1000,
            "output_tokens": 400, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "tool_name": None, "cwd": "/tmp",
            "message_id": "msg-pruned-1",
        }])
        conn.commit()
        conn.close()

        url = f"http://127.0.0.1:{self.port}/api/rescan"
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)

        conn = sqlite3.connect(db_path)
        try:
            turn_count = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE session_id = 'pruned-sess'"
            ).fetchone()[0]
            sess_count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE session_id = 'pruned-sess'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(turn_count, 1, "rescan must not delete existing turns")
        self.assertEqual(sess_count, 1, "rescan must not delete existing sessions")

    def test_404_for_unknown_path(self):
        url = f"http://127.0.0.1:{self.port}/nonexistent"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_index_injects_app_config(self):
        # do_GET must substitute the __APP_CONFIG_JSON__ placeholder with a real
        # JSON object (version + surface). The raw placeholder must never reach
        # the browser, or window.APP_CONFIG would be a syntax error.
        from scanner import VERSION
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            body = resp.read().decode("utf-8")
        self.assertNotIn("__APP_CONFIG_JSON__", body)
        self.assertIn("window.APP_CONFIG =", body)
        self.assertIn(VERSION, body)
        # The HTTP test server keeps the default surface ("web").
        self.assertIn('"surface": "web"', body)


class TestHTMLTemplate(unittest.TestCase):
    def test_template_is_valid_html(self):
        self.assertIn("<!DOCTYPE html>", HTML_TEMPLATE)
        self.assertIn("</html>", HTML_TEMPLATE)

    def test_template_has_esc_function(self):
        """Verify XSS protection is present (PR #10)."""
        self.assertIn("function esc(", HTML_TEMPLATE)

    def test_template_has_chart_js(self):
        self.assertIn("chart.js", HTML_TEMPLATE.lower())

    def test_template_has_substring_matching(self):
        """Verify getPricing falls back to substring match for unknown models."""
        self.assertIn("m.includes('opus')", HTML_TEMPLATE)
        self.assertIn("m.includes('sonnet')", HTML_TEMPLATE)
        self.assertIn("m.includes('haiku')", HTML_TEMPLATE)

    def test_unknown_models_return_null(self):
        """Verify getPricing returns null for non-Anthropic models."""
        self.assertIn("return null;", HTML_TEMPLATE)

    def test_hourly_chart_canvas_present(self):
        """Hourly distribution chart has a canvas + TZ toggle."""
        self.assertIn('id="chart-hourly"', HTML_TEMPLATE)
        self.assertIn('data-tz="local"', HTML_TEMPLATE)
        self.assertIn('data-tz="utc"', HTML_TEMPLATE)

    def test_hourly_peak_hour_constants(self):
        """Peak-hour set covers UTC 12–17 (Mon–Fri 05:00–11:00 PT)."""
        self.assertIn('PEAK_HOURS_UTC', HTML_TEMPLATE)
        self.assertIn('[12, 13, 14, 15, 16, 17]', HTML_TEMPLATE)

    def test_today_range_option_present(self):
        """The 'Today' range is wired into RANGE_LABELS, RANGE_TICKS,
        getRangeBounds, and the filter-bar range dropdown."""
        self.assertIn("<option value=\"today\">", HTML_TEMPLATE)
        self.assertIn("'today': 'Today'", HTML_TEMPLATE)
        self.assertIn("'today': 1", HTML_TEMPLATE)
        # Bounds case: today returns start === end === today's ISO date
        self.assertIn("range === 'today'", HTML_TEMPLATE)

    def test_app_config_placeholder_present(self):
        """The head carries the server-substituted config placeholder and the
        footer carries the element + JS the version/update feature drives."""
        self.assertIn("__APP_CONFIG_JSON__", HTML_TEMPLATE)
        self.assertIn("window.APP_CONFIG", HTML_TEMPLATE)
        self.assertIn('id="footer-meta"', HTML_TEMPLATE)
        self.assertIn("function initFooterMeta(", HTML_TEMPLATE)
        self.assertIn("function checkForUpdate(", HTML_TEMPLATE)

    def test_update_check_is_surface_gated(self):
        """The GitHub update check and the extension promo are web-only: both
        guard on surface !== 'vscode' so the embedded panel stays quiet."""
        self.assertIn("APP_CONFIG.surface !== 'vscode'", HTML_TEMPLATE)
        # The update check hits GitHub's public releases API, not any usage data.
        self.assertIn("api.github.com/repos/phuryn/claude-usage/releases/latest", HTML_TEMPLATE)


class TestPricingParity(unittest.TestCase):
    """Verify CLI and dashboard pricing tables stay in sync."""

    def _extract_js_pricing(self):
        """Extract pricing values from the dashboard JS PRICING object."""
        import re
        prices = {}
        for match in re.finditer(
            r"'(claude-[^']+)':\s*\{\s*input:\s*([\d.]+),\s*output:\s*([\d.]+)",
            HTML_TEMPLATE
        ):
            model, inp, out = match.group(1), float(match.group(2)), float(match.group(3))
            prices[model] = {"input": inp, "output": out}
        return prices

    def test_all_cli_models_in_dashboard(self):
        from cli import PRICING as CLI_PRICING
        js_prices = self._extract_js_pricing()
        for model in CLI_PRICING:
            self.assertIn(model, js_prices, f"{model} missing from dashboard JS")

    def test_prices_match(self):
        from cli import PRICING as CLI_PRICING
        js_prices = self._extract_js_pricing()
        for model in CLI_PRICING:
            self.assertAlmostEqual(
                CLI_PRICING[model]["input"], js_prices[model]["input"],
                msg=f"{model} input price mismatch"
            )
            self.assertAlmostEqual(
                CLI_PRICING[model]["output"], js_prices[model]["output"],
                msg=f"{model} output price mismatch"
            )


def _js_block(header):
    """Slice a brace-balanced JS declaration out of HTML_TEMPLATE by its header.

    None of the sliced declarations contain a brace inside a string literal, so a
    naive depth counter is enough (template literals like `${d.getFullYear()}` are
    themselves balanced).
    """
    start = HTML_TEMPLATE.index(header)
    depth = 0
    for i in range(HTML_TEMPLATE.index("{", start), len(HTML_TEMPLATE)):
        if HTML_TEMPLATE[i] == "{":
            depth += 1
        elif HTML_TEMPLATE[i] == "}":
            depth -= 1
            if depth == 0:
                return HTML_TEMPLATE[start:i + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _js_line(header):
    start = HTML_TEMPLATE.index(header)
    return HTML_TEMPLATE[start:HTML_TEMPLATE.index("\n", start)]


# The date/refresh logic pulled straight out of the shipped template, so these tests
# break if the real code changes. Order matters only for the `let` lines, which run
# at injection time; the function declarations hoist.
def _refresh_js():
    return "\n".join([
        _js_block("const RANGE_LABELS ="),
        _js_line("const VALID_RANGES ="),
        _js_block("function localISODate("),
        _js_block("function rangeIncludesToday("),
        _js_block("function getRangeBounds("),
        _js_block("function autoRefreshTick("),
        _js_block("function scheduleAutoRefresh("),
        _js_line("let autoRefreshTimer ="),
        _js_line("let lastSeenDate ="),
        # The visibilitychange registration is a bare statement, not a declaration.
        HTML_TEMPLATE[
            HTML_TEMPLATE.index("document.addEventListener('visibilitychange'"):
        ].split("});", 1)[0] + "});",
    ])


HARNESS = r"""
const RealDate = Date;
let FAKE = new RealDate(2026, 6, 31, 20, 0, 0);   // Fri Jul 31 2026, local time
globalThis.Date = class extends RealDate {
  constructor(...a) { if (a.length === 0) { super(FAKE.getTime()); } else { super(...a); } }
  static now() { return FAKE.getTime(); }
};

const calls = { applyFilter: 0, loadData: 0 };
function applyFilter() { calls.applyFilter++; }
function loadData() { calls.loadData++; }

let intervalFn = null, intervalMs = null, clearedCount = 0;
globalThis.setInterval = (fn, ms) => { intervalFn = fn; intervalMs = ms; return 1; };
globalThis.clearInterval = () => { clearedCount++; };

const listeners = {};
globalThis.document = {
  hidden: false,
  addEventListener: (ev, fn) => { listeners[ev] = fn; },
};

let selectedRange = '30d';
const snap = () => ({ ...calls });
"""


@unittest.skipUnless(__import__("shutil").which("node"), "node not installed")
class TestAutoRefreshTick(unittest.TestCase):
    """Execute the shipped refresh JS under node with a fake clock and DOM.

    Covers the frontend half of "a long-lived dashboard stays live": a page left open
    across midnight (and across a month boundary) has to notice the calendar moved.
    """

    def _run(self, scenario):
        import subprocess
        script = "\n".join([HARNESS, _refresh_js(), scenario])
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
            fh.write(script)
            path = fh.name
        try:
            proc = subprocess.run(
                ["node", path], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout.strip().splitlines()[-1])
        finally:
            os.unlink(path)

    def test_month_rollover_refilters(self):
        """Midnight into a new month re-runs applyFilter so the bounds follow along."""
        out = self._run(r"""
        selectedRange = 'month';
        scheduleAutoRefresh();
        intervalFn();                                   // tick on Jul 31
        const beforeRollover = snap();
        FAKE = new RealDate(2026, 7, 1, 0, 5, 0);       // Sat Aug 1 2026, 00:05
        intervalFn();
        const afterRollover = snap();
        intervalFn();                                   // same day, no second re-filter
        console.log(JSON.stringify({ beforeRollover, afterRollover, afterSameDay: snap() }));
        """)
        # Same day: fetch only, no re-filter.
        self.assertEqual(out["beforeRollover"], {"applyFilter": 0, "loadData": 1})
        # Rollover: exactly one re-filter, and the fetch still happens.
        self.assertEqual(out["afterRollover"], {"applyFilter": 1, "loadData": 2})
        # Still Aug 1 — the rollover must not re-trigger on every later tick.
        self.assertEqual(out["afterSameDay"], {"applyFilter": 1, "loadData": 3})

    def test_timer_armed_even_when_range_excludes_today(self):
        """The timer must exist regardless of range, or it can't see the rollover.

        Previously `scheduleAutoRefresh` only created an interval when the range
        already included today, so the page went permanently blind to the calendar.
        """
        out = self._run(r"""
        selectedRange = 'prev-month';
        scheduleAutoRefresh();
        intervalFn();
        console.log(JSON.stringify({
          armed: intervalFn !== null, ms: intervalMs, calls: snap(),
        }));
        """)
        self.assertTrue(out["armed"])
        self.assertEqual(out["ms"], 30000)
        # ...but a range that can't contain today still must not fetch.
        self.assertEqual(out["calls"], {"applyFilter": 0, "loadData": 0})

    def test_rollover_moves_a_range_back_into_today(self):
        """'prev-month' on Jul 31 is June; on Aug 1 it becomes July — still not today.

        'month' is the one that flips, and the tick has to start fetching again on its
        own without the user touching the dropdown.
        """
        out = self._run(r"""
        selectedRange = 'month';
        scheduleAutoRefresh();
        FAKE = new RealDate(2026, 7, 1, 0, 5, 0);
        intervalFn();
        console.log(JSON.stringify({ calls: snap() }));
        """)
        self.assertEqual(out["calls"], {"applyFilter": 1, "loadData": 1})

    def test_visibilitychange_catches_up_only_when_shown(self):
        """A throttled background tab refreshes the moment it comes back."""
        out = self._run(r"""
        selectedRange = 'today';
        scheduleAutoRefresh();
        document.hidden = true;
        listeners['visibilitychange']();
        const whileHidden = snap();
        document.hidden = false;
        listeners['visibilitychange']();
        console.log(JSON.stringify({ whileHidden, whenVisible: snap() }));
        """)
        self.assertEqual(out["whileHidden"], {"applyFilter": 0, "loadData": 0})
        self.assertEqual(out["whenVisible"], {"applyFilter": 0, "loadData": 1})

    def test_rollover_while_hidden_is_repaired_on_focus(self):
        """The exact reported shape: tab left open, month changes, come back to it."""
        out = self._run(r"""
        selectedRange = 'month';
        scheduleAutoRefresh();
        document.hidden = true;
        FAKE = new RealDate(2026, 7, 1, 9, 0, 0);
        document.hidden = false;
        listeners['visibilitychange']();
        console.log(JSON.stringify({ calls: snap() }));
        """)
        self.assertEqual(out["calls"], {"applyFilter": 1, "loadData": 1})


class TestAutoRefreshWiring(unittest.TestCase):
    """Source-level checks that don't need node, so CI covers them unconditionally."""

    def test_range_restored_before_timer_is_armed(self):
        """loadData() is async; its own restore lands too late to arm the timer with.

        Scoped to the init block on purpose — loadData's internal restore sits earlier
        in the file, so a whole-template ordering check passes even when the init block
        is missing the synchronous restore entirely.
        """
        init = HTML_TEMPLATE[HTML_TEMPLATE.rindex("initSectionNav();"):]
        self.assertIn("selectedRange = readURLRange();", init)
        self.assertLess(
            init.index("selectedRange = readURLRange();"),
            init.index("loadData();"),
        )

    def test_tick_is_scheduled_not_loaddata(self):
        """The interval must run the tick, not loadData directly."""
        self.assertIn("setInterval(autoRefreshTick, 30000)", HTML_TEMPLATE)

    def test_visibilitychange_listener_registered(self):
        self.assertIn("document.addEventListener('visibilitychange'", HTML_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
