"""Tests for the dashboard's subagent data layer (get_dashboard_data)."""

import tempfile
import unittest
from pathlib import Path

from scanner import get_db, init_db, insert_turns, upsert_agents, upsert_sessions
import dashboard


def _turn(session_id, message_id, model="claude-opus-4-8",
          inp=100, out=50, is_subagent=0, agent_id=None,
          timestamp="2026-04-08T10:00:00Z"):
    return {
        "session_id": session_id, "timestamp": timestamp, "model": model,
        "input_tokens": inp, "output_tokens": out,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_name": None, "cwd": "/home/user/proj",
        "message_id": message_id, "is_subagent": is_subagent, "agent_id": agent_id,
    }


class TestDashboardSubagentData(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-1", "project_name": "user/proj",
            "first_timestamp": "2026-04-08T10:00:00Z",
            "last_timestamp": "2026-04-08T10:30:00Z",
            "git_branch": "main", "model": "claude-opus-4-8",
            "total_input_tokens": 400, "total_output_tokens": 210,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 3,
        }])
        insert_turns(conn, [
            _turn("sess-1", "m-main", inp=100, out=50, is_subagent=0),
            _turn("sess-1", "m-sub1", inp=300, out=80, is_subagent=1, agent_id="agent-1"),
            _turn("sess-1", "m-sub2", inp=200, out=40, is_subagent=1, agent_id="acompact-xyz"),
        ])
        upsert_agents(conn, [{
            "agent_id": "agent-1", "agent_type": "Explore",
            "dispatched_in_session": "sess-1", "completed_at": "2026-04-08T10:20:00Z",
            "status": "completed", "total_tokens": 380,
            "total_duration_ms": 4200, "tool_use_count": 5,
        }])
        conn.commit()
        conn.close()

    def test_returns_subagent_keys(self):
        d = dashboard.get_dashboard_data(self.db_path)
        self.assertIn("subagent_by_type", d)
        self.assertIn("top_dispatches", d)

    def test_subagent_by_type_resolves_agent_type(self):
        d = dashboard.get_dashboard_data(self.db_path)
        types = {r["agent_type"] for r in d["subagent_by_type"]}
        # agent-1 -> Explore (from agents table); acompact-* -> auto-compact
        self.assertIn("Explore", types)
        self.assertIn("auto-compact", types)

    def test_top_dispatches_carries_dispatch_metadata(self):
        d = dashboard.get_dashboard_data(self.db_path)
        explore = [r for r in d["top_dispatches"] if r["agent_type"] == "Explore"]
        self.assertEqual(len(explore), 1)
        self.assertEqual(explore[0]["tool_uses"], 5)
        self.assertEqual(explore[0]["duration_ms"], 4200)
        self.assertEqual(explore[0]["turns"], 1)

    def test_main_turn_excluded_from_subagent_data(self):
        d = dashboard.get_dashboard_data(self.db_path)
        # Only the 2 subagent turns contribute; the main turn must not appear.
        total_turns = sum(r["turns"] for r in d["subagent_by_type"])
        self.assertEqual(total_turns, 2)


if __name__ == "__main__":
    unittest.main()
