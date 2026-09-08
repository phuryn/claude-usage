"""Tests for hook-injection tracking: extraction from `type: "attachment"`
records, scan/backfill wiring, dedup, and the dashboard's hook_injections
dataset (see scanner.extract_hook_injection / hook_events table)."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import dashboard
from scanner import (
    get_db, init_db, extract_hook_injection, insert_hook_events,
    parse_jsonl_file, scan,
)

NL = chr(10)


def _hook_success(session_id="s1", timestamp="2026-04-08T10:00:00Z",
                   cwd="/home/user/project", uuid="u1",
                   hook_name="SessionStart:startup", hook_event="SessionStart",
                   command="/home/user/.claude/hooks/startup.sh",
                   additional_context="hello from a hook"):
    stdout = json.dumps({
        "hookSpecificOutput": {
            "hookEventName": hook_event,
            "additionalContext": additional_context,
        }
    }) if additional_context is not None else ""
    return json.dumps({
        "type": "attachment",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "uuid": uuid,
        "attachment": {
            "type": "hook_success",
            "hookName": hook_name,
            "hookEvent": hook_event,
            "command": command,
            "content": "",
            "stdout": stdout,
            "stderr": "",
            "exitCode": 0,
        },
    })


def _assistant(session_id="s1", model="claude-sonnet-5", timestamp="2026-04-08T10:00:00Z",
               cwd="/home/user/project", message_id="m1"):
    return json.dumps({
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "message": {
            "model": model, "id": message_id,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [],
        },
    })


class TestExtractHookInjection(unittest.TestCase):
    def test_extracts_additional_context_length(self):
        rec = json.loads(_hook_success(additional_context="12345"))
        result = extract_hook_injection(rec)
        self.assertIsNotNone(result)
        self.assertEqual(result["injected_chars"], 5)
        self.assertEqual(result["hook_name"], "SessionStart:startup")
        self.assertEqual(result["hook_event"], "SessionStart")
        self.assertEqual(result["command"], "/home/user/.claude/hooks/startup.sh")

    def test_non_attachment_record_ignored(self):
        self.assertIsNone(extract_hook_injection({"type": "assistant"}))

    def test_non_hook_success_attachment_ignored(self):
        rec = {"type": "attachment", "sessionId": "s1", "attachment": {"type": "opened_file_in_ide"}}
        self.assertIsNone(extract_hook_injection(rec))

    def test_hook_with_no_additional_context_ignored(self):
        """Most PreToolUse/PostToolUse hooks are pass/fail gates with empty
        stdout — nothing measurable, so they shouldn't show up at all."""
        rec = json.loads(_hook_success(additional_context=None))
        self.assertIsNone(extract_hook_injection(rec))

    def test_malformed_stdout_json_ignored_not_crashed(self):
        rec = json.loads(_hook_success())
        rec["attachment"]["stdout"] = "not json{"
        self.assertIsNone(extract_hook_injection(rec))

    def test_missing_session_id_ignored(self):
        rec = json.loads(_hook_success())
        del rec["sessionId"]
        self.assertIsNone(extract_hook_injection(rec))


class TestParseJsonlFileCollectsHookEvents(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write(self, lines):
        path = os.path.join(self.tmpdir, "a.jsonl")
        with open(path, "w") as f:
            f.write(NL.join(lines) + NL)
        return path

    def test_hook_events_param_collects_injections(self):
        path = self._write([
            _assistant(message_id="m1"),
            _hook_success(uuid="u1", additional_context="x" * 100),
        ])
        hook_events = []
        parse_jsonl_file(path, hook_events=hook_events)
        self.assertEqual(len(hook_events), 1)
        self.assertEqual(hook_events[0]["injected_chars"], 100)

    def test_hook_events_none_by_default_does_not_crash(self):
        path = self._write([_hook_success(uuid="u1")])
        # Default call (no hook_events list) must not raise even though the
        # file contains a hook_success record.
        session_metas, turns, agents, line_count = parse_jsonl_file(path)
        self.assertEqual(turns, [])


class TestScanPersistsHookEvents(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"

    def _write(self, relpath, lines):
        path = os.path.join(self.tmpdir, relpath)
        with open(path, "w") as f:
            f.write(NL.join(lines) + NL)
        return path

    def test_scan_inserts_hook_events_for_new_file(self):
        self._write("a.jsonl", [
            _assistant(message_id="m1"),
            _hook_success(uuid="u1", additional_context="hello"),
        ])
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT hook_name, hook_event, command, injected_chars FROM hook_events"
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("SessionStart:startup", "SessionStart",
                                "/home/user/.claude/hooks/startup.sh", 5))

    def test_rescan_does_not_duplicate_hook_events(self):
        self._write("a.jsonl", [_hook_success(uuid="u1", additional_context="hello")])
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)  # no-op: mtime unchanged

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM hook_events").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_incremental_append_also_captures_hook_events(self):
        path = self._write("a.jsonl", [_assistant(message_id="m1")])
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)

        time.sleep(0.05)
        with open(path, "a") as f:
            f.write(_hook_success(uuid="u-new", additional_context="new content here") + NL)
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT injected_chars FROM hook_events WHERE uuid = 'u-new'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], len("new content here"))

    def test_backfill_captures_hooks_from_already_processed_file(self):
        """Simulate a DB scanned before hook_events existed: the file is
        already fully recorded in processed_files (same mtime/line count), so
        a plain incremental scan would skip it — the one-time backfill must
        still pick up the hook_success records it already contains."""
        path = self._write("a.jsonl", [
            _assistant(message_id="m1"),
            _hook_success(uuid="u1", additional_context="preexisting"),
        ])
        conn = get_db(self.db_path)
        init_db(conn)
        # Pretend this file was already fully scanned under an older schema
        # (mtime + line count recorded, but hook_events never populated).
        mtime = os.path.getmtime(path)
        with open(path) as f:
            line_count = sum(1 for _ in f)
        conn.execute(
            "INSERT INTO processed_files (path, mtime, lines) VALUES (?, ?, ?)",
            (path, mtime, line_count),
        )
        from scanner import _meta_set
        _meta_set(conn, "hook_events_backfill_done", "0")
        conn.commit()
        conn.close()

        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT injected_chars FROM hook_events WHERE uuid = 'u1'").fetchone()
        skipped = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        conn.close()
        self.assertEqual(row[0], len("preexisting"))
        # The backfill must not touch turns (they were already correctly scanned).
        self.assertEqual(skipped, 0)

    def test_backfill_runs_only_once(self):
        self._write("a.jsonl", [_hook_success(uuid="u1")])
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM hook_events")
        conn.commit()
        conn.close()

        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)  # backfill marker already set

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM hook_events").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)


class TestDashboardHookInjections(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        with open(os.path.join(self.tmpdir, "a.jsonl"), "w") as f:
            f.write(_hook_success(
                uuid="u1", timestamp="2026-04-08T10:00:00Z",
                hook_name="SessionStart:startup", additional_context="x" * 50,
            ) + NL)
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)

    def test_hook_injections_in_dashboard_data(self):
        data = dashboard.get_dashboard_data(db_path=self.db_path)
        rows = data["hook_injections"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["hook_name"], "SessionStart:startup")
        self.assertEqual(rows[0]["chars"], 50)
        self.assertEqual(rows[0]["day"], "2026-04-08")
        self.assertEqual(rows[0]["events"], 1)


if __name__ == "__main__":
    unittest.main()
