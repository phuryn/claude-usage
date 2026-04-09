"""Tests for scanner.py - JSONL parsing, DB operations, and scanning."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scanner import (
    get_db, init_db, project_name_from_cwd, parse_jsonl_file,
    aggregate_sessions, upsert_sessions, insert_turns, scan,
)


class TestProjectNameFromCwd(unittest.TestCase):
    def test_two_components(self):
        self.assertEqual(project_name_from_cwd("/home/user/myproject"), "user/myproject")

    def test_deep_path(self):
        self.assertEqual(project_name_from_cwd("/a/b/c/d"), "c/d")

    def test_single_component(self):
        self.assertEqual(project_name_from_cwd("/root"), "/root")

    def test_windows_path(self):
        self.assertEqual(project_name_from_cwd("C:\\Users\\me\\project"), "me/project")

    def test_trailing_slash(self):
        self.assertEqual(project_name_from_cwd("/home/user/project/"), "user/project")

    def test_empty_string(self):
        self.assertEqual(project_name_from_cwd(""), "unknown")

    def test_none(self):
        self.assertEqual(project_name_from_cwd(None), "unknown")


def _make_assistant_record(session_id="sess-1", model="claude-sonnet-4-6",
                           input_tokens=100, output_tokens=50,
                           cache_read=10, cache_creation=5,
                           timestamp="2026-04-08T10:00:00Z",
                           cwd="/home/user/project"):
    return json.dumps({
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
            "content": [],
        },
    })


def _make_user_record(session_id="sess-1", timestamp="2026-04-08T09:59:00Z",
                      cwd="/home/user/project"):
    return json.dumps({
        "type": "user",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
    })


class TestParseJsonlFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write_jsonl(self, filename, lines):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w") as f:
            for line in lines:
                f.write(line + "\n")
        return path

    def test_basic_parsing(self):
        path = self._write_jsonl("test.jsonl", [
            _make_user_record(),
            _make_assistant_record(),
        ])
        metas, turns = parse_jsonl_file(path)
        self.assertEqual(len(metas), 1)
        self.assertEqual(len(turns), 1)
        self.assertEqual(metas[0]["session_id"], "sess-1")
        self.assertEqual(turns[0]["input_tokens"], 100)
        self.assertEqual(turns[0]["output_tokens"], 50)

    def test_skips_zero_token_records(self):
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(input_tokens=0, output_tokens=0,
                                   cache_read=0, cache_creation=0),
        ])
        _, turns = parse_jsonl_file(path)
        self.assertEqual(len(turns), 0)

    def test_skips_non_assistant_user_types(self):
        path = self._write_jsonl("test.jsonl", [
            json.dumps({"type": "system", "sessionId": "s1"}),
            _make_assistant_record(session_id="s1"),
        ])
        metas, turns = parse_jsonl_file(path)
        self.assertEqual(len(turns), 1)

    def test_handles_malformed_json(self):
        path = self._write_jsonl("test.jsonl", [
            "not valid json",
            _make_assistant_record(),
        ])
        _, turns = parse_jsonl_file(path)
        self.assertEqual(len(turns), 1)

    def test_handles_empty_file(self):
        path = self._write_jsonl("test.jsonl", [])
        metas, turns = parse_jsonl_file(path)
        self.assertEqual(len(metas), 0)
        self.assertEqual(len(turns), 0)

    def test_multiple_sessions(self):
        path = self._write_jsonl("test.jsonl", [
            _make_assistant_record(session_id="s1"),
            _make_assistant_record(session_id="s2"),
        ])
        metas, turns = parse_jsonl_file(path)
        self.assertEqual(len(metas), 2)
        self.assertEqual(len(turns), 2)

    def test_session_timestamps_tracked(self):
        path = self._write_jsonl("test.jsonl", [
            _make_user_record(timestamp="2026-04-08T09:00:00Z"),
            _make_assistant_record(timestamp="2026-04-08T09:05:00Z"),
            _make_assistant_record(timestamp="2026-04-08T09:10:00Z"),
        ])
        metas, _ = parse_jsonl_file(path)
        self.assertEqual(metas[0]["first_timestamp"], "2026-04-08T09:00:00Z")
        self.assertEqual(metas[0]["last_timestamp"], "2026-04-08T09:10:00Z")

    def test_tool_name_extracted(self):
        record = json.dumps({
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-04-08T10:00:00Z",
            "cwd": "/tmp",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 50,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0},
                "content": [{"type": "tool_use", "name": "Read"}],
            },
        })
        path = self._write_jsonl("test.jsonl", [record])
        _, turns = parse_jsonl_file(path)
        self.assertEqual(turns[0]["tool_name"], "Read")


class TestAggregateSessions(unittest.TestCase):
    def test_aggregation(self):
        metas = [{"session_id": "s1", "project_name": "test",
                  "first_timestamp": "t1", "last_timestamp": "t2",
                  "git_branch": "main", "model": None}]
        turns = [
            {"session_id": "s1", "input_tokens": 100, "output_tokens": 50,
             "cache_read_tokens": 10, "cache_creation_tokens": 5, "model": "claude-sonnet-4-6"},
            {"session_id": "s1", "input_tokens": 200, "output_tokens": 100,
             "cache_read_tokens": 20, "cache_creation_tokens": 10, "model": "claude-sonnet-4-6"},
        ]
        sessions = aggregate_sessions(metas, turns)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["total_input_tokens"], 300)
        self.assertEqual(sessions[0]["total_output_tokens"], 150)
        self.assertEqual(sessions[0]["turn_count"], 2)
        self.assertEqual(sessions[0]["model"], "claude-sonnet-4-6")

    def test_empty_turns(self):
        metas = [{"session_id": "s1", "project_name": "test",
                  "first_timestamp": "t1", "last_timestamp": "t2",
                  "git_branch": "main", "model": None}]
        sessions = aggregate_sessions(metas, [])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["total_input_tokens"], 0)
        self.assertEqual(sessions[0]["turn_count"], 0)


class TestDatabaseOperations(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        self.conn = get_db(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_init_db_creates_tables(self):
        tables = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in tables}
        self.assertIn("sessions", table_names)
        self.assertIn("turns", table_names)
        self.assertIn("processed_files", table_names)

    def test_init_db_is_idempotent(self):
        # Running init_db twice should not error
        init_db(self.conn)
        init_db(self.conn)

    def test_upsert_new_session(self):
        sessions = [{
            "session_id": "s1", "project_name": "test",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 1000, "total_output_tokens": 500,
            "total_cache_read": 100, "total_cache_creation": 50,
            "turn_count": 5,
        }]
        upsert_sessions(self.conn, sessions)
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id = 's1'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["total_input_tokens"], 1000)
        self.assertEqual(row["turn_count"], 5)

    def test_upsert_updates_existing_session(self):
        session = {
            "session_id": "s1", "project_name": "test",
            "first_timestamp": "2026-04-08T09:00:00Z",
            "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-4-6",
            "total_input_tokens": 1000, "total_output_tokens": 500,
            "total_cache_read": 100, "total_cache_creation": 50,
            "turn_count": 5,
        }
        upsert_sessions(self.conn, [session])
        # Add more tokens
        session2 = {**session, "total_input_tokens": 200, "total_output_tokens": 100,
                    "total_cache_read": 20, "total_cache_creation": 10, "turn_count": 2}
        upsert_sessions(self.conn, [session2])
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id = 's1'").fetchone()
        self.assertEqual(row["total_input_tokens"], 1200)  # 1000 + 200
        self.assertEqual(row["turn_count"], 7)  # 5 + 2

    def test_insert_turns(self):
        turns = [{
            "session_id": "s1", "timestamp": "2026-04-08T10:00:00Z",
            "model": "claude-sonnet-4-6", "input_tokens": 100,
            "output_tokens": 50, "cache_read_tokens": 10,
            "cache_creation_tokens": 5, "tool_name": "Read", "cwd": "/tmp",
        }]
        insert_turns(self.conn, turns)
        rows = self.conn.execute("SELECT * FROM turns").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "claude-sonnet-4-6")


class TestScanIntegration(unittest.TestCase):
    """Integration test: create fake JSONL files and run scan()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects"
        self.projects_dir.mkdir()
        self.db_path = Path(self.tmpdir) / "usage.db"

    def _write_project_jsonl(self, project_name, session_id, num_turns=3):
        project_dir = self.projects_dir / project_name
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{session_id}.jsonl"
        with open(path, "w") as f:
            f.write(_make_user_record(session_id=session_id) + "\n")
            for i in range(num_turns):
                ts = f"2026-04-08T10:{i:02d}:00Z"
                f.write(_make_assistant_record(
                    session_id=session_id,
                    timestamp=ts,
                    input_tokens=100 * (i + 1),
                    output_tokens=50 * (i + 1),
                ) + "\n")

    def test_scan_new_files(self):
        self._write_project_jsonl("user/myproject", "sess-1", num_turns=3)
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["turns"], 3)
        self.assertEqual(result["sessions"], 1)

    def test_scan_is_incremental(self):
        self._write_project_jsonl("user/myproject", "sess-1")
        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        # Second scan should skip
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["new"], 0)

    def test_scan_empty_directory(self):
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["turns"], 0)

    def test_scan_multiple_files(self):
        self._write_project_jsonl("user/project-a", "sess-1", num_turns=2)
        self._write_project_jsonl("user/project-b", "sess-2", num_turns=4)
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["new"], 2)
        self.assertEqual(result["turns"], 6)
        self.assertEqual(result["sessions"], 2)


