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
from dashboard import get_dashboard_data, get_session_detail, DashboardHandler, HTML_TEMPLATE

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
        turns = [{
            "session_id": "sess-abc123", "timestamp": "2026-04-08T09:30:00Z",
            "model": "claude-sonnet-4-6", "input_tokens": 500,
            "output_tokens": 200, "cache_read_tokens": 50,
            "cache_creation_tokens": 20, "tool_name": None, "cwd": "/tmp",
        }]
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

    def test_session_id_truncated(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(len(session["session_id"]), 8)

    def test_session_duration_calculated(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        # 1 hour = 60 minutes
        self.assertEqual(session["duration_min"], 60.0)

    def test_session_detail_includes_tools_and_cwds(self):
        detail = get_session_detail("sess-abc123", db_path=self.db_path)
        self.assertEqual(detail["project"], "user/myproject")
        self.assertEqual(detail["branch"], "main")
        self.assertEqual(detail["tool_usage"][0]["tool_name"], "reply")
        self.assertEqual(detail["cwd_usage"][0]["cwd"], "/tmp")
        self.assertEqual(len(detail["turn_history"]), 1)

    def test_session_detail_token_values(self):
        detail = get_session_detail("sess-abc123", db_path=self.db_path)
        turn = detail["turn_history"][0]
        self.assertEqual(turn["input"], 500)
        self.assertEqual(turn["output"], 200)
        self.assertEqual(turn["cache_read"], 50)
        self.assertEqual(turn["cache_creation"], 20)
        self.assertEqual(turn["total"], 770)
        tool = detail["tool_usage"][0]
        self.assertEqual(tool["tokens"], 770)
        self.assertEqual(tool["turns"], 1)

    def test_session_detail_missing_db(self):
        data = get_session_detail("any-id", db_path=Path("/nonexistent/usage.db"))
        self.assertIn("error", data)
        self.assertIn("Database not found", data["error"])

    def test_session_detail_not_found(self):
        detail = get_session_detail("nonexistent-session", db_path=self.db_path)
        self.assertIn("error", detail)
        self.assertEqual(detail["error"], "Session not found")

    def test_session_detail_empty_session_id(self):
        detail = get_session_detail("", db_path=self.db_path)
        self.assertIn("error", detail)
        self.assertEqual(detail["error"], "Session not found")

    def test_session_detail_zero_turns(self):
        conn = get_db(self.db_path)
        upsert_sessions(conn, [{
            "session_id": "sess-empty", "project_name": "proj",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T09:00:00Z",
            "git_branch": None, "model": "claude-sonnet-4-6",
            "total_input_tokens": 0, "total_output_tokens": 0,
            "total_cache_read": 0, "total_cache_creation": 0,
            "turn_count": 0,
        }])
        conn.commit()
        conn.close()
        detail = get_session_detail("sess-empty", db_path=self.db_path)
        self.assertEqual(detail["tool_usage"], [])
        self.assertEqual(detail["cwd_usage"], [])
        self.assertEqual(detail["turn_history"], [])

    def test_sessions_include_new_fields(self):
        data = get_dashboard_data(db_path=self.db_path)
        session = data["sessions_all"][0]
        self.assertEqual(session["session_id_full"], "sess-abc123")
        self.assertEqual(session["branch"], "main")
        self.assertIn("2026-04-08", session["first"])


class TestDashboardHTTP(unittest.TestCase):
    """Integration test: start server and make HTTP requests."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_index_returns_html(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

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

    def test_api_session_unknown_id_returns_404(self):
        url = f"http://127.0.0.1:{self.port}/api/session?session_id=nonexistent"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
            data = json.loads(e.read())
            self.assertIn("error", data)
            e.close()

    def test_api_session_missing_param_returns_404(self):
        url = f"http://127.0.0.1:{self.port}/api/session"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
            e.close()

    def test_404_for_unknown_path(self):
        url = f"http://127.0.0.1:{self.port}/nonexistent"
        try:
            urllib.request.urlopen(url)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


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


if __name__ == "__main__":
    unittest.main()
