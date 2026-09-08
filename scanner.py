"""
scanner.py - Scans Claude Code JSONL transcript files and stores data in SQLite.
"""

import json
import os
import glob
import sqlite3
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timezone

# Single source of truth for the app version reported by the CLI (`--version`)
# and the dashboard footer. CHANGELOG.md is the canonical version reference, but
# it isn't bundled into the .vsix — only the three Python files are — so the
# runtime version has to live here as a constant. Keep this in lockstep with the
# top CHANGELOG heading and vscode-extension/package.json (a parity test guards
# all three; see tests/test_version.py).
VERSION = "1.6.0"

PROJECTS_DIR = Path.home() / ".claude" / "projects"
XCODE_PROJECTS_DIR = Path.home() / "Library" / "Developer" / "Xcode" / "CodingAssistant" / "ClaudeAgentConfig" / "projects"
DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))
DEFAULT_PROJECTS_DIRS = [PROJECTS_DIR, XCODE_PROJECTS_DIR]

# Higher number = higher priority when choosing a session's primary model.
# Fable / Mythos are Anthropic's most capable class, so they outrank Opus.
MODEL_PRIORITY = {"fable": 5, "mythos": 5, "opus": 3, "sonnet": 2, "haiku": 1}


def _model_priority(model):
    """Return a priority score for a model name (higher = more capable)."""
    if not model:
        return 0
    m = model.lower()
    for keyword, priority in MODEL_PRIORITY.items():
        if keyword in m:
            return priority
    return 0


def get_db(db_path=DB_PATH):
    # Ensure the parent directory exists — on a fresh install or CI runner
    # ~/.claude may not yet exist, and sqlite3.connect needs the parent dir.
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            project_name    TEXT,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            git_branch      TEXT,
            total_input_tokens      INTEGER DEFAULT 0,
            total_output_tokens     INTEGER DEFAULT 0,
            total_cache_read        INTEGER DEFAULT 0,
            total_cache_creation    INTEGER DEFAULT 0,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0,
            topic           TEXT
        );

        CREATE TABLE IF NOT EXISTS turns (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              TEXT,
            timestamp               TEXT,
            model                   TEXT,
            input_tokens            INTEGER DEFAULT 0,
            output_tokens           INTEGER DEFAULT 0,
            cache_read_tokens       INTEGER DEFAULT 0,
            cache_creation_tokens   INTEGER DEFAULT 0,
            tool_name               TEXT,
            cwd                     TEXT,
            message_id              TEXT,
            is_subagent             INTEGER DEFAULT 0,
            agent_id                TEXT,
            attribution_agent       TEXT,
            attribution_skill       TEXT,
            attribution_plugin      TEXT,
            mcp_server              TEXT,
            mcp_tool                TEXT,
            thread_type             TEXT
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            lines   INTEGER
        );

        CREATE TABLE IF NOT EXISTS agents (
            agent_id              TEXT PRIMARY KEY,
            agent_type            TEXT,
            dispatched_in_session TEXT,
            completed_at          TEXT,
            status                TEXT,
            total_tokens          INTEGER,
            total_duration_ms     INTEGER,
            tool_use_count        INTEGER
        );

        CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS hook_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid            TEXT,
            session_id      TEXT,
            timestamp       TEXT,
            cwd             TEXT,
            hook_event      TEXT,
            hook_name       TEXT,
            command         TEXT,
            injected_chars  INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
        CREATE INDEX IF NOT EXISTS idx_agents_type ON agents(agent_type);
    """)
    # Add message_id column if upgrading from older schema
    try:
        conn.execute("SELECT message_id FROM turns LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE turns ADD COLUMN message_id TEXT")
    # Subagent attribution columns (added in a later schema version)
    _ensure_column(conn, "turns", "is_subagent", "INTEGER DEFAULT 0")
    _ensure_column(conn, "turns", "agent_id", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_subagent ON turns(is_subagent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_agent_id ON turns(agent_id)")
    # Attribution columns (agent/skill/plugin/mcp/thread-type; added in a later
    # schema version) — power the breakdown views by category x model/day/month.
    _ensure_column(conn, "turns", "attribution_agent", "TEXT")
    _ensure_column(conn, "turns", "attribution_skill", "TEXT")
    _ensure_column(conn, "turns", "attribution_plugin", "TEXT")
    _ensure_column(conn, "turns", "mcp_server", "TEXT")
    _ensure_column(conn, "turns", "mcp_tool", "TEXT")
    _ensure_column(conn, "turns", "thread_type", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_skill ON turns(attribution_skill)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_mcp_server ON turns(mcp_server)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_thread_type ON turns(thread_type)")
    # Session topic (from custom-title / ai-title records; added in a later
    # schema version). The one-time backfill of pre-existing sessions is driven
    # by scan() via the schema_meta 'topic_backfill_done' marker (not by the
    # column-add event), so it also covers DBs that gained the column from an
    # earlier build that predated the backfill.
    _ensure_column(conn, "sessions", "topic", "TEXT")
    # Conditional unique index: only dedup non-null message IDs
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_message_id
        ON turns(message_id) WHERE message_id IS NOT NULL AND message_id != ''
    """)
    # hook_events: dedup by uuid (every JSONL record carries one) the same way
    # turns dedup by message_id, so a rescan doesn't double-count injections.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_hook_events_uuid
        ON hook_events(uuid) WHERE uuid IS NOT NULL AND uuid != ''
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hook_events_session ON hook_events(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hook_events_timestamp ON hook_events(timestamp)")
    conn.commit()


def _ensure_column(conn, table, column, decl):
    """Add a column to an existing table if it isn't already present.

    Returns True if the column was just added (an upgrade of an existing DB),
    False if it was already there (fresh DB or already-migrated).
    """
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        return True
    return False


def _meta_get(conn, key):
    """Read a value from the schema_meta key/value table (None if absent)."""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn, key, value):
    """Upsert a value into the schema_meta key/value table."""
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
        (key, value))


