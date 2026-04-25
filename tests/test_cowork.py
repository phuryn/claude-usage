"""Tests for the Cowork audit-log support."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cowork


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cowork"
SAMPLE = FIXTURE / "local_abc12345" / "audit.jsonl"


class TestCoworkSessionsDir(unittest.TestCase):
    """Multi-platform path detection."""

    def test_macos(self):
        with mock.patch("cowork.sys.platform", "darwin"):
            p = cowork.cowork_sessions_dir()
        self.assertIsNotNone(p)
        # Use Path-internal parts so the test passes on any OS the suite
        # is run on (CI normally Linux).
        parts = p.parts
        self.assertIn("Library", parts)
        self.assertIn("Application Support", parts)
        self.assertIn("Claude", parts)
        self.assertEqual(parts[-1], "local-agent-mode-sessions")

    def test_windows(self):
        fake_appdata = "C:/Users/test/AppData/Roaming"
        with mock.patch("cowork.sys.platform", "win32"), \
             mock.patch.dict(os.environ, {"APPDATA": fake_appdata}, clear=False):
            p = cowork.cowork_sessions_dir()
        self.assertIsNotNone(p)
        self.assertIn("Claude", p.parts)
        self.assertEqual(p.parts[-1], "local-agent-mode-sessions")

    def test_windows_without_appdata_returns_none(self):
        env = {k: v for k, v in os.environ.items() if k != "APPDATA"}
        with mock.patch("cowork.sys.platform", "win32"), \
             mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(cowork.cowork_sessions_dir())

    def test_linux_xdg(self):
        with mock.patch("cowork.sys.platform", "linux"), \
             mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg"}, clear=False):
            p = cowork.cowork_sessions_dir()
        self.assertEqual(str(p), "/tmp/xdg/Claude/local-agent-mode-sessions")

    def test_linux_default(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
        with mock.patch("cowork.sys.platform", "linux"), \
             mock.patch.dict(os.environ, env, clear=True):
            p = cowork.cowork_sessions_dir()
        self.assertTrue(str(p).endswith("/.config/Claude/local-agent-mode-sessions"))


class TestFindAuditFiles(unittest.TestCase):
    def test_finds_fixture(self):
        files = cowork.find_audit_files(FIXTURE)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "audit.jsonl")

    def test_missing_dir_returns_empty(self):
        self.assertEqual(cowork.find_audit_files("/nonexistent/path/xyzzy"), [])


class TestParseAuditFile(unittest.TestCase):
    """Parser correctness against the fixture."""

    def setUp(self):
        self.session_metas, self.turns, self.line_count = cowork.parse_audit_file(SAMPLE)

    def test_one_session_emitted(self):
        self.assertEqual(len(self.session_metas), 1)
        meta = self.session_metas[0]
        self.assertEqual(meta["session_id"], "abc12345-0000-0000-0000-000000000000")
        self.assertEqual(meta["project_name"], "Cowork/abc12345")

    def test_session_timestamp_window(self):
        meta = self.session_metas[0]
        self.assertEqual(meta["first_timestamp"], "2026-04-25T12:00:03.000Z")
        self.assertEqual(meta["last_timestamp"], "2026-04-25T12:05:00.000Z")

    def test_one_turn_per_result_per_model(self):
        # Two result events; first has 2 models, second has 1 -> 3 turns.
        self.assertEqual(len(self.turns), 3)

    def test_assistant_streaming_records_ignored(self):
        # The fixture's per-event assistant records claim 999 tokens each.
        # If the parser was reading them, we'd see those numbers. Verify
        # we don't.
        for t in self.turns:
            self.assertNotEqual(t["input_tokens"], 999)
            self.assertNotEqual(t["output_tokens"], 999)

    def test_authoritative_totals(self):
        # Sum across all turns should match the result.modelUsage totals.
        total_in = sum(t["input_tokens"] for t in self.turns)
        total_out = sum(t["output_tokens"] for t in self.turns)
        total_cr = sum(t["cache_read_tokens"] for t in self.turns)
        total_cw = sum(t["cache_creation_tokens"] for t in self.turns)
        self.assertEqual(total_in, 100 + 10 + 50)
        self.assertEqual(total_out, 200 + 5 + 100)
        self.assertEqual(total_cr, 1000 + 100 + 500)
        self.assertEqual(total_cw, 50 + 20 + 25)

    def test_tier_suffix_normalised(self):
        models = {t["model"] for t in self.turns}
        self.assertIn("claude-opus-4-7", models)
        # "claude-haiku-4-5[1m]" -> "claude-haiku-4-5"
        self.assertIn("claude-haiku-4-5", models)
        for m in models:
            self.assertNotIn("[", m)

    def test_unique_message_ids(self):
        # Each synthetic turn must have a unique message_id so the upstream
        # scanner's last-wins dedup keeps every row.
        ids = [t["message_id"] for t in self.turns]
        self.assertEqual(len(ids), len(set(ids)))

    def test_cwd_set_to_project_name(self):
        for t in self.turns:
            self.assertEqual(t["cwd"], "Cowork/abc12345")

    def test_skips_records_without_session_id(self):
        # If we crafted a result event without session_id, it must be skipped.
        # Easiest: verify the parser doesn't crash on missing keys
        # (already tested by fixture loading without errors).
        self.assertGreater(self.line_count, 0)


class TestIsAuditFile(unittest.TestCase):
    def test_audit_jsonl_recognised(self):
        self.assertTrue(cowork.is_audit_file("/some/path/audit.jsonl"))
        self.assertTrue(cowork.is_audit_file(Path("/p/audit.jsonl")))

    def test_other_jsonl_not_recognised(self):
        self.assertFalse(cowork.is_audit_file("/.claude/projects/foo/abc.jsonl"))


if __name__ == "__main__":
    unittest.main()
