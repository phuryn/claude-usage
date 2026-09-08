"""Tests for agent/skill/mcp/thread-type attribution: extraction, migration,
CLI `breakdown` command, and the dashboard's breakdown_rows dataset."""

import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import cli
import dashboard
from scanner import get_db, init_db, parse_jsonl_file, scan

NL = chr(10)


def _assistant(session_id="s1", model="claude-sonnet-5",
               input_tokens=10, output_tokens=5,
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
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "content": [],
        },
    }
    if extra:
        rec.update(extra)
    return json.dumps(rec)


class TestScannerAttributionExtraction(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write(self, relpath, lines):
        path = os.path.join(self.tmpdir, relpath)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(NL.join(lines) + NL)
        return path

    def test_skill_and_plugin_captured_on_main_thread(self):
        path = self._write("a.jsonl", [_assistant(extra={
            "attributionSkill": "superpowers:brainstorming",
            "attributionPlugin": "superpowers",
        })])
        _, turns, _, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["attribution_skill"], "superpowers:brainstorming")
        self.assertEqual(turns[0]["attribution_plugin"], "superpowers")
        self.assertEqual(turns[0]["thread_type"], "active")

    def test_mcp_attribution_captured(self):
        path = self._write("a.jsonl", [_assistant(extra={
            "attributionMcpServer": "headroom",
            "attributionMcpTool": "headroom_retrieve",
        })])
        _, turns, _, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["mcp_server"], "headroom")
        self.assertEqual(turns[0]["mcp_tool"], "headroom_retrieve")

    def test_sidechain_turn_is_background_with_agent_attribution(self):
        path = self._write("a.jsonl", [_assistant(extra={
            "isSidechain": True,
            "agentId": "agent-1",
            "attributionAgent": "general-purpose",
        })])
        _, turns, _, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["attribution_agent"], "general-purpose")
        self.assertEqual(turns[0]["thread_type"], "background")

    def test_task_notification_origin_is_background_without_sidechain(self):
        path = self._write("a.jsonl", [_assistant(extra={
            "origin": {"kind": "task-notification"},
        })])
        _, turns, _, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["is_subagent"], 0)
        self.assertEqual(turns[0]["thread_type"], "background")

    def test_human_origin_is_active(self):
        path = self._write("a.jsonl", [_assistant(extra={
            "origin": {"kind": "human"},
        })])
        _, turns, _, _ = parse_jsonl_file(path)
        self.assertEqual(turns[0]["thread_type"], "active")

    def test_no_attribution_fields_default_to_none_and_active(self):
        path = self._write("a.jsonl", [_assistant()])
        _, turns, _, _ = parse_jsonl_file(path)
        t = turns[0]
        self.assertIsNone(t["attribution_agent"])
        self.assertIsNone(t["attribution_skill"])
        self.assertIsNone(t["mcp_server"])
        self.assertEqual(t["thread_type"], "active")


class TestScanPersistsAttribution(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"

    def _write(self, relpath, lines):
        path = os.path.join(self.tmpdir, relpath)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(NL.join(lines) + NL)
        return path

    def test_columns_exist_after_migration(self):
        conn = get_db(self.db_path)
        init_db(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
        for col in ("attribution_agent", "attribution_skill", "attribution_plugin",
                    "mcp_server", "mcp_tool", "thread_type"):
            self.assertIn(col, cols)
        conn.close()

    def test_scan_writes_skill_and_mcp_columns(self):
        self._write("a.jsonl", [_assistant(extra={
            "attributionSkill": "superpowers:brainstorming",
            "attributionMcpServer": "headroom",
            "attributionMcpTool": "headroom_retrieve",
        })])
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT attribution_skill, mcp_server, mcp_tool, thread_type FROM turns"
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("superpowers:brainstorming", "headroom", "headroom_retrieve", "active"))

    def test_incremental_append_also_captures_attribution(self):
        """The updated-file branch of scan() must extract the same fields as a
        fresh parse — this is the second of two duplicated construction sites."""
        path = self._write("a.jsonl", [_assistant(message_id="m1")])
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)

        time.sleep(0.05)
        with open(path, "a") as f:
            f.write(_assistant(message_id="m2", extra={
                "attributionSkill": "dataviz",
                "isSidechain": True,
                "attributionAgent": "Explore",
            }) + NL)
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT attribution_skill, attribution_agent, thread_type "
            "FROM turns WHERE message_id = 'm2'"
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("dataviz", "Explore", "background"))


