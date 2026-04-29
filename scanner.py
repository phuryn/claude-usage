"""
scanner.py - Scans Claude Code JSONL transcript files and stores data in SQLite.
"""

import json
import os
import glob
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

PROJECTS_DIR = Path.home() / ".claude" / "projects"
XCODE_PROJECTS_DIR = Path.home() / "Library" / "Developer" / "Xcode" / "CodingAssistant" / "ClaudeAgentConfig" / "projects"
DB_PATH = Path.home() / ".claude" / "usage.db"
DEFAULT_PROJECTS_DIRS = [PROJECTS_DIR, XCODE_PROJECTS_DIR]

# Higher number = higher priority when choosing a session's primary model
MODEL_PRIORITY = {"opus": 3, "sonnet": 2, "haiku": 1}


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
            session_name    TEXT
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
            message_id              TEXT
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            lines   INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
    """)
    # Add message_id column if upgrading from older schema
    try:
        conn.execute("SELECT message_id FROM turns LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE turns ADD COLUMN message_id TEXT")
    # Add session_name column if upgrading from older schema
    try:
        conn.execute("SELECT session_name FROM sessions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE sessions ADD COLUMN session_name TEXT")
    # Conditional unique index: only dedup non-null message IDs
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_message_id
        ON turns(message_id) WHERE message_id IS NOT NULL AND message_id != ''
    """)
    conn.commit()


def extract_session_name(record, current_custom, current_agent):
    """Extract customTitle / agentName from a record, preferring non-empty updates.

    Claude Code logs emit `{"type":"custom-title","customTitle":"..."}` and
    `{"type":"agent-name","agentName":"..."}` records when a session is renamed
    via `/rename`. The last non-empty value per session wins, with customTitle
    taking precedence over agentName at display time.
    """
    ct = record.get("customTitle")
    an = record.get("agentName")
    if ct:
        current_custom = ct
    if an:
        current_agent = an
    return current_custom, current_agent


def resolve_session_name(custom_title, agent_name):
    """Return the preferred display name: customTitle first, agentName second."""
    return custom_title or agent_name or None


def project_name_from_cwd(cwd):
    """Derive a friendly project name from cwd path."""
    if not cwd:
        return "unknown"
    # Normalize to forward slashes, take last 2 components
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


def parse_jsonl_file(filepath):
    """Parse a JSONL file and return (session_metas, turns, line_count).

    Deduplicates streaming events by message.id — Claude Code logs multiple
    JSONL records per API response, all sharing the same message.id. Only the
    last record per message_id is kept (it has the final usage tallies).
    """
    seen_messages = {}  # message_id -> turn dict (dedup streaming records)
    turns_no_id = []    # turns without a message_id (kept as-is)
    session_meta = {}   # session_id -> dict
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

                session_id = record.get("sessionId")
                if not session_id:
                    continue

                # Initialise meta on first sight of a session, regardless of record type
                if session_id not in session_meta:
                    session_meta[session_id] = {
                        "session_id": session_id,
                        "project_name": project_name_from_cwd(record.get("cwd", "")),
                        "first_timestamp": record.get("timestamp", ""),
                        "last_timestamp": record.get("timestamp", ""),
                        "git_branch": record.get("gitBranch", ""),
                        "model": None,
                        "custom_title": None,
                        "agent_name": None,
                    }

                # Capture session_name from any record (custom-title / agent-name types
                # emit it as a top-level field, but the issue notes it can appear opportunistically)
                meta = session_meta[session_id]
                meta["custom_title"], meta["agent_name"] = extract_session_name(
                    record, meta["custom_title"], meta["agent_name"]
                )

                rtype = record.get("type")
                if rtype not in ("assistant", "user"):
                    continue

                timestamp = record.get("timestamp", "")
                cwd = record.get("cwd", "")
                git_branch = record.get("gitBranch", "")

                # Update session metadata from assistant/user records
                if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
                    meta["first_timestamp"] = timestamp
                if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
                    meta["last_timestamp"] = timestamp
                if git_branch and not meta["git_branch"]:
                    meta["git_branch"] = git_branch
                # Backfill project_name if the record that created meta lacked cwd
                # (e.g. a custom-title record preceded the first user/assistant line)
                if cwd and (not meta["project_name"] or meta["project_name"] == "unknown"):
                    meta["project_name"] = project_name_from_cwd(cwd)

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

                    turn = {
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
                    }

                    # Dedup: last record per message_id wins (final usage tallies)
                    if message_id:
                        seen_messages[message_id] = turn
                    else:
                        turns_no_id.append(turn)

    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")

    turns = turns_no_id + list(seen_messages.values())
    return list(session_meta.values()), turns, line_count


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
        merged = {**meta, **stats}
        merged["session_name"] = resolve_session_name(
            meta.get("custom_title"), meta.get("agent_name")
        )
        result.append(merged)
    return result


