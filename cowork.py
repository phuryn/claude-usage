"""
Cowork (Claude Desktop "agent" / Cowork mode) session log support.

Claude Desktop writes one JSONL audit log per Cowork session to its userData
directory. Each `result` event in the log carries an authoritative `modelUsage`
breakdown — the same numbers Anthropic uses for billing — so we synthesize one
turn per (result, model) pair and let the rest of the pipeline (aggregation,
pricing, dashboard) treat them like any other Claude Code turns.

Why we don't read the per-event `assistant` records: they mix per-event
input/output tokens with cumulative cache numbers, and some streaming chunks
are duplicated. Naive aggregation undercounts output tokens by ~20x. The
`result` events have already done the bookkeeping correctly.
"""

import json
import os
import sys
from pathlib import Path


def cowork_sessions_dir():
    """Directory where Claude Desktop writes per-session audit.jsonl files.

    Returns the platform-specific Electron userData path joined with
    "local-agent-mode-sessions". Returns None on platforms where we can't
    determine the path (e.g. Windows without %APPDATA%).
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Claude"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        base = Path(appdata) / "Claude"
    else:  # Linux/BSD — Electron uses XDG_CONFIG_HOME (or ~/.config)
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        base = Path(xdg) / "Claude"
    return base / "local-agent-mode-sessions"


def find_audit_files(base_dir=None):
    """Return all audit.jsonl files under base_dir (default: cowork_sessions_dir())."""
    base = Path(base_dir) if base_dir else cowork_sessions_dir()
    if not base or not base.exists():
        return []
    return sorted(base.rglob("audit.jsonl"))


def _normalise_model(name):
    """Cowork sometimes appends a tier hint like "[1m]" for 1-hour cache.
    Strip it so the dashboard's pricing lookup matches a known model name."""
    if not name:
        return name
    return name.split("[", 1)[0]


def parse_audit_file(filepath):
    """Parse one Cowork audit.jsonl into the same shape as parse_jsonl_file().

    Returns (session_metas, turns, line_count). The contract matches
    scanner.parse_jsonl_file() so the scan loop can dispatch by filename.
    """
    session_meta = {}
    turns = []
    line_count = 0
    msg_idx = 0

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

                if record.get("type") != "result":
                    continue

                session_id = record.get("session_id")
                if not session_id:
                    continue

                timestamp = record.get("_audit_timestamp", "")
                project_name = f"Cowork/{session_id[:8]}"

                if session_id not in session_meta:
                    session_meta[session_id] = {
                        "session_id": session_id,
                        "project_name": project_name,
                        "first_timestamp": timestamp,
                        "last_timestamp": timestamp,
                        "git_branch": "",
                        "model": None,
                    }
                else:
                    meta = session_meta[session_id]
                    if timestamp:
                        if not meta["first_timestamp"] or timestamp < meta["first_timestamp"]:
                            meta["first_timestamp"] = timestamp
                        if not meta["last_timestamp"] or timestamp > meta["last_timestamp"]:
                            meta["last_timestamp"] = timestamp

                model_usage = record.get("modelUsage") or {}
                for model_raw, usage in model_usage.items():
                    model = _normalise_model(model_raw)
                    msg_idx += 1
                    turns.append({
                        "session_id": session_id,
                        "timestamp": timestamp,
                        "model": model,
                        "input_tokens": int(usage.get("inputTokens", 0) or 0),
                        "output_tokens": int(usage.get("outputTokens", 0) or 0),
                        "cache_read_tokens": int(usage.get("cacheReadInputTokens", 0) or 0),
                        "cache_creation_tokens": int(usage.get("cacheCreationInputTokens", 0) or 0),
                        "tool_name": None,
                        "cwd": project_name,
                        "message_id": f"cowork-{session_id}-{msg_idx}-{model}",
                    })
    except FileNotFoundError:
        pass

    return list(session_meta.values()), turns, line_count


def is_audit_file(filepath):
    """True if filepath looks like a Cowork audit.jsonl."""
    return Path(filepath).name == "audit.jsonl"