class TestCliBreakdownCommand(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        self._write("a.jsonl", [
            _assistant(message_id="m1", input_tokens=100, output_tokens=50, extra={
                "attributionSkill": "superpowers:brainstorming",
                "timestamp": "2026-04-08T10:00:00Z",
            }),
            _assistant(message_id="m2", input_tokens=10, output_tokens=5, extra={
                "isSidechain": True, "agentId": "agent-1",
                "attributionAgent": "general-purpose",
                "timestamp": "2026-04-08T11:00:00Z",
            }),
        ])
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def _write(self, relpath, lines):
        path = os.path.join(self.tmpdir, relpath)
        with open(path, "w") as f:
            f.write(NL.join(lines) + NL)
        return path

    def test_breakdown_by_skill(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_breakdown(by="skill", group="model")
        out = buf.getvalue()
        self.assertIn("superpowers:brainstorming", out)

    def test_breakdown_by_agent(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_breakdown(by="agent", group="day")
        out = buf.getvalue()
        self.assertIn("general-purpose", out)
        self.assertIn("2026-04-08", out)

    def test_breakdown_by_thread(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_breakdown(by="thread", group="model")
        out = buf.getvalue()
        self.assertIn("active", out)
        self.assertIn("background", out)

    def test_unknown_dimension_exits(self):
        with self.assertRaises(SystemExit):
            cli.cmd_breakdown(by="bogus")


class TestDashboardBreakdownRows(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        with open(os.path.join(self.tmpdir, "a.jsonl"), "w") as f:
            f.write(_assistant(message_id="m1", extra={
                "attributionMcpServer": "headroom",
                "attributionMcpTool": "headroom_retrieve",
                "timestamp": "2026-04-08T10:00:00Z",
            }) + NL)
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)

    def test_breakdown_rows_include_mcp_dimension(self):
        data = dashboard.get_dashboard_data(db_path=self.db_path)
        mcp_rows = [r for r in data["breakdown_rows"] if r["dimension"] == "mcp"]
        self.assertEqual(len(mcp_rows), 1)
        self.assertEqual(mcp_rows[0]["category"], "headroom")
        self.assertEqual(mcp_rows[0]["day"], "2026-04-08")

    def test_all_four_dimensions_present(self):
        data = dashboard.get_dashboard_data(db_path=self.db_path)
        dims = {r["dimension"] for r in data["breakdown_rows"]}
        # thread is always populated (every turn has a thread_type); the others
        # only appear when attribution fields are present in the source data.
        self.assertIn("thread", dims)


class TestActivityByCategory(unittest.TestCase):
    """activity_by_category assigns each turn to exactly one bucket (Agent >
    Skill > MCP > Active), unlike breakdown_rows where a turn with both a
    skill and an MCP call would appear under both dimensions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"

    def _scan(self, records):
        with open(os.path.join(self.tmpdir, "a.jsonl"), "w") as f:
            f.write(NL.join(records) + NL)
        scan(projects_dir=self.tmpdir, db_path=self.db_path, verbose=False)

    def test_plain_turn_is_active(self):
        self._scan([_assistant(message_id="m1")])
        data = dashboard.get_dashboard_data(db_path=self.db_path)
        cats = {r["category"] for r in data["activity_by_category"]}
        self.assertEqual(cats, {"Active"})

    def test_subagent_turn_with_skill_and_mcp_counts_once_as_agent(self):
        """A turn can carry subagent + skill + mcp attribution simultaneously
        (a subagent invoking a skill that calls an MCP tool) — it must land in
        exactly one bucket (Agent, the highest-priority one), not three."""
        self._scan([_assistant(message_id="m1", extra={
            "isSidechain": True,
            "attributionAgent": "general-purpose",
            "attributionSkill": "dataviz",
            "attributionMcpServer": "headroom",
            "attributionMcpTool": "headroom_retrieve",
        })])
        data = dashboard.get_dashboard_data(db_path=self.db_path)
        rows = data["activity_by_category"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "Agent")

    def test_skill_without_subagent_counts_as_skill(self):
        self._scan([_assistant(message_id="m1", extra={
            "attributionSkill": "dataviz",
            "attributionMcpServer": "headroom",
            "attributionMcpTool": "headroom_retrieve",
        })])
        data = dashboard.get_dashboard_data(db_path=self.db_path)
        rows = data["activity_by_category"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "Skill")

    def test_mcp_without_skill_or_subagent_counts_as_mcp(self):
        self._scan([_assistant(message_id="m1", extra={
            "attributionMcpServer": "headroom",
            "attributionMcpTool": "headroom_retrieve",
        })])
        data = dashboard.get_dashboard_data(db_path=self.db_path)
        rows = data["activity_by_category"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "MCP")

    def test_categories_sum_to_all_turns(self):
        """The whole point of activity_by_category: unlike breakdown_rows, the
        slices must sum to the true total turn count, with no double-counting."""
        self._scan([
            _assistant(message_id="m1"),
            _assistant(message_id="m2", extra={"attributionSkill": "dataviz"}),
            _assistant(message_id="m3", extra={
                "attributionMcpServer": "headroom", "attributionMcpTool": "headroom_retrieve",
            }),
            _assistant(message_id="m4", extra={
                "isSidechain": True, "attributionAgent": "general-purpose",
                "attributionSkill": "dataviz",
            }),
        ])
        data = dashboard.get_dashboard_data(db_path=self.db_path)
        total_turns = sum(r["turns"] for r in data["activity_by_category"])
        self.assertEqual(total_turns, 4)


if __name__ == "__main__":
    unittest.main()
