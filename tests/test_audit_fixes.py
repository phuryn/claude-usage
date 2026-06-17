"""Regression tests for the three remaining audit findings:
1. scanner shrink/compaction permanent-skip
2. CLI today/week UTC date
3. dashboard /api/data result cache
"""

import json
import os
import sqlite3
import time
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import dashboard
from scanner import scan

NL = chr(10)


def _assistant(message_id, session_id="s1", inp=100, out=50,
               ts="2026-06-17T10:00:00Z"):
    return json.dumps({
        "type": "assistant", "sessionId": session_id, "timestamp": ts,
        "cwd": "/home/user/proj",
        "message": {"id": message_id, "model": "claude-opus-4-8",
                    "usage": {"input_tokens": inp, "output_tokens": out,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0},
                    "content": []},
    })


class TestScannerShrinkNotStuck(unittest.TestCase):
    """A file rewritten shorter must update its stored line count, so later
    growth is still detected (was: skipped forever)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects" / "p"
        self.projects.mkdir(parents=True)
        self.db = self.tmp / "usage.db"
        self.f = self.projects / "sess.jsonl"

    def _scan(self):
        return scan(projects_dir=self.tmp / "projects", db_path=self.db, verbose=False)

    def _lines_recorded(self):
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT lines FROM processed_files").fetchone()
        conn.close()
        return row[0]

    def test_shrink_updates_lines_and_allows_future_growth(self):
        # Initial: 2 lines.
        self.f.write_text(_assistant("m1") + NL + _assistant("m2") + NL)
        self._scan()
        self.assertEqual(self._lines_recorded(), 2)

        # Rewrite SHORTER: 1 line (compaction). mtime must change.
        time.sleep(0.05)
        self.f.write_text(_assistant("m3") + NL)
        self._scan()
        # Bug fix: stored line count follows the shrink (was stuck at 2).
        self.assertEqual(self._lines_recorded(), 1)

        # Append a new turn -> file grows to 2 lines; must be detected & ingested
        # (pre-fix this was permanently skipped because stored lines stayed 2).
        time.sleep(0.05)
        with open(self.f, "a") as fh:
            fh.write(_assistant("m4") + NL)
        self._scan()
        conn = sqlite3.connect(self.db)
        got = conn.execute("SELECT 1 FROM turns WHERE message_id='m4'").fetchone()
        conn.close()
        self.assertIsNotNone(got, "appended turn after a shrink must be ingested")


class TestCliUtcToday(unittest.TestCase):
    def test_utc_today_matches_utc_clock(self):
        from cli import _utc_today
        self.assertEqual(_utc_today(), datetime.now(timezone.utc).date())


class TestDashboardCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects = self.tmp / "projects" / "p"
        self.projects.mkdir(parents=True)
        self.db = self.tmp / "usage.db"
        (self.projects / "sess.jsonl").write_text(_assistant("m1") + NL)
        scan(projects_dir=self.tmp / "projects", db_path=self.db, verbose=False)

    def test_cache_hit_returns_same_object(self):
        d1 = dashboard.get_dashboard_data(self.db)
        d2 = dashboard.get_dashboard_data(self.db)
        self.assertIs(d1, d2)  # unchanged DB -> cached payload reused

    def test_mtime_change_invalidates_cache(self):
        d1 = dashboard.get_dashboard_data(self.db)
        future = time.time() + 10
        os.utime(self.db, (future, future))
        d3 = dashboard.get_dashboard_data(self.db)
        self.assertIsNot(d3, d1)  # mtime bump -> recomputed


if __name__ == "__main__":
    unittest.main()