class TestScanIncrementalUpdate(unittest.TestCase):
    """Tests for the incremental (updated file) scan path.

    These guard against:
    - Double-counting turns when a file is read twice (the original bug)
    - new_session_metas not being populated (unused new_metas bug)
    - Race condition where line count differs between two reads
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects"
        self.projects_dir.mkdir()
        self.db_path = Path(self.tmpdir) / "usage.db"
        project_dir = self.projects_dir / "user" / "proj"
        project_dir.mkdir(parents=True)
        self.jsonl_path = project_dir / "sess-1.jsonl"

    def _append_turn(self, session_id="sess-1", input_tokens=100,
                     output_tokens=50, timestamp="2026-04-08T10:00:00Z"):
        with open(self.jsonl_path, "a") as f:
            f.write(_make_assistant_record(
                session_id=session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                timestamp=timestamp,
            ) + "\n")

    def _db_turn_count(self):
        conn = get_db(self.db_path)
        n = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        conn.close()
        return n

    def _db_session(self, session_id="sess-1"):
        conn = get_db(self.db_path)
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def test_no_duplicate_turns_on_update(self):
        """Growing a file must add only new turns, not re-insert old ones."""
        self._append_turn(input_tokens=100, timestamp="2026-04-08T10:00:00Z")
        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(self._db_turn_count(), 1)

        self._append_turn(input_tokens=200, timestamp="2026-04-08T10:01:00Z")
        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        # Must be 2, not 3 (which would happen if the first turn were re-inserted)
        self.assertEqual(self._db_turn_count(), 2)

    def test_token_counts_accumulate_without_double_counting(self):
        """Session token totals must be the sum of all turns, not doubled."""
        self._append_turn(input_tokens=100, output_tokens=50,
                          timestamp="2026-04-08T10:00:00Z")
        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)

        self._append_turn(input_tokens=300, output_tokens=150,
                          timestamp="2026-04-08T10:01:00Z")
        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)

        s = self._db_session()
        self.assertEqual(s["total_input_tokens"], 400)   # 100 + 300, not 200 + 300
        self.assertEqual(s["total_output_tokens"], 200)  # 50 + 150
        self.assertEqual(s["turn_count"], 2)

    def test_session_last_timestamp_updated(self):
        """last_timestamp must advance to reflect turns added in subsequent scans."""
        self._append_turn(timestamp="2026-04-08T10:00:00Z")
        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertIn("10:00:00", self._db_session()["last_timestamp"])

        self._append_turn(timestamp="2026-04-08T11:00:00Z")
        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertIn("11:00:00", self._db_session()["last_timestamp"])

    def test_result_counts_updated_not_new(self):
        """scan() must report updated_files, not new_files, for a grown file."""
        self._append_turn(timestamp="2026-04-08T10:00:00Z")
        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)

        self._append_turn(timestamp="2026-04-08T10:01:00Z")
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["turns"], 1)

    def test_unchanged_line_count_skipped_even_if_mtime_differs(self):
        """If line count hasn't grown, file must be skipped and DB unchanged."""
        self._append_turn(timestamp="2026-04-08T10:00:00Z")
        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)

        # Touch the file (changes mtime) without adding content
        os.utime(self.jsonl_path, None)
        result = scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self._db_turn_count(), 1)


if __name__ == "__main__":
    unittest.main()
