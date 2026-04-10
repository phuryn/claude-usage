"""Tests for dashboard.py - API endpoint and data retrieval."""

import json
import os
import shutil
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


class TestTimezoneBucketing(unittest.TestCase):
    def test_utc_midnight_converts_to_chicago_6pm_previous_day_standard_time(self):
        """January timestamp → CST (UTC-6)."""
        from dashboard import to_local_hour
        # 2026-01-15 00:00 UTC = 2026-01-14 18:00 CST
        day, hour = to_local_hour("2026-01-15T00:00:00Z")
        self.assertEqual(day, "2026-01-14")
        self.assertEqual(hour, 18)

    def test_utc_midnight_converts_to_chicago_7pm_previous_day_dst(self):
        """July timestamp → CDT (UTC-5)."""
        from dashboard import to_local_hour
        # 2026-07-15 00:00 UTC = 2026-07-14 19:00 CDT
        day, hour = to_local_hour("2026-07-15T00:00:00Z")
        self.assertEqual(day, "2026-07-14")
        self.assertEqual(hour, 19)

    def test_dst_spring_forward_boundary(self):
        """2026-03-08 08:00 UTC = 03:00 CDT (after spring forward)."""
        from dashboard import to_local_hour
        day, hour = to_local_hour("2026-03-08T08:00:00Z")
        self.assertEqual(day, "2026-03-08")
        self.assertEqual(hour, 3)

    def test_dst_fall_back_boundary(self):
        """2026-11-01 07:00 UTC = 01:00 CST (after fall back)."""
        from dashboard import to_local_hour
        day, hour = to_local_hour("2026-11-01T07:00:00Z")
        self.assertEqual(day, "2026-11-01")
        self.assertEqual(hour, 1)

    def test_unparseable_timestamp_returns_empty(self):
        from dashboard import to_local_hour
        day, hour = to_local_hour("not a timestamp")
        self.assertEqual(day, "")
        self.assertEqual(hour, 0)

    def test_none_timestamp_returns_empty(self):
        from dashboard import to_local_hour
        day, hour = to_local_hour(None)
        self.assertEqual(day, "")
        self.assertEqual(hour, 0)


