"""
transcript_reader.py - Read full Claude Code conversation transcripts from JSONL.

Standalone, stdlib-only. Given a session_id, locates the JSONL file under
~/.claude/projects/ that contains it and returns a structured turn-by-turn
view suitable for rendering.
"""

import json
import sqlite3
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Hard cap on bytes returned per content block to keep payload sane
MAX_BLOCK_CHARS = 8000


def _flatten_text(content):
    """Coerce a Claude message content field (str OR list-of-blocks) to plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "text":
            parts.append(item.get("text", ""))
        elif t == "tool_use":
            name = item.get("name", "tool")
            inp = item.get("input", {})
            try:
                preview = json.dumps(inp, indent=2)[:MAX_BLOCK_CHARS]
            except Exception:
                preview = str(inp)[:MAX_BLOCK_CHARS]
            parts.append(f"[tool_use: {name}]\n{preview}")
        elif t == "tool_result":
            res = item.get("content", "")
            if isinstance(res, list):
                res = _flatten_text(res)
            parts.append(f"[tool_result]\n{str(res)[:MAX_BLOCK_CHARS]}")
        elif t == "thinking":
            parts.append(f"[thinking]\n{item.get('thinking', '')[:MAX_BLOCK_CHARS]}")
    return "\n\n".join(p for p in parts if p)


def find_jsonl_for_session(session_id, conn=None):
    """Locate the JSONL file containing the given session_id.

    Strategy:
      1. Glob ~/.claude/projects/**/<session_id>.jsonl (Claude Code names the
         file after the session_id by default).
      2. Fall back to scanning processed_files in the DB if available.
    """
    if not session_id:
        return None
    matches = list(PROJECTS_DIR.glob(f"**/{session_id}.jsonl"))
    if matches:
        return matches[0]

    if conn is not None:
        try:
            row = conn.execute(
                "SELECT path FROM processed_files WHERE path LIKE ? LIMIT 1",
                (f"%{session_id}%",),
            ).fetchone()
            if row:
                return Path(row[0])
        except sqlite3.OperationalError:
            pass
    return None


def read_transcript(session_id, conn=None, max_turns=500):
    """Return a structured transcript for one session.

    Output:
      { session_id, file, project, turn_count, turns: [
          { idx, role, timestamp, model, text, tool_name } ...
      ] }
    """
    path = find_jsonl_for_session(session_id, conn)
    if not path or not path.exists():
        return {"error": f"No JSONL found for session {session_id}"}

    turns = []
    project = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("sessionId") and rec.get("sessionId") != session_id:
                continue
            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                continue
            if not project:
                project = rec.get("cwd")
            msg = rec.get("message", {}) or {}
            text = _flatten_text(msg.get("content", ""))
            if not text:
                continue
            tool_name = None
            if isinstance(msg.get("content"), list):
                for item in msg["content"]:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        tool_name = item.get("name")
                        break
            turns.append({
                "idx": len(turns),
                "role": rtype,
                "timestamp": rec.get("timestamp", ""),
                "model": msg.get("model"),
                "text": text,
                "tool_name": tool_name,
            })
            if len(turns) >= max_turns:
                break

    return {
        "session_id": session_id,
        "file": str(path),
        "project": project,
        "turn_count": len(turns),
        "turns": turns,
    }


if __name__ == "__main__":
    import sys
    sid = sys.argv[1]
    out = read_transcript(sid)
    print(json.dumps({k: v for k, v in out.items() if k != "turns"}, indent=2))
    print(f"\n{out.get('turn_count', 0)} turns")
    for t in out.get("turns", [])[:5]:
        print(f"  [{t['idx']}] {t['role']:9} {t['timestamp'][:19]}  {t['text'][:80]}")
