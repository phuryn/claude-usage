"""Tests for git-aware project naming and branch resolution — the fix for
worktrees fragmenting one repo's usage across several bogus "projects" (see
scanner._resolve_git / project_name_from_cwd / git_branch_from_cwd)."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scanner import (
    get_db, init_db, parse_jsonl_file, scan,
    project_name_from_cwd, git_branch_from_cwd, _resolve_git,
    _backfill_project_names,
)

NL = chr(10)


def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _init_repo(root):
    """Create a real git repo at `root` with one commit on `main`."""
    root.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], root)
    _run(["git", "config", "user.email", "test@example.com"], root)
    _run(["git", "config", "user.name", "Test"], root)
    (root / "README.md").write_text("hello")
    _run(["git", "add", "README.md"], root)
    _run(["git", "commit", "-q", "-m", "init"], root)


def _add_worktree(root, worktree_path, branch):
    _run(["git", "worktree", "add", "-q", "-b", branch, str(worktree_path)], root)


def _assistant(session_id, cwd, model="claude-sonnet-5",
               timestamp="2026-04-08T10:00:00Z", message_id="m1", extra=None):
    rec = {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "message": {
            "model": model,
            "id": message_id,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [],
        },
    }
    if extra:
        rec.update(extra)
    return json.dumps(rec)


@unittest.skipUnless(
    subprocess.run(["git", "--version"], capture_output=True).returncode == 0,
    "git not available",
)
class TestGitWorktreeResolution(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.main_root = self.base / "myrepo"
        _init_repo(self.main_root)
        self.worktree_root = self.base / "myrepo-feature"
        _add_worktree(self.main_root, self.worktree_root, "feature/x")

    def test_main_checkout_project_name(self):
        name = project_name_from_cwd(str(self.main_root))
        self.assertEqual(name, f"{self.base.name}/myrepo")

    def test_worktree_collapses_to_same_project_as_main(self):
        main_name = project_name_from_cwd(str(self.main_root))
        wt_name = project_name_from_cwd(str(self.worktree_root))
        self.assertEqual(main_name, wt_name)

    def test_worktree_reports_its_own_branch(self):
        _, branch = _resolve_git(str(self.worktree_root))
        self.assertEqual(branch, "feature/x")

    def test_main_checkout_reports_main_branch(self):
        _, branch = _resolve_git(str(self.main_root))
        self.assertEqual(branch, "main")

    def test_subdirectory_of_worktree_still_resolves(self):
        subdir = self.worktree_root / "src" / "nested"
        subdir.mkdir(parents=True)
        name = project_name_from_cwd(str(subdir))
        self.assertEqual(name, project_name_from_cwd(str(self.main_root)))

    def test_git_branch_from_cwd_fallback(self):
        self.assertEqual(git_branch_from_cwd(str(self.worktree_root)), "feature/x")

    def test_nonexistent_path_falls_back_to_path_heuristic(self):
        fake = str(self.base / "does-not-exist" / "sub")
        name = project_name_from_cwd(fake)
        self.assertEqual(name, "does-not-exist/sub")

    def test_non_git_directory_falls_back_to_path_heuristic(self):
        plain = self.base / "not-a-repo" / "sub"
        plain.mkdir(parents=True)
        name = project_name_from_cwd(str(plain))
        self.assertEqual(name, "not-a-repo/sub")


@unittest.skipUnless(
    subprocess.run(["git", "--version"], capture_output=True).returncode == 0,
    "git not available",
)
class TestScanUnifiesWorktreeProjects(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.main_root = self.base / "myrepo"
        _init_repo(self.main_root)
        self.worktree_root = self.base / "myrepo-feature"
        _add_worktree(self.main_root, self.worktree_root, "feature/x")

        self.transcripts_dir = Path(tempfile.mkdtemp())
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"

        with open(self.transcripts_dir / "main.jsonl", "w") as f:
            f.write(_assistant("sess-main", str(self.main_root), message_id="m-main") + NL)
        with open(self.transcripts_dir / "wt.jsonl", "w") as f:
            f.write(_assistant("sess-wt", str(self.worktree_root), message_id="m-wt") + NL)

    def test_sessions_from_main_and_worktree_share_project_name(self):
        scan(projects_dir=self.transcripts_dir, db_path=self.db_path, verbose=False)
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = {r["session_id"]: r for r in conn.execute(
            "SELECT session_id, project_name, git_branch FROM sessions")}
        conn.close()

        self.assertEqual(rows["sess-main"]["project_name"], rows["sess-wt"]["project_name"])
        self.assertEqual(rows["sess-main"]["git_branch"], "main")
        self.assertEqual(rows["sess-wt"]["git_branch"], "feature/x")

    def test_backfill_recomputes_pre_existing_bogus_project_names(self):
        """Simulate a DB scanned before git-aware naming existed: project_name
        was the raw path heuristic (so main/worktree looked like two projects).
        The backfill should collapse them without touching turns."""
        conn = get_db(self.db_path)
        init_db(conn)
        from scanner import upsert_sessions, insert_turns
        upsert_sessions(conn, [{
            "session_id": "sess-main", "project_name": str(self.base.name) + "/" + "myrepo",
            "first_timestamp": "2026-04-08T10:00:00Z", "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "main", "model": "claude-sonnet-5",
            "total_input_tokens": 10, "total_output_tokens": 5,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1,
        }, {
            "session_id": "sess-wt", "project_name": "myrepo-feature",  # bogus: 1 path component
            "first_timestamp": "2026-04-08T10:00:00Z", "last_timestamp": "2026-04-08T10:00:00Z",
            "git_branch": "feature/x", "model": "claude-sonnet-5",
            "total_input_tokens": 10, "total_output_tokens": 5,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 1,
        }])
        insert_turns(conn, [{
            "session_id": "sess-main", "timestamp": "2026-04-08T10:00:00Z", "model": "claude-sonnet-5",
            "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": str(self.main_root), "message_id": "m-main",
        }, {
            "session_id": "sess-wt", "timestamp": "2026-04-08T10:00:00Z", "model": "claude-sonnet-5",
            "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "tool_name": None, "cwd": str(self.worktree_root), "message_id": "m-wt",
        }])
        conn.commit()

        updated = _backfill_project_names(conn)
        self.assertGreaterEqual(updated, 1)

        rows = {r["session_id"]: r["project_name"] for r in conn.execute(
            "SELECT session_id, project_name FROM sessions")}
        conn.close()
        self.assertEqual(rows["sess-main"], rows["sess-wt"])


if __name__ == "__main__":
    unittest.main()
