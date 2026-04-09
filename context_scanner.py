"""
context_scanner.py - Measure the startup "context window budget" for Claude Code projects.

Claude Code loads several files into the conversation before the user types anything:
  * Global instructions:   ~/.claude/CLAUDE.md
  * Project instructions:  <project>/CLAUDE.md
  * Local instructions:    <project>/CLAUDE.local.md
  * Auto-memory index:     ~/.claude/projects/<encoded-cwd>/memory/MEMORY.md (if present)
  * @-imports:             any `@path/to/file.md` reference inside the above files

Everything else under ~/.claude/agents, ~/.claude/commands, .claude/agents,
.claude/commands, .claude/skills (and the user-level equivalents) is "on-demand" —
it's available to the agent but only loaded if invoked.

This module:
  1. Auto-discovers those files for a given project directory (no project-specific
     conventions baked in).
  2. Estimates token cost via the standard ~4 chars/token heuristic.
  3. Persists a daily snapshot to the same SQLite DB used by the rest of claude-usage,
     so the dashboard can render a 90-day trend.

It is intentionally stdlib-only and works for any Claude Code project.
"""

import os
import re
import sqlite3
from pathlib import Path
from datetime import date

# Standard 200k context window for current Claude models.
CONTEXT_WINDOW = 200_000

# Heuristic: 1 token ≈ 4 characters of English text.
CHARS_PER_TOKEN = 4

HOME = Path.home()
GLOBAL_CLAUDE_DIR = HOME / ".claude"

# Files Claude Code loads at conversation start, relative to <project>.
ALWAYS_LOADED_PROJECT_FILES = ("CLAUDE.md", "CLAUDE.local.md")

# Directories whose contents are exposed to the agent but only loaded on demand.
ON_DEMAND_DIRS_PROJECT = (".claude/agents", ".claude/commands", ".claude/skills")
ON_DEMAND_DIRS_GLOBAL  = ("agents", "commands", "skills")

# Matches `@some/path.md` references that Claude Code expands inline.
IMPORT_RE = re.compile(r'(?<![\w/])@([\w./\-]+\.(?:md|markdown|txt))')


def estimate_tokens(text_or_chars):
    if isinstance(text_or_chars, int):
        return max(0, text_or_chars // CHARS_PER_TOKEN)
    if not text_or_chars:
        return 0
    return len(text_or_chars) // CHARS_PER_TOKEN


def _read(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return ""


def _file_entry(path, kind, root):
    if not path.exists() or not path.is_file():
        return None
    text = _read(path)
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)
    return {
        "path": str(path),
        "rel": rel,
        "kind": kind,                       # "always" | "on-demand"
        "chars": len(text),
        "tokens": estimate_tokens(text),
    }


def _resolve_imports(text, base_dirs, seen):
    """Yield absolute Paths for @-imports in `text`, searched in `base_dirs`."""
    for match in IMPORT_RE.findall(text or ""):
        for base in base_dirs:
            candidate = (base / match).resolve()
            if candidate in seen:
                continue
            if candidate.exists() and candidate.is_file():
                seen.add(candidate)
                yield candidate
                break


EXCLUDE_DIRS = {"node_modules", ".git", "dist", "build", "__pycache__", ".venv", "venv"}


def _walk_on_demand(directory, root, kind="on-demand"):
    """Yield file entries for every text file under `directory`, skipping vendored junk."""
    if not directory.exists() or not directory.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            if not name.lower().endswith((".md", ".markdown", ".txt")):
                continue
            entry = _file_entry(Path(dirpath) / name, kind, root)
            if entry:
                yield entry


def scan_project(project_dir):
    """Return a structured snapshot of the context window budget for one project.

    project_dir: absolute path to the project root (the cwd Claude Code was launched in).
    """
    project_dir = Path(project_dir).expanduser().resolve()

    files = []
    seen = set()

    # ── Always-loaded: global ─────────────────────────────────────────────────
    global_md = GLOBAL_CLAUDE_DIR / "CLAUDE.md"
    if global_md.exists():
        seen.add(global_md.resolve())
        entry = _file_entry(global_md, "always", HOME)
        if entry:
            entry["scope"] = "global"
            files.append(entry)

    # ── Always-loaded: project ────────────────────────────────────────────────
    for name in ALWAYS_LOADED_PROJECT_FILES:
        p = project_dir / name
        if p.exists():
            seen.add(p.resolve())
            entry = _file_entry(p, "always", project_dir)
            if entry:
                entry["scope"] = "project"
                files.append(entry)

    # ── Always-loaded: auto-memory MEMORY.md (Claude Code SDK convention) ────
    # ~/.claude/projects/<encoded-cwd>/memory/MEMORY.md
    encoded = str(project_dir).replace("/", "-")
    memory_md = GLOBAL_CLAUDE_DIR / "projects" / encoded / "memory" / "MEMORY.md"
    if memory_md.exists():
        seen.add(memory_md.resolve())
        entry = _file_entry(memory_md, "always", HOME)
        if entry:
            entry["scope"] = "memory"
            files.append(entry)

    # ── Resolve @-imports recursively from already-loaded files ──────────────
    queue = list(files)
    while queue:
        f = queue.pop()
        text = _read(Path(f["path"]))
        base_dirs = [Path(f["path"]).parent, project_dir, GLOBAL_CLAUDE_DIR]
        for imp in _resolve_imports(text, base_dirs, seen):
            entry = _file_entry(imp, "always", project_dir)
            if entry:
                entry["scope"] = "import"
                files.append(entry)
                queue.append(entry)

    # ── On-demand: project agents/commands/skills ─────────────────────────────
    for sub in ON_DEMAND_DIRS_PROJECT:
        for entry in _walk_on_demand(project_dir / sub, project_dir):
            if Path(entry["path"]).resolve() in seen:
                continue
            seen.add(Path(entry["path"]).resolve())
            entry["scope"] = "project"
            files.append(entry)

    # ── On-demand: global agents/commands/skills ──────────────────────────────
    for sub in ON_DEMAND_DIRS_GLOBAL:
        for entry in _walk_on_demand(GLOBAL_CLAUDE_DIR / sub, HOME):
            if Path(entry["path"]).resolve() in seen:
                continue
            seen.add(Path(entry["path"]).resolve())
            entry["scope"] = "global"
            files.append(entry)

    always = [f for f in files if f["kind"] == "always"]
    on_demand = [f for f in files if f["kind"] == "on-demand"]

    always_tokens    = sum(f["tokens"] for f in always)
    on_demand_tokens = sum(f["tokens"] for f in on_demand)

    return {
        "project_dir":      str(project_dir),
        "context_window":   CONTEXT_WINDOW,
        "always_tokens":    always_tokens,
        "on_demand_tokens": on_demand_tokens,
        "total_tokens":     always_tokens + on_demand_tokens,
        "remaining_tokens": max(0, CONTEXT_WINDOW - always_tokens),
        "files":            files,
    }


# ── Persistence ──────────────────────────────────────────────────────────────

def init_context_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS context_snapshots (
            day              TEXT,
            project_dir      TEXT,
            always_tokens    INTEGER,
            on_demand_tokens INTEGER,
            file_count       INTEGER,
            PRIMARY KEY (day, project_dir)
        );
        CREATE INDEX IF NOT EXISTS idx_context_day ON context_snapshots(day);
    """)
    conn.commit()


def save_snapshot(conn, snapshot, day=None):
    init_context_tables(conn)
    day = day or date.today().isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO context_snapshots
            (day, project_dir, always_tokens, on_demand_tokens, file_count)
        VALUES (?, ?, ?, ?, ?)
    """, (
        day,
        snapshot["project_dir"],
        snapshot["always_tokens"],
        snapshot["on_demand_tokens"],
        len(snapshot["files"]),
    ))
    conn.commit()