class TestLoadPeakBands(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config(self, data):
        p = self.tmp / "peak-hours.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_valid_file_loads(self):
        from dashboard import load_peak_bands
        p = self._write_config({
            "bands": [
                {"timezone": "America/Los_Angeles", "days": ["Mon", "Tue"],
                 "start": "05:00", "end": "11:00", "label": "Test"}
            ]
        })
        bands = load_peak_bands(p)
        self.assertEqual(len(bands), 1)
        self.assertEqual(bands[0]["timezone"], "America/Los_Angeles")
        self.assertEqual(bands[0]["start"], "05:00")

    def test_missing_file_returns_empty(self):
        from dashboard import load_peak_bands
        bands = load_peak_bands(self.tmp / "nonexistent.json")
        self.assertEqual(bands, [])

    def test_malformed_json_returns_empty(self):
        from dashboard import load_peak_bands
        p = self.tmp / "peak-hours.json"
        p.write_text("{not valid json", encoding="utf-8")
        bands = load_peak_bands(p)
        self.assertEqual(bands, [])

    def test_invalid_band_is_dropped(self):
        """One valid + one missing required field → only valid returned."""
        from dashboard import load_peak_bands
        p = self._write_config({
            "bands": [
                {"timezone": "America/Los_Angeles", "days": ["Mon"],
                 "start": "05:00", "end": "11:00"},  # valid
                {"timezone": "America/Los_Angeles", "days": ["Tue"],
                 "start": "05:00"},  # missing 'end'
            ]
        })
        bands = load_peak_bands(p)
        self.assertEqual(len(bands), 1)
        self.assertEqual(bands[0]["days"], ["Mon"])

    def test_bad_timezone_is_dropped(self):
        from dashboard import load_peak_bands
        p = self._write_config({
            "bands": [
                {"timezone": "Not/AReal_Zone", "days": ["Mon"],
                 "start": "05:00", "end": "11:00"}
            ]
        })
        bands = load_peak_bands(p)
        self.assertEqual(bands, [])

    def test_hh_mm_format_is_validated(self):
        """Non HH:MM strings should be rejected even if they pass isinstance(str)."""
        from dashboard import load_peak_bands
        p = self._write_config({
            "bands": [
                {"timezone": "America/Los_Angeles", "days": ["Mon"],
                 "start": "banana", "end": "carrot"}
            ]
        })
        self.assertEqual(load_peak_bands(p), [])

    def test_empty_days_list_is_rejected(self):
        from dashboard import load_peak_bands
        p = self._write_config({
            "bands": [
                {"timezone": "America/Los_Angeles", "days": [],
                 "start": "05:00", "end": "11:00"}
            ]
        })
        self.assertEqual(load_peak_bands(p), [])

    def test_start_after_end_is_rejected(self):
        from dashboard import load_peak_bands
        p = self._write_config({
            "bands": [
                {"timezone": "America/Los_Angeles", "days": ["Mon"],
                 "start": "11:00", "end": "05:00"}
            ]
        })
        self.assertEqual(load_peak_bands(p), [])


class TestHourlyAggregation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "test.db"
        from scanner import get_db, init_db
        conn = get_db(self.db_path)
        init_db(conn)
        # Seed: two turns in the same local hour, one turn in a different hour
        conn.execute("""
            INSERT INTO sessions (session_id, project_name, first_timestamp,
                last_timestamp, total_input_tokens, total_output_tokens,
                total_cache_read, total_cache_creation, model, turn_count)
            VALUES ('s1', 'proj', '2026-04-10T14:00:00Z', '2026-04-10T14:45:00Z',
                    300, 150, 0, 0, 'claude-opus-4-6', 2)
        """)
        conn.executemany("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, tool_name, cwd, message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            # 2026-04-10T14:15:00Z = 2026-04-10 09:15 Chicago (CDT, UTC-5)
            ("s1", "2026-04-10T14:15:00Z", "claude-opus-4-6", 100, 50, 0, 0, None, "/cwd", "m1"),
            # 2026-04-10T14:45:00Z = 2026-04-10 09:45 Chicago
            ("s1", "2026-04-10T14:45:00Z", "claude-opus-4-6", 200, 100, 0, 0, None, "/cwd", "m2"),
            # 2026-04-10T20:00:00Z = 2026-04-10 15:00 Chicago
            ("s1", "2026-04-10T20:00:00Z", "claude-opus-4-6",  50,  25, 0, 0, None, "/cwd", "m3"),
        ])
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_single_turn_lands_in_correct_hour_bucket(self):
        from dashboard import get_dashboard_data
        data = get_dashboard_data(self.db_path)
        hourly = data["turns_by_hour_local"]
        # Find the 15:00 bucket (Chicago) — should have one turn, 50 input
        h15 = [h for h in hourly if h["hour_local"] == 15]
        self.assertEqual(len(h15), 1)
        self.assertEqual(h15[0]["input"], 50)
        self.assertEqual(h15[0]["turns"], 1)
        self.assertEqual(h15[0]["day_local"], "2026-04-10")

    def test_multiple_turns_same_hour_sum_correctly(self):
        from dashboard import get_dashboard_data
        data = get_dashboard_data(self.db_path)
        hourly = data["turns_by_hour_local"]
        h9 = [h for h in hourly if h["hour_local"] == 9]
        self.assertEqual(len(h9), 1)
        self.assertEqual(h9[0]["input"], 300)  # 100 + 200
        self.assertEqual(h9[0]["output"], 150)  # 50 + 100
        self.assertEqual(h9[0]["turns"], 2)

    def test_response_includes_peak_bands_and_timezone(self):
        from dashboard import get_dashboard_data
        data = get_dashboard_data(self.db_path)
        self.assertIn("peak_bands", data)
        self.assertIn("viewer_timezone", data)
        self.assertEqual(data["viewer_timezone"], "America/Chicago")
        self.assertIsInstance(data["peak_bands"], list)


if __name__ == "__main__":
    unittest.main()