def upsert_sessions(conn, sessions):
    for s in sessions:
        # Check if session exists
        existing = conn.execute(
            "SELECT total_input_tokens, total_output_tokens, total_cache_read, "
            "total_cache_creation, turn_count FROM sessions WHERE session_id = ?",
            (s["session_id"],)
        ).fetchone()

        if existing is None:
            conn.execute("""
                INSERT INTO sessions
                    (session_id, project_name, first_timestamp, last_timestamp,
                     git_branch, total_input_tokens, total_output_tokens,
                     total_cache_read, total_cache_creation, model, turn_count,
                     session_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["session_id"], s["project_name"], s["first_timestamp"],
                s["last_timestamp"], s["git_branch"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["model"], s["turn_count"], s.get("session_name")
            ))
        else:
            # Update: add new tokens on top of existing (since we only insert new turns)
            # Keep the highest-priority model (e.g. opus over haiku from subagents)
            existing_model = conn.execute(
                "SELECT model FROM sessions WHERE session_id = ?",
                (s["session_id"],)
            ).fetchone()["model"]
            new_model = s["model"]
            if _model_priority(new_model) > _model_priority(existing_model):
                model_to_set = new_model
            else:
                model_to_set = existing_model

            # session_name: replace only when we have a new non-empty value, to avoid
            # clobbering an existing name with a later scan that sees no rename records
            conn.execute("""
                UPDATE sessions SET
                    last_timestamp = MAX(last_timestamp, ?),
                    total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cache_read = total_cache_read + ?,
                    total_cache_creation = total_cache_creation + ?,
                    turn_count = turn_count + ?,
                    model = ?,
                    session_name = COALESCE(NULLIF(?, ''), session_name)
                WHERE session_id = ?
            """, (
                s["last_timestamp"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["turn_count"], model_to_set,
                s.get("session_name"),
                s["session_id"]
            ))


def insert_turns(conn, turns):
    conn.executemany("""
        INSERT OR IGNORE INTO turns
            (session_id, timestamp, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, tool_name, cwd, message_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (t["session_id"], t["timestamp"], t["model"],
         t["input_tokens"], t["output_tokens"],
         t["cache_read_tokens"], t["cache_creation_tokens"],
         t["tool_name"], t["cwd"], t.get("message_id", ""))
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
            session_metas, turns, line_count = parse_jsonl_file(filepath)

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

                        session_id = record.get("sessionId")
                        if not session_id:
                            continue

                        # Initialise meta on first sight, regardless of record type
                        if session_id not in new_session_metas:
                            new_session_metas[session_id] = {
                                "session_id": session_id,
                                "project_name": project_name_from_cwd(record.get("cwd", "")),
                                "first_timestamp": record.get("timestamp", ""),
                                "last_timestamp": record.get("timestamp", ""),
                                "git_branch": record.get("gitBranch", ""),
                                "model": None,
                                "custom_title": None,
                                "agent_name": None,
                            }

                        # Capture session_name updates from any record
                        meta = new_session_metas[session_id]
                        meta["custom_title"], meta["agent_name"] = extract_session_name(
                            record, meta["custom_title"], meta["agent_name"]
                        )

                        rtype = record.get("type")
                        if rtype not in ("assistant", "user"):
                            continue

                        timestamp = record.get("timestamp", "")
                        cwd = record.get("cwd", "")

                        if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
                            meta["last_timestamp"] = timestamp
                        if cwd and (not meta["project_name"] or meta["project_name"] == "unknown"):
                            meta["project_name"] = project_name_from_cwd(cwd)

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

                            turn = {
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
                            }

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