def _extract_title(record):
    """Extract a session title from a custom-title or ai-title record."""
    rtype = record.get("type")
    if rtype == "custom-title":
        return record.get("customTitle")
    if rtype == "ai-title":
        return record.get("aiTitle")
    return None


def _backfill_topics(conn, jsonl_files):
    """One-time backfill of topics for a DB created before topic support.

    Transcript files scanned before the topic column existed are already in
    processed_files, so an incremental scan skips them and never sees the
    custom-title / ai-title records they already contain. Re-read just those
    records (turns are left untouched, so token totals cannot drift) and set the
    topic for any session that doesn't have one yet. Runs once, gated by a flag
    in schema_meta (see scan()). Returns the number of sessions filled.
    """
    needing = {r["session_id"] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE topic IS NULL OR topic = ''")}
    if not needing:
        return 0

    titles = {}          # session_id -> chosen title
    has_custom = set()   # sessions whose topic came from a custom-title record
    for filepath in jsonl_files:
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    # Cheap prefilter: only title records carry the substring
                    # "title" (in their "custom-title" / "ai-title" type), so we
                    # skip JSON-parsing the ~99% of lines that are turns.
                    if "title" not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    title = _extract_title(record)
                    if not title:
                        continue
                    sid = record.get("sessionId")
                    if sid not in needing:
                        continue
                    # custom-title wins; ai-title only if no custom-title seen.
                    if record.get("type") == "custom-title":
                        titles[sid] = title
                        has_custom.add(sid)
                    elif sid not in has_custom:
                        titles.setdefault(sid, title)
        except Exception as e:
            print(f"  Warning: error reading {filepath}: {e}")

    for sid, title in titles.items():
        conn.execute(
            "UPDATE sessions SET topic = ? WHERE session_id = ? "
            "AND (topic IS NULL OR topic = '')", (title, sid))
    conn.commit()
    return len(titles)


def _backfill_hook_events(conn, jsonl_files):
    """One-time backfill of hook_events for a DB scanned before hook-injection
    tracking existed.

    Transcript files scanned before the hook_events table existed are already
    in processed_files, so an incremental scan skips them and never sees the
    hook_success attachment records they already contain. Re-read just those
    records (turns are left untouched) and insert them. Runs once, gated by
    the schema_meta 'hook_events_backfill_done' marker (see scan()). Returns
    the number of hook events inserted.
    """
    hook_events = []
    for filepath in jsonl_files:
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    # Cheap prefilter: only hook_success attachments carry this
                    # substring, so we skip JSON-parsing everything else.
                    if '"hook_success"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    injection = extract_hook_injection(record)
                    if injection is not None:
                        hook_events.append(injection)
        except Exception as e:
            print(f"  Warning: error reading {filepath}: {e}")

    insert_hook_events(conn, hook_events)
    conn.commit()
    return len(hook_events)


def _project_name_from_path(path_str):
    """Last-two-path-components heuristic, e.g. '/home/user/proj' -> 'user/proj'."""
    if not path_str:
        return "unknown"
    parts = str(path_str).replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


def _read_gitdir_pointer(git_file_path):
    """Resolve a `gitdir: <path>` pointer file (worktrees and submodules use a
    `.git` *file* instead of a directory) to the git dir it points at."""
    try:
        content = git_file_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    target = content[len("gitdir:"):].strip()
    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = (git_file_path.parent / target_path)
    try:
        return target_path.resolve()
    except OSError:
        return target_path


def _find_git_dir(start_path):
    """Walk upward from start_path for the nearest `.git`, resolving worktree/
    submodule pointer files. Returns the resolved git dir, or None."""
    try:
        p = Path(start_path)
        if not p.exists():
            return None
        p = p.resolve()
    except OSError:
        return None
    for candidate in [p, *p.parents]:
        git_path = candidate / ".git"
        if git_path.is_dir():
            return git_path
        if git_path.is_file():
            resolved = _read_gitdir_pointer(git_path)
            if resolved is not None:
                return resolved
    return None


def _git_common_dir(git_dir):
    """Resolve a worktree's private git dir to the *main* repo's `.git` via the
    `commondir` file it contains (absent for a non-worktree repo, which is
    already the common dir)."""
    commondir_file = git_dir / "commondir"
    try:
        rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return git_dir
    try:
        return (git_dir / rel).resolve()
    except OSError:
        return git_dir


@lru_cache(maxsize=4096)
def _resolve_git(cwd):
    """Resolve (main_worktree_root, branch) for cwd from on-disk git metadata.

    Every worktree of a repo has its own `.git` file pointing at a private git
    dir under the main repo's `.git/worktrees/<name>/`; that private dir's
    `commondir` file points back at the main repo's `.git`. Walking that chain
    lets every worktree resolve to the *same* main_worktree_root (so they group
    as one project) while HEAD is still read from the worktree's own private
    git dir (so each worktree reports its own branch). Returns (None, None) if
    cwd isn't inside an accessible git working tree — callers fall back to the
    plain path heuristic in that case.
    """
    if not cwd:
        return None, None
    git_dir = _find_git_dir(cwd)
    if git_dir is None:
        return None, None

    common_dir = _git_common_dir(git_dir)
    main_root = common_dir.parent if common_dir.name == ".git" else None

    branch = None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        head = ""
    prefix = "ref: refs/heads/"
    if head.startswith(prefix):
        branch = head[len(prefix):]
    # A bare SHA (detached HEAD) yields branch=None — nothing sensible to report.

    return main_root, branch


def project_name_from_cwd(cwd):
    """Derive a friendly project name from cwd, preferring git repo identity.

    Plain path heuristics give every git worktree its own project (their
    directories differ), fragmenting one repo's usage across several bogus
    "projects". Resolving the actual main-worktree root via git metadata
    collapses all of a repo's worktrees back into a single project. Falls back
    to the path heuristic when cwd isn't a real, accessible git working tree
    (also covers the unit tests' fictional paths).
    """
    if not cwd:
        return "unknown"
    main_root, _ = _resolve_git(cwd)
    if main_root is not None:
        return _project_name_from_path(str(main_root))
    return _project_name_from_path(cwd)


def git_branch_from_cwd(cwd):
    """Filesystem fallback for a record's git branch, used only when the JSONL
    record itself doesn't carry a `gitBranch` (never overrides a real value).
    Reads HEAD from cwd's own git dir, so it's worktree-correct."""
    if not cwd:
        return None
    _, branch = _resolve_git(cwd)
    return branch


def _backfill_project_names(conn):
    """One-time recompute of every session's project_name with the git-aware
    resolver, for DBs scanned before worktree-collapsing existed.

    A session's project_name was previously derived once, from the cwd of its
    *first* turn, and never revisited — so this reproduces that same rule
    (rather than e.g. the most common cwd) to stay consistent with how new
    scans set it. Only touches sessions whose recomputed name actually
    differs, and only if the working tree is still present on disk (deleted
    repos/worktrees fall back to the original path-based name, unchanged).
    Runs once, gated by the schema_meta 'project_git_backfill_done' marker
    (see scan()). Returns the number of sessions updated.
    """
    rows = conn.execute("""
        SELECT s.session_id, s.project_name, (
            SELECT t.cwd FROM turns t
            WHERE t.session_id = s.session_id
            ORDER BY t.timestamp ASC, t.id ASC LIMIT 1
        ) AS cwd
        FROM sessions s
    """).fetchall()

    updated = 0
    for r in rows:
        if not r["cwd"]:
            continue
        new_name = project_name_from_cwd(r["cwd"])
        if new_name != r["project_name"]:
            conn.execute(
                "UPDATE sessions SET project_name = ? WHERE session_id = ?",
                (new_name, r["session_id"])
            )
            updated += 1
    conn.commit()
    return updated


def is_subagent_record(record, source_path=""):
    """True if a record belongs to a dispatched subagent (Task/Agent tool).

    Subagents are detected three ways: an explicit ``isSidechain`` flag, an
    ``agentId`` on the record (or its ``data`` wrapper), or a transcript path
    under a ``subagents`` directory (Claude Code writes one jsonl per subagent).
    """
    if record.get("isSidechain"):
        return True
    if record.get("agentId"):
        return True
    data = record.get("data")
    if isinstance(data, dict) and data.get("agentId"):
        return True
    sp = str(source_path).replace("\\", "/").lower()
    return "/subagents/" in sp


def record_agent_id(record):
    """Pull the subagent id off a record, if any (top-level or data wrapper)."""
    agent_id = record.get("agentId")
    if not agent_id:
        data = record.get("data")
        if isinstance(data, dict):
            agent_id = data.get("agentId")
    return agent_id


def _origin_kind(record):
    """Pull ``origin.kind`` off a record (e.g. 'human', 'task-notification')."""
    origin = record.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind")
    return None


# origin.kind values that mark a turn as unattended (a scheduled wakeup fired,
# an auto-continuation resumed, or a coordinator loop stepped) rather than a
# human directly driving the turn. Subagent turns (isSidechain) are always
# background regardless of origin.
_BACKGROUND_ORIGIN_KINDS = {"task-notification", "auto-continuation", "coordinator"}


def thread_type(record, is_subagent):
    """Classify a turn as 'background' or 'active' for the thread-type breakdown."""
    if is_subagent or _origin_kind(record) in _BACKGROUND_ORIGIN_KINDS:
        return "background"
    return "active"


def build_turn(record, filepath, session_id, timestamp, cwd, model, message_id,
               input_tokens, output_tokens, cache_read, cache_creation, tool_name):
    """Assemble a turn dict, including subagent/skill/mcp/thread attribution.

    Shared by the full-file parse (parse_jsonl_file) and the incremental-append
    branch in scan() so the two stay in sync on what gets attributed.
    """
    is_subagent = is_subagent_record(record, filepath)
    return {
        "session_id": session_id,
        "timestamp": timestamp,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "tool_name": tool_name,
        "cwd": cwd,
        "message_id": message_id,
        "is_subagent": 1 if is_subagent else 0,
        "agent_id": record_agent_id(record),
        "attribution_agent": record.get("attributionAgent"),
        "attribution_skill": record.get("attributionSkill"),
        "attribution_plugin": record.get("attributionPlugin"),
        "mcp_server": record.get("attributionMcpServer"),
        "mcp_tool": record.get("attributionMcpTool"),
        "thread_type": thread_type(record, is_subagent),
    }


def extract_agent_dispatch(record):
    """Pull subagent identity from a parent's tool_result record.

    Claude Code writes a ``toolUseResult`` dict on the user-side record that
    closes out an Agent/Task tool invocation. It carries ``agentId`` (matching
    the subagent jsonl's records) and ``agentType`` (the human-readable type
    such as 'general-purpose' or 'Explore') plus aggregate stats.
    """
    if record.get("type") != "user":
        return None
    tur = record.get("toolUseResult")
    if not isinstance(tur, dict):
        return None
    agent_id = tur.get("agentId")
    agent_type = tur.get("agentType")
    if not agent_id or not agent_type:
        return None
    return {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "dispatched_in_session": record.get("sessionId"),
        "completed_at": record.get("timestamp", ""),
        "status": tur.get("status"),
        "total_tokens": tur.get("totalTokens"),
        "total_duration_ms": tur.get("totalDurationMs"),
        "tool_use_count": tur.get("totalToolUseCount"),
    }


def extract_hook_injection(record):
    """Measure the context a hook injects, from a `type: "attachment"` record.

    Claude Code logs one such record per hook invocation. For a `hook_success`
    attachment, `stdout` is the hook's raw stdout — when the hook follows the
    documented hook-output-JSON protocol, that's
    `{"hookSpecificOutput": {"hookEventName": ..., "additionalContext": "<text
    actually injected into the model's context>"}}`. This is how skills/plugins
    piggyback a SessionStart hook to load their instructions, and how a
    PreToolUse/PostToolUse hook can add context around a tool call.

    Returns None for hooks that didn't inject anything measurable (most
    PreToolUse/PostToolUse hooks are pass/fail gates with no additionalContext,
    e.g. a command-rewriting hook that only touches exit code/stderr) — there's
    nothing to attribute a size to. This measures injected *characters*, not a
    dollar cost: hook injections aren't billed separately, they just become
    part of the next turn's input tokens.
    """
    if record.get("type") != "attachment":
        return None
    att = record.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "hook_success":
        return None
    session_id = record.get("sessionId")
    if not session_id:
        return None

    injected_chars = 0
    try:
        parsed = json.loads(att.get("stdout") or "")
        ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext")
        if isinstance(ctx, str):
            injected_chars = len(ctx)
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    if injected_chars == 0:
        return None

    return {
        "uuid": record.get("uuid"),
        "session_id": session_id,
        "timestamp": record.get("timestamp", ""),
        "cwd": record.get("cwd", ""),
        "hook_event": att.get("hookEvent"),
        "hook_name": att.get("hookName"),
        "command": att.get("command"),
        "injected_chars": injected_chars,
    }


def insert_hook_events(conn, hook_events):
    if not hook_events:
        return
    conn.executemany("""
        INSERT OR IGNORE INTO hook_events
            (uuid, session_id, timestamp, cwd, hook_event, hook_name, command, injected_chars)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (h.get("uuid"), h["session_id"], h["timestamp"], h["cwd"],
         h.get("hook_event"), h.get("hook_name"), h.get("command"), h["injected_chars"])
        for h in hook_events
    ])


def upsert_agents(conn, agents):
    """Insert or update agent dispatch metadata. Last write wins per agent_id."""
    if not agents:
        return
    conn.executemany("""
        INSERT INTO agents
            (agent_id, agent_type, dispatched_in_session, completed_at,
             status, total_tokens, total_duration_ms, tool_use_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            agent_type            = excluded.agent_type,
            dispatched_in_session = excluded.dispatched_in_session,
            completed_at          = excluded.completed_at,
            status                = excluded.status,
            total_tokens          = excluded.total_tokens,
            total_duration_ms     = excluded.total_duration_ms,
            tool_use_count        = excluded.tool_use_count
    """, [
        (a["agent_id"], a["agent_type"], a.get("dispatched_in_session"),
         a.get("completed_at"), a.get("status"),
         a.get("total_tokens"), a.get("total_duration_ms"), a.get("tool_use_count"))
        for a in agents
    ])


def parse_jsonl_file(filepath, hook_events=None):
    """Parse a JSONL file and return (session_metas, turns, agents, line_count).

    Deduplicates streaming events by message.id — Claude Code logs multiple
    JSONL records per API response, all sharing the same message.id. Only the
    last record per message_id is kept (it has the final usage tallies).

    If `hook_events` is passed (a list), hook-injected-context records found
    in this file are appended to it (see extract_hook_injection) — an optional
    out-param rather than a 5th return value, so existing 4-tuple call sites
    (including every test that unpacks this function's return) don't break.
    """
    seen_messages = {}  # message_id -> turn dict (dedup streaming records)
    turns_no_id = []    # turns without a message_id (kept as-is)
    session_meta = {}   # session_id -> dict
    agents = {}         # agent_id -> dispatch dict
    line_count = 0

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line_count, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rtype = record.get("type")
                if rtype not in ("assistant", "user", "custom-title", "ai-title", "attachment"):
                    continue

                if rtype == "attachment":
                    if hook_events is not None:
                        injection = extract_hook_injection(record)
                        if injection is not None:
                            hook_events.append(injection)
                    continue

                session_id = record.get("sessionId")
                if not session_id:
                    continue

                # Extract session title from title records
                title = _extract_title(record)
                if title:
                    if session_id not in session_meta:
                        session_meta[session_id] = {
                            "session_id": session_id,
                            "project_name": "unknown",
                            "first_timestamp": "",
                            "last_timestamp": "",
                            "git_branch": "",
                            "model": None,
                            "topic": None,
                        }
                    meta = session_meta[session_id]
                    # custom-title always wins; ai-title only if no custom-title set
                    if rtype == "custom-title":
                        meta["topic"] = title
                    elif rtype == "ai-title" and not meta.get("topic"):
                        meta["topic"] = title
                    continue

                if rtype == "user":
                    dispatch = extract_agent_dispatch(record)
                    if dispatch is not None:
                        agents[dispatch["agent_id"]] = dispatch

                timestamp = record.get("timestamp", "")
                cwd = record.get("cwd", "")
                git_branch = record.get("gitBranch") or git_branch_from_cwd(cwd) or ""

                # Update session metadata from any record
                if session_id not in session_meta:
                    session_meta[session_id] = {
                        "session_id": session_id,
                        "project_name": project_name_from_cwd(cwd),
                        "first_timestamp": timestamp,
                        "last_timestamp": timestamp,
                        "git_branch": git_branch,
                        "model": None,
                        "topic": None,
                    }
                else:
                    meta = session_meta[session_id]
                    if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
                        meta["first_timestamp"] = timestamp
                    if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
                        meta["last_timestamp"] = timestamp
                    if git_branch and not meta["git_branch"]:
                        meta["git_branch"] = git_branch

                if rtype == "assistant":
                    msg = record.get("message", {})
                    usage = msg.get("usage", {})
                    model = msg.get("model", "")
                    message_id = msg.get("id", "")

                    input_tokens = usage.get("input_tokens", 0) or 0
                    output_tokens = usage.get("output_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

                    # Only record turns that have actual token usage
                    if input_tokens + output_tokens + cache_read + cache_creation == 0:
                        continue

                    # Extract tool name from content if present
                    tool_name = None
                    for item in msg.get("content", []):
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            tool_name = item.get("name")
                            break

                    if model:
                        session_meta[session_id]["model"] = model

                    turn = build_turn(
                        record, filepath, session_id, timestamp, cwd, model,
                        message_id, input_tokens, output_tokens, cache_read,
                        cache_creation, tool_name,
                    )

                    # Dedup: last record per message_id wins (final usage tallies)
                    if message_id:
                        seen_messages[message_id] = turn
                    else:
                        turns_no_id.append(turn)

    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")

    turns = turns_no_id + list(seen_messages.values())
    return list(session_meta.values()), turns, list(agents.values()), line_count


def aggregate_sessions(session_metas, turns):
    """Aggregate turn data back into session-level stats."""
    from collections import defaultdict, Counter

    session_stats = defaultdict(lambda: {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_creation": 0,
        "turn_count": 0,
        "model": None,
    })
    session_model_counts = defaultdict(Counter)

    for t in turns:
        s = session_stats[t["session_id"]]
        s["total_input_tokens"] += t["input_tokens"]
        s["total_output_tokens"] += t["output_tokens"]
        s["total_cache_read"] += t["cache_read_tokens"]
        s["total_cache_creation"] += t["cache_creation_tokens"]
        s["turn_count"] += 1
        if t["model"]:
            session_model_counts[t["session_id"]][t["model"]] += 1

    for sid, counts in session_model_counts.items():
        if counts:
            session_stats[sid]["model"] = counts.most_common(1)[0][0]

    # Merge into session_metas
    result = []
    for meta in session_metas:
        sid = meta["session_id"]
        stats = session_stats[sid]
        result.append({**meta, **stats})
    return result


def upsert_sessions(conn, sessions):
    for s in sessions:
        # Check if session exists
        existing = conn.execute(
            "SELECT total_input_tokens, total_output_tokens, total_cache_read, "
            "total_cache_creation, turn_count FROM sessions WHERE session_id = ?",
            (s["session_id"],)
        ).fetchone()

        # A session seen only via a title record (custom-title / ai-title carry a
        # sessionId but no timestamp) has no real content. Don't let it INSERT a
        # phantom, token-less row; if the session already exists it still falls
        # through to the UPDATE below and sets its topic.
        if existing is None and not s.get("first_timestamp"):
            continue

        if existing is None:
            conn.execute("""
                INSERT INTO sessions
                    (session_id, project_name, first_timestamp, last_timestamp,
                     git_branch, total_input_tokens, total_output_tokens,
                     total_cache_read, total_cache_creation, model, turn_count,
                     topic)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["session_id"], s["project_name"], s["first_timestamp"],
                s["last_timestamp"], s["git_branch"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["model"], s["turn_count"], s.get("topic")
            ))
        else:
            # Update: add new tokens on top of existing (since we only insert new turns)
            # Keep the highest-priority model (e.g. opus over haiku from subagents)
            existing_row = conn.execute(
                "SELECT model, topic FROM sessions WHERE session_id = ?",
                (s["session_id"],)
            ).fetchone()
            existing_model = existing_row["model"]
            new_model = s["model"]
            if _model_priority(new_model) > _model_priority(existing_model):
                model_to_set = new_model
            else:
                model_to_set = existing_model

            # Update topic if the new scan found one and the existing is empty
            new_topic = s.get("topic")
            existing_topic = existing_row["topic"]
            topic_to_set = new_topic if new_topic else existing_topic

            conn.execute("""
                UPDATE sessions SET
                    last_timestamp = MAX(last_timestamp, ?),
                    total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cache_read = total_cache_read + ?,
                    total_cache_creation = total_cache_creation + ?,
                    turn_count = turn_count + ?,
                    model = ?,
                    topic = ?
                WHERE session_id = ?
            """, (
                s["last_timestamp"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["turn_count"], model_to_set, topic_to_set,
                s["session_id"]
            ))


def insert_turns(conn, turns):
    conn.executemany("""
        INSERT OR IGNORE INTO turns
            (session_id, timestamp, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, tool_name, cwd, message_id,
             is_subagent, agent_id, attribution_agent, attribution_skill,
             attribution_plugin, mcp_server, mcp_tool, thread_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (t["session_id"], t["timestamp"], t["model"],
         t["input_tokens"], t["output_tokens"],
         t["cache_read_tokens"], t["cache_creation_tokens"],
         t["tool_name"], t["cwd"], t.get("message_id", ""),
         t.get("is_subagent", 0), t.get("agent_id"),
         t.get("attribution_agent"), t.get("attribution_skill"),
         t.get("attribution_plugin"), t.get("mcp_server"),
         t.get("mcp_tool"), t.get("thread_type"))
        for t in turns
    ])


def scan(projects_dir=None, projects_dirs=None, db_path=DB_PATH, verbose=True):
    conn = get_db(db_path)
    init_db(conn)

    if projects_dirs:
        dirs_to_scan = [Path(d) for d in projects_dirs]
    elif projects_dir:
        dirs_to_scan = [Path(projects_dir)]
    else:
        dirs_to_scan = DEFAULT_PROJECTS_DIRS

    jsonl_files = []
    for d in dirs_to_scan:
        if not d.exists():
            continue
        if verbose:
            print(f"Scanning {d} ...")
        jsonl_files.extend(glob.glob(str(d / "**" / "*.jsonl"), recursive=True))
    jsonl_files.sort()

    # One-time topic backfill for DBs whose sessions predate topic support: fill
    # topics from title records in already-processed transcripts that an
    # incremental scan would otherwise never revisit. Runs once, gated by the
    # schema_meta 'topic_backfill_done' marker. It runs before the main loop, so
    # on a fresh DB the sessions table is still empty and this no-ops; only DBs
    # with pre-existing untitled sessions do real work.
    if _meta_get(conn, "topic_backfill_done") != "1":
        filled = _backfill_topics(conn, jsonl_files)
        _meta_set(conn, "topic_backfill_done", "1")
        conn.commit()
        if verbose and filled:
            print(f"Backfilled topic for {filled} existing session(s).")

    # One-time project-name backfill for DBs scanned before git-aware, worktree-
    # collapsing project names existed: recomputes every session's project_name
    # from its first turn's cwd using the same resolver new scans use. Runs
    # once, gated by the schema_meta 'project_git_backfill_done' marker.
    if _meta_get(conn, "project_git_backfill_done") != "1":
        renamed = _backfill_project_names(conn)
        _meta_set(conn, "project_git_backfill_done", "1")
        conn.commit()
        if verbose and renamed:
            print(f"Recomputed project name for {renamed} existing session(s) (git worktree grouping).")

    # One-time hook-injection backfill for DBs scanned before hook_events
    # existed. Runs once, gated by the schema_meta 'hook_events_backfill_done'
    # marker.
    if _meta_get(conn, "hook_events_backfill_done") != "1":
        added = _backfill_hook_events(conn, jsonl_files)
        _meta_set(conn, "hook_events_backfill_done", "1")
        conn.commit()
        if verbose and added:
            print(f"Backfilled {added} hook injection event(s) from existing transcripts.")

    new_files = 0
    updated_files = 0
    skipped_files = 0
    total_turns = 0
    total_sessions = set()

    for filepath in jsonl_files:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue

        row = conn.execute(
            "SELECT mtime, lines FROM processed_files WHERE path = ?",
            (filepath,)
        ).fetchone()

        if row and abs(row["mtime"] - mtime) < 0.01:
            skipped_files += 1
            continue

        is_new = row is None
        if verbose:
            status = "NEW" if is_new else "UPD"
            print(f"  [{status}] {filepath}")

        if is_new:
            # New file: full parse (single read, returns line count)
            hook_events = []
            session_metas, turns, agents, line_count = parse_jsonl_file(filepath, hook_events=hook_events)
            upsert_agents(conn, agents)
            insert_hook_events(conn, hook_events)

            if turns or session_metas:
                sessions = aggregate_sessions(session_metas, turns)
                upsert_sessions(conn, sessions)
                insert_turns(conn, turns)
                for s in sessions:
                    total_sessions.add(s["session_id"])
                total_turns += len(turns)
                new_files += 1

        else:
            # Updated file: read once, process only new lines
            old_lines = row["lines"] if row else 0
            seen_messages = {}  # message_id -> turn (dedup streaming)
            turns_no_id = []
            new_session_metas = {}
            agents = {}         # agent_id -> dispatch dict
            hook_events = []
            line_count = 0

            try:
                with open(filepath, encoding="utf-8", errors="replace") as f:
                    for line_count, line in enumerate(f, 1):
                        if line_count <= old_lines:
                            continue
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        rtype = record.get("type")
                        if rtype not in ("assistant", "user", "custom-title", "ai-title", "attachment"):
                            continue

                        if rtype == "attachment":
                            injection = extract_hook_injection(record)
                            if injection is not None:
                                hook_events.append(injection)
                            continue

                        session_id = record.get("sessionId")
                        if not session_id:
                            continue

                        # Extract session title from title records
                        title = _extract_title(record)
                        if title:
                            if session_id not in new_session_metas:
                                new_session_metas[session_id] = {
                                    "session_id": session_id,
                                    "project_name": "unknown",
                                    "first_timestamp": "",
                                    "last_timestamp": "",
                                    "git_branch": "",
                                    "model": None,
                                    "topic": None,
                                }
                            meta = new_session_metas[session_id]
                            if rtype == "custom-title":
                                meta["topic"] = title
                            elif rtype == "ai-title" and not meta.get("topic"):
                                meta["topic"] = title
                            continue

                        if rtype == "user":
                            dispatch = extract_agent_dispatch(record)
                            if dispatch is not None:
                                agents[dispatch["agent_id"]] = dispatch

                        timestamp = record.get("timestamp", "")
                        cwd = record.get("cwd", "")

                        # Track session metadata from new lines
                        if session_id not in new_session_metas:
                            new_session_metas[session_id] = {
                                "session_id": session_id,
                                "project_name": project_name_from_cwd(cwd),
                                "first_timestamp": timestamp,
                                "last_timestamp": timestamp,
                                "git_branch": record.get("gitBranch") or git_branch_from_cwd(cwd) or "",
                                "model": None,
                                "topic": None,
                            }
                        else:
                            meta = new_session_metas[session_id]
                            if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
                                meta["last_timestamp"] = timestamp
                            if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
                                meta["first_timestamp"] = timestamp

                        if rtype == "assistant":
                            msg = record.get("message", {})
                            usage = msg.get("usage", {})
                            model = msg.get("model", "")
                            message_id = msg.get("id", "")

                            input_tokens = usage.get("input_tokens", 0) or 0
                            output_tokens = usage.get("output_tokens", 0) or 0
                            cache_read = usage.get("cache_read_input_tokens", 0) or 0
                            cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

                            if input_tokens + output_tokens + cache_read + cache_creation == 0:
                                continue

                            tool_name = None
                            for item in msg.get("content", []):
                                if isinstance(item, dict) and item.get("type") == "tool_use":
                                    tool_name = item.get("name")
                                    break

                            if model:
                                new_session_metas[session_id]["model"] = model

                            turn = build_turn(
                                record, filepath, session_id, timestamp, cwd, model,
                                message_id, input_tokens, output_tokens, cache_read,
                                cache_creation, tool_name,
                            )

                            if message_id:
                                seen_messages[message_id] = turn
                            else:
                                turns_no_id.append(turn)
            except Exception as e:
                print(f"  Warning: {e}")

            if line_count <= old_lines:
                # File didn't grow (mtime changed but no new content)
                conn.execute("UPDATE processed_files SET mtime = ? WHERE path = ?",
                             (mtime, filepath))
                conn.commit()
                skipped_files += 1
                continue

            new_turns = turns_no_id + list(seen_messages.values())
            upsert_agents(conn, list(agents.values()))
            insert_hook_events(conn, hook_events)

            if new_turns or new_session_metas:
                sessions = aggregate_sessions(list(new_session_metas.values()), new_turns)
                upsert_sessions(conn, sessions)
                insert_turns(conn, new_turns)
                for s in sessions:
                    total_sessions.add(s["session_id"])
                total_turns += len(new_turns)
            updated_files += 1

        # Record file as processed (line_count already known from the single read)
        conn.execute("""
            INSERT OR REPLACE INTO processed_files (path, mtime, lines)
            VALUES (?, ?, ?)
        """, (filepath, mtime, line_count))
        conn.commit()

    # Recompute session totals from actual turns in DB.
    # This ensures correctness when INSERT OR IGNORE skips duplicate turns
    # but upsert_sessions had already added their tokens additively.
    if new_files or updated_files:
        conn.execute("""
            UPDATE sessions SET
                total_input_tokens = COALESCE((SELECT SUM(input_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
                total_output_tokens = COALESCE((SELECT SUM(output_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
                total_cache_read = COALESCE((SELECT SUM(cache_read_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
                total_cache_creation = COALESCE((SELECT SUM(cache_creation_tokens) FROM turns WHERE turns.session_id = sessions.session_id), 0),
                turn_count = COALESCE((SELECT COUNT(*) FROM turns WHERE turns.session_id = sessions.session_id), 0)
        """)
        conn.commit()

    if verbose:
        print(f"\nScan complete:")
        print(f"  New files:     {new_files}")
        print(f"  Updated files: {updated_files}")
        print(f"  Skipped files: {skipped_files}")
        print(f"  Turns added:   {total_turns}")
        print(f"  Sessions seen: {len(total_sessions)}")

    conn.close()
    return {"new": new_files, "updated": updated_files, "skipped": skipped_files,
            "turns": total_turns, "sessions": len(total_sessions)}


if __name__ == "__main__":
    import sys
    projects_dir = None
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--projects-dir" and i + 1 < len(sys.argv[1:]):
            projects_dir = Path(sys.argv[i + 2])
            break
    scan(projects_dir=projects_dir)
