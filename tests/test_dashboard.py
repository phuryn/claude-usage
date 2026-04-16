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
from dashboard import (
    get_dashboard_data,
    DashboardHandler,
    HTML_TEMPLATE,
    CSRF_TOKEN,
    _configure_allowed_hosts,
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


class TestDashboardHTTP(unittest.TestCase):
    """Integration test: start server and make HTTP requests."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        _configure_allowed_hosts("127.0.0.1", cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _get(self, path, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, headers=headers or {})
        return urllib.request.urlopen(req)

    def _post(self, path, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, method="POST", headers=headers or {})
        return urllib.request.urlopen(req)

    def test_index_returns_html(self):
        with self._get("/") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])

    def test_api_data_returns_json(self):
        with self._get("/api/data") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertTrue("all_models" in data or "error" in data)

    def test_api_rescan_returns_json(self):
        with self._post("/api/rescan", {"X-CSRF-Token": CSRF_TOKEN}) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])
            data = json.loads(resp.read())
            self.assertIn("new", data)
            self.assertIn("updated", data)
            self.assertIn("skipped", data)

    def test_404_for_unknown_path(self):
        try:
            self._get("/nonexistent")
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    # ── Security: CSRF ───────────────────────────────────────────────────────
    def test_rescan_rejected_without_csrf_token(self):
        """POST /api/rescan must fail without X-CSRF-Token."""
        try:
            self._post("/api/rescan")
            self.fail("Expected 403")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_rescan_rejected_with_wrong_csrf_token(self):
        try:
            self._post("/api/rescan", {"X-CSRF-Token": "nope"})
            self.fail("Expected 403")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_rescan_rejected_with_foreign_origin(self):
        """Valid token but cross-origin request must still be rejected."""
        try:
            self._post("/api/rescan", {
                "X-CSRF-Token": CSRF_TOKEN,
                "Origin": "https://evil.example",
            })
            self.fail("Expected 403")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    # ── Security: Host header allowlist (DNS rebinding defense) ──────────────
    def test_index_rejected_with_foreign_host_header(self):
        try:
            self._get("/", {"Host": "attacker.rebind.network"})
            self.fail("Expected 403")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_api_data_rejected_with_foreign_host_header(self):
        try:
            self._get("/api/data", {"Host": "evil.example"})
            self.fail("Expected 403")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    # ── Security: response headers ───────────────────────────────────────────
    def test_index_has_security_headers(self):
        with self._get("/") as resp:
            self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
            csp = resp.headers.get("Content-Security-Policy") or ""
            self.assertIn("frame-ancestors 'none'", csp)

    # ── Security: CSRF token is embedded in served HTML ──────────────────────
    def test_csrf_token_rendered_into_html(self):
        with self._get("/") as resp:
            body = resp.read().decode("utf-8")
            self.assertIn(CSRF_TOKEN, body)
            self.assertNotIn("__CSRF_TOKEN__", body)


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
