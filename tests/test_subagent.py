"""Tests for subagent attribution: detection, agent-dispatch capture, scan integration."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scanner import (
    get_db, init_db, parse_jsonl_file, scan, extract_agent_attribution,
)

NL = chr(10)  # avoid backslash-escaped newline literals in source


def _assistant(session_id="s1", model="claude-opus-4-8",
               input_tokens=100, output_tokens=50,
               cache_read=0, cache_creation=0,
               timestamp="2026-04-08T10:00:00Z", cwd="/home/user/project",
               message_id="m1", extra=None):
    rec = {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "message": {
            "model": model,
            "id": message_id,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
            "content": [],
        },
    }
    if extra:
        rec.update(extra)
    return json.dumps(rec)


def _dispatch(session_id="s1", agent_id="agent-1", agent_type="Explore",
              timestamp="2026-04-08T10:01:00Z", total_tokens=999):
    return json.dumps({
        "type": "user",
        "sessionId": session_id,
        "timestamp": timestamp,
        "toolUseResult": {
            "agentId": agent_id,
            "agentType": agent_type,
            "status": "completed",
            "totalTokens": total_tokens,
            "totalDurationMs": 4200,
            "totalToolUseCount": 3,
        },
    })


class TestSubagentDetection(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write(self, relpath, lines):
        path = os.path.join(self.tmpdir, relpath)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(NL.join(lines) + NL)
        return path

    def test_sidechain_flag_marks_subagent(self):
        path = self._write("a.jsonl", [_assistant(extra={"isSidechain": True})])
        _, turns, _, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["is_subagent"], 1)

    def test_agent_id_marks_subagent_and_is_captured(self):
        path = self._write("a.jsonl", [_assistant(extra={"agentId": "agent-xyz"})])
        _, turns, _, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["is_subagent"], 1)
        self.assertEqual(turns[0]["agent_id"], "agent-xyz")

    def test_path_under_subagents_marks_subagent(self):
        path = self._write(os.path.join("proj", "subagents", "x.jsonl"),
                           [_assistant()])
        _, turns, _, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["is_subagent"], 1)

    def test_normal_record_not_subagent(self):
        path = self._write("a.jsonl", [_assistant()])
        _, turns, _, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["is_subagent"], 0)
        self.assertIsNone(turns[0]["agent_id"])

    def test_agent_dispatch_extracted_from_tool_result(self):
        path = self._write("a.jsonl", [_dispatch(agent_id="agent-xyz", agent_type="Plan",
                                                  total_tokens=1234)])
        _, _, agents, _ = parse_jsonl_file(path)
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["agent_id"], "agent-xyz")
        self.assertEqual(agents[0]["agent_type"], "Plan")
        self.assertEqual(agents[0]["total_tokens"], 1234)

    def test_tool_result_without_agent_fields_ignored(self):
        rec = json.dumps({"type": "user", "sessionId": "s1",
                          "toolUseResult": {"status": "ok"}})
        path = self._write("a.jsonl", [rec])
        _, _, agents, _ = parse_jsonl_file(path)
        self.assertEqual(agents, [])


class TestSubagentScanIntegration(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = Path(self.tmpdir) / "projects"
        self.projects_dir.mkdir()
        self.db_path = Path(self.tmpdir) / "usage.db"

    def test_scan_populates_agents_and_flags(self):
        parent = self.projects_dir / "user" / "proj"
        parent.mkdir(parents=True)
        with open(parent / "sess-1.jsonl", "w") as f:
            f.write(_assistant(session_id="sess-1", message_id="m-main",
                               input_tokens=100, output_tokens=50) + NL)
            f.write(_dispatch(session_id="sess-1", agent_id="agent-1",
                              agent_type="Explore", total_tokens=999) + NL)
        sub = parent / "subagents"
        sub.mkdir()
        with open(sub / "agent-1.jsonl", "w") as f:
            f.write(_assistant(session_id="sess-1", message_id="m-sub",
                               input_tokens=300, output_tokens=80,
                               extra={"agentId": "agent-1"}) + NL)

        scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        agent = conn.execute("SELECT * FROM agents WHERE agent_id='agent-1'").fetchone()
        self.assertIsNotNone(agent)
        self.assertEqual(agent["agent_type"], "Explore")
        self.assertEqual(agent["total_tokens"], 999)

        sub_turn = conn.execute("SELECT * FROM turns WHERE message_id='m-sub'").fetchone()
        self.assertEqual(sub_turn["is_subagent"], 1)
        self.assertEqual(sub_turn["agent_id"], "agent-1")

        main_turn = conn.execute("SELECT * FROM turns WHERE message_id='m-main'").fetchone()
        self.assertEqual(main_turn["is_subagent"], 0)
        conn.close()

    def test_migration_adds_subagent_columns_and_agents_table(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            "CREATE TABLE turns ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp TEXT,"
            " model TEXT, input_tokens INTEGER, output_tokens INTEGER,"
            " cache_read_tokens INTEGER, cache_creation_tokens INTEGER,"
            " tool_name TEXT, cwd TEXT, message_id TEXT);"
        )
        conn.commit()
        conn.close()

        conn = get_db(self.db_path)
        init_db(conn)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(turns)")}
        self.assertIn("is_subagent", cols)
        self.assertIn("agent_id", cols)
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("agents", tables)
        conn.close()


def _async_dispatch(session_id="s1", agent_id="agent-1",
                    timestamp="2026-04-08T10:01:00Z"):
    """A background (isAsync) dispatch launch record: agentId but no agentType."""
    return json.dumps({
        "type": "user",
        "sessionId": session_id,
        "timestamp": timestamp,
        "toolUseResult": {
            "agentId": agent_id,
            "isAsync": True,
            "status": "async_launched",
            "description": "Do the thing",
            "resolvedModel": "claude-sonnet-5",
        },
    })


class TestAgentAttribution(unittest.TestCase):
    """attributionAgent on the subagent's own records names it (issue: 'unknown')."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write(self, relpath, lines):
        path = os.path.join(self.tmpdir, relpath)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(NL.join(lines) + NL)
        return path

    def test_extract_reads_name_and_id(self):
        rec = {"type": "assistant", "agentId": "agent-x",
               "attributionAgent": "Explore"}
        self.assertEqual(extract_agent_attribution(rec),
                         {"source": "attribution", "agent_id": "agent-x",
                          "agent_type": "Explore"})

    def test_extract_requires_both_fields(self):
        self.assertIsNone(extract_agent_attribution({"agentId": "agent-x"}))
        self.assertIsNone(extract_agent_attribution({"attributionAgent": "Explore"}))

    def test_parse_captures_attribution(self):
        path = self._write(os.path.join("proj", "subagents", "agent-1.jsonl"), [
            _assistant(extra={"agentId": "agent-1", "attributionAgent": "herald"}),
        ])
        _, _, agents, _ = parse_jsonl_file(path)
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["agent_id"], "agent-1")
        self.assertEqual(agents[0]["agent_type"], "herald")
        self.assertEqual(agents[0]["source"], "attribution")

    def test_dispatch_name_wins_over_attribution(self):
        """A parent-supplied agentType is authoritative and is never clobbered."""
        projects = Path(self.tmpdir) / "projects"
        parent = projects / "user" / "proj"
        parent.mkdir(parents=True)
        with open(parent / "sess-1.jsonl", "w") as f:
            f.write(_dispatch(session_id="sess-1", agent_id="agent-1",
                              agent_type="Explore") + NL)
        sub = parent / "subagents"
        sub.mkdir()
        with open(sub / "agent-1.jsonl", "w") as f:
            f.write(_assistant(session_id="sess-1", message_id="m-sub",
                               extra={"agentId": "agent-1",
                                      "attributionAgent": "something-else"}) + NL)

        db = Path(self.tmpdir) / "usage.db"
        scan(projects_dir=projects, db_path=db, verbose=False)
        conn = sqlite3.connect(db)
        self.assertEqual(
            conn.execute("SELECT agent_type FROM agents WHERE agent_id='agent-1'")
                .fetchone()[0], "Explore")
        conn.close()

    def test_async_dispatch_is_named_from_attribution(self):
        """The real-world 'unknown' case: background dispatch, no agentType."""
        projects = Path(self.tmpdir) / "projects"
        parent = projects / "user" / "proj"
        parent.mkdir(parents=True)
        with open(parent / "sess-1.jsonl", "w") as f:
            f.write(_async_dispatch(session_id="sess-1", agent_id="agent-9") + NL)
        sub = parent / "subagents"
        sub.mkdir()
        with open(sub / "agent-9.jsonl", "w") as f:
            f.write(_assistant(session_id="sess-1", message_id="m-sub",
                               input_tokens=300,
                               extra={"agentId": "agent-9",
                                      "attributionAgent": "general-purpose"}) + NL)

        db = Path(self.tmpdir) / "usage.db"
        scan(projects_dir=projects, db_path=db, verbose=False)
        conn = sqlite3.connect(db)
        self.assertEqual(
            conn.execute("SELECT agent_type FROM agents WHERE agent_id='agent-9'")
                .fetchone()[0], "general-purpose")
        conn.close()

    def test_backfill_names_agents_in_already_scanned_db(self):
        """Files already in processed_files are skipped; the backfill covers them."""
        projects = Path(self.tmpdir) / "projects"
        parent = projects / "user" / "proj"
        parent.mkdir(parents=True)
        sub = parent / "subagents"
        sub.mkdir(parents=True)
        with open(sub / "agent-7.jsonl", "w") as f:
            f.write(_assistant(session_id="sess-1", message_id="m-sub",
                               extra={"agentId": "agent-7",
                                      "attributionAgent": "archivist"}) + NL)

        db = Path(self.tmpdir) / "usage.db"
        scan(projects_dir=projects, db_path=db, verbose=False)

        # Simulate a DB produced before attribution existed: drop the name and
        # the backfill marker, leaving processed_files intact.
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM agents")
        conn.execute("DELETE FROM schema_meta WHERE key='agent_type_backfill_done'")
        conn.commit()
        conn.close()

        scan(projects_dir=projects, db_path=db, verbose=False)

        conn = sqlite3.connect(db)
        self.assertEqual(
            conn.execute("SELECT agent_type FROM agents WHERE agent_id='agent-7'")
                .fetchone()[0], "archivist")
        conn.close()


if __name__ == "__main__":
    unittest.main()
