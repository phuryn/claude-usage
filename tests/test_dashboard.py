"""Tests for dashboard.py - API endpoint and data retrieval."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from scanner import get_db, init_db, upsert_sessions, insert_turns
from dashboard import (
    DashboardHandler,
    MISSING_BUILD_HTML,
    _content_type,
    _safe_static_path,
    get_dashboard_data,
)

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
        self.assertIn("project_usage_by_day", data)
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

    def test_session_id_preserved_with_display_label(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(session["session_id"], "sess-abc123")
        self.assertEqual(session["session_label"], "sess-abc")

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

    def test_project_usage_by_day_aggregates_turns_by_session_day_and_model(self):
        data = get_dashboard_data(db_path=self.db_path)
        rows = data["project_usage_by_day"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["day"], "2026-04-08")
        self.assertEqual(row["model"], "claude-sonnet-4-6")
        self.assertEqual(row["project"], "user/myproject")
        self.assertEqual(row["branch"], "main")
        self.assertEqual(row["session_id"], "sess-abc123")
        self.assertEqual(row["turns"], 2)
        self.assertEqual(row["input"], 800)
        self.assertEqual(row["output"], 350)

    def test_project_usage_by_day_keeps_cross_day_turns_separate(self):
        conn = get_db(self.db_path)
        insert_turns(conn, [{
            "session_id": "sess-abc123", "timestamp": "2026-04-09T01:00:00Z",
            "model": "claude-sonnet-4-6", "input_tokens": 900,
            "output_tokens": 450, "cache_read_tokens": 90,
            "cache_creation_tokens": 45, "tool_name": None, "cwd": "/tmp",
        }])
        conn.commit()
        conn.close()

        data = get_dashboard_data(db_path=self.db_path)
        rows = sorted(data["project_usage_by_day"], key=lambda r: r["day"])
        self.assertEqual([r["day"] for r in rows], ["2026-04-08", "2026-04-09"])
        self.assertEqual(rows[0]["input"], 800)
        self.assertEqual(rows[1]["input"], 900)


class TestDashboardHTTP(unittest.TestCase):
    """Integration test: start server and make HTTP requests."""

    @classmethod
    def setUpClass(cls):
        # Redirect DB_PATH + projects dirs to a tempdir so /api/rescan
        # doesn't unlink the user's real ~/.claude/usage.db or scan their
        # real transcript directory during tests.
        import dashboard as _d
        import scanner as _s
        cls._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmpdir.name)
        tmp_projects = tmp / "projects"
        tmp_projects.mkdir()
        tmp_dist = tmp / "dist"
        tmp_assets = tmp_dist / "assets"
        tmp_assets.mkdir(parents=True)
        (tmp_dist / "index.html").write_text(
            '<!doctype html><html><head>'
            '<script type="module" src="/assets/index-test.js"></script>'
            '<link rel="stylesheet" href="/assets/index-test.css">'
            '</head><body><div id="root"></div></body></html>',
            encoding="utf-8",
        )
        (tmp_assets / "index-test.js").write_text("console.log('dashboard');", encoding="utf-8")
        (tmp_assets / "index-test.css").write_text(":root { color: white; }", encoding="utf-8")
        cls._patches = {
            (_d, "DB_PATH"):                (_d.DB_PATH,                tmp / "usage.db"),
            (_d, "STATIC_DIR"):             (_d.STATIC_DIR,             tmp_dist),
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
        cls.server.server_close()
        cls.thread.join(timeout=2)
        for (mod, name), (orig, _new) in cls._patches.items():
            setattr(mod, name, orig)
        cls._tmpdir.cleanup()

    def test_index_returns_html(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
            self.assertIn(b'<div id="root"></div>', resp.read())

    def test_index_with_query_returns_html(self):
        url = f"http://127.0.0.1:{self.port}/?range=30d"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_index_html_with_query_returns_html(self):
        url = f"http://127.0.0.1:{self.port}/index.html?models=claude-sonnet-4-6"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_vite_js_asset_returns_javascript(self):
        url = f"http://127.0.0.1:{self.port}/assets/index-test.js"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/javascript", resp.headers["Content-Type"])
            self.assertIn(b"dashboard", resp.read())

    def test_vite_css_asset_returns_css(self):
        url = f"http://127.0.0.1:{self.port}/assets/index-test.css"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/css", resp.headers["Content-Type"])

    def test_api_data_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/data"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            # Should have expected keys (or error if no DB)
            self.assertTrue("all_models" in data or "error" in data)

    def test_api_data_with_query_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/data?cacheBust=1"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])

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

    def test_api_rescan_with_query_returns_json(self):
        url = f"http://127.0.0.1:{self.port}/api/rescan?force=1"
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])

    def test_404_for_unknown_path(self):
        url = f"http://127.0.0.1:{self.port}/nonexistent"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            with e:
                self.assertEqual(e.code, 404)

    def test_404_for_unknown_path_with_query(self):
        url = f"http://127.0.0.1:{self.port}/nonexistent?range=7d"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            with e:
                self.assertEqual(e.code, 404)


class TestReactDashboardStaticContracts(unittest.TestCase):
    def setUp(self):
        self.src = (Path(__file__).resolve().parents[1] / "src" / "App.tsx").read_text(encoding="utf-8")

    def test_missing_build_fallback_is_valid_html(self):
        self.assertIn("<!DOCTYPE html>", MISSING_BUILD_HTML)
        self.assertIn("Dashboard build missing", MISSING_BUILD_HTML)

    def test_safe_static_path_blocks_escape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "dist"
            root.mkdir()
            self.assertIsNone(_safe_static_path("/../../etc/passwd", static_dir=root))

    def test_content_type_maps_vite_assets(self):
        self.assertEqual(_content_type(Path("app.js")), "application/javascript")
        self.assertEqual(_content_type(Path("app.css")), "text/css; charset=utf-8")

    def test_react_pricing_has_substring_matching(self):
        self.assertIn("m.includes('opus')", self.src)
        self.assertIn("m.includes('sonnet')", self.src)
        self.assertIn("m.includes('haiku')", self.src)

    def test_unknown_models_return_null(self):
        self.assertIn("return null;", self.src)

    def test_hourly_peak_hour_constants(self):
        self.assertIn("PEAK_HOURS_UTC", self.src)
        self.assertIn("[12, 13, 14, 15, 16, 17]", self.src)

    def test_hourly_filter_does_not_reference_undefined_cutoff(self):
        """Regression guard: the dashboard used to throw on first render."""
        self.assertNotIn("cutoff", self.src)
        self.assertIn("day >= start", self.src)
        self.assertIn("day <= end", self.src)

    def test_session_export_uses_full_id(self):
        self.assertIn("record.session_id", self.src)
        self.assertIn("session.session_label", self.src)

    def test_project_views_use_turn_date_aggregate(self):
        self.assertIn("project_usage_by_day", self.src)
        self.assertIn("filteredProjectUsage", self.src)
        self.assertIn("sessionIds.add(row.session_id)", self.src)

    def test_project_branch_sort_uses_typed_sort_state(self):
        self.assertIn("type BranchSortCol", self.src)
        self.assertIn("numericSort(totals.byProjectBranch, branchSort.col", self.src)


class TestPricingParity(unittest.TestCase):
    """Verify CLI and dashboard pricing tables stay in sync."""

    def _extract_js_pricing(self):
        """Extract pricing values from the React dashboard PRICING object."""
        import re
        src = (Path(__file__).resolve().parents[1] / "src" / "App.tsx").read_text(encoding="utf-8")
        prices = {}
        for match in re.finditer(
            r"'(claude-[^']+)':\s*\{\s*input:\s*([\d.]+),\s*output:\s*([\d.]+)",
            src
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


if __name__ == "__main__":
    unittest.main()
