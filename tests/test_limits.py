"""Tests for usage-limit event detection (scanner.extract_limit_event + scan)."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scanner import extract_limit_event, scan

NL = chr(10)


def _limit_record(reset_ts="1736503200", uuid="u-limit-1", session_id="s1",
                  api_error=True):
    rec = {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": "2026-06-17T05:00:00Z",
        "message": {"content": [
            {"type": "text", "text": f"Claude AI usage limit reached|{reset_ts}"}
        ]},
    }
    if api_error:
        rec["isApiErrorMessage"] = True
    return rec


class TestExtractLimitEvent(unittest.TestCase):
    def test_detects_api_error_limit(self):
        e = extract_limit_event(_limit_record(reset_ts="1736503200"))
        self.assertIsNotNone(e)
        self.assertEqual(e["reset_at"], 1736503200)
        self.assertEqual(e["session_id"], "s1")
        self.assertEqual(e["uuid"], "u-limit-1")

    def test_no_api_error_flag_is_ignored(self):
        # Same text but not flagged as an API error -> not a real limit event
        # (guards against conversation text that merely mentions limits).
        self.assertIsNone(extract_limit_event(_limit_record(api_error=False)))

    def test_api_error_without_limit_text_ignored(self):
        rec = {"type": "assistant", "uuid": "u2", "isApiErrorMessage": True,
               "message": {"content": [{"type": "text", "text": "some other error"}]}}
        self.assertIsNone(extract_limit_event(rec))

    def test_reset_at_zero_when_unknown(self):
        e = extract_limit_event(_limit_record(reset_ts="0"))
        self.assertEqual(e["reset_at"], 0)


class TestLimitEventScanIntegration(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects" / "user" / "proj"
        self.projects_dir.mkdir(parents=True)
        self.db_path = Path(self.tmpdir) / "usage.db"

    def test_scan_records_limit_event(self):
        with open(self.projects_dir / "sess-1.jsonl", "w") as f:
            f.write(json.dumps({
                "type": "assistant", "sessionId": "s1", "uuid": "m-1",
                "timestamp": "2026-06-17T04:00:00Z",
                "message": {"id": "m-1", "model": "claude-opus-4-8",
                            "usage": {"input_tokens": 100, "output_tokens": 50,
                                      "cache_read_input_tokens": 0,
                                      "cache_creation_input_tokens": 0},
                            "content": []},
            }) + NL)
            f.write(json.dumps(_limit_record(reset_ts="1750000000", uuid="u-lim")) + NL)

        scan(projects_dir=self.projects_dir.parent.parent, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM limit_events").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reset_at"], 1750000000)
        self.assertEqual(rows[0]["uuid"], "u-lim")
        conn.close()


if __name__ == "__main__":
    unittest.main()