def known_projects(conn):
    """Return distinct project cwds Claude Code has been launched in.

    Tries the `turns` table first (populated by scanner.py). Falls back to
    decoding directory names under ~/.claude/projects/, which Claude Code
    creates per-cwd by replacing '/' with '-'.
    """
    try:
        rows = conn.execute("""
            SELECT cwd, COUNT(*) as turns
            FROM turns
            WHERE cwd IS NOT NULL AND cwd != ''
            GROUP BY cwd
            ORDER BY turns DESC
        """).fetchall()
        if rows:
            return [r[0] for r in rows]
    except sqlite3.OperationalError:
        pass

    projects_dir = GLOBAL_CLAUDE_DIR / "projects"
    if not projects_dir.exists():
        return []
    found = []
    for d in projects_dir.iterdir():
        if not d.is_dir():
            continue
        candidate = "/" + d.name.lstrip("-").replace("-", "/")
        p = Path(candidate)
        # Skip root and trivial paths — naive '-' → '/' decoding is ambiguous
        # for directory names that contain hyphens. Require ≥3 path components.
        if p.exists() and len(p.parts) >= 4:
            found.append(candidate)
    return sorted(set(found))


def snapshot_all_known(conn, day=None):
    """Snapshot every project Claude Code has been run in. Returns count."""
    n = 0
    for cwd in known_projects(conn):
        if not Path(cwd).exists():
            continue
        snap = scan_project(cwd)
        save_snapshot(conn, snap, day=day)
        n += 1
    return n


def get_trend(conn, project_dir=None, days=90):
    """Return [(day, always_tokens, on_demand_tokens), ...] for the trend chart."""
    init_context_tables(conn)
    if project_dir:
        rows = conn.execute("""
            SELECT day,
                   SUM(always_tokens),
                   SUM(on_demand_tokens)
            FROM context_snapshots
            WHERE project_dir = ?
              AND day >= date('now', ?)
            GROUP BY day
            ORDER BY day
        """, (project_dir, f'-{days} days')).fetchall()
    else:
        rows = conn.execute("""
            SELECT day,
                   AVG(always_tokens),
                   AVG(on_demand_tokens)
            FROM context_snapshots
            WHERE day >= date('now', ?)
            GROUP BY day
            ORDER BY day
        """, (f'-{days} days',)).fetchall()
    return [
        {"day": r[0], "always": int(r[1] or 0), "on_demand": int(r[2] or 0)}
        for r in rows
    ]


if __name__ == "__main__":
    import json, sys
    target = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    snap = scan_project(target)
    print(json.dumps({k: v for k, v in snap.items() if k != "files"}, indent=2))
    print(f"\n{len(snap['files'])} files discovered:")
    for f in sorted(snap["files"], key=lambda x: -x["tokens"])[:20]:
        print(f"  {f['kind']:9}  {f['tokens']:>7,} tok  {f['rel']}")
