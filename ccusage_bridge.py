"""
ccusage_bridge.py - Optional integration with the `ccusage` CLI.

ccusage (https://github.com/ryoppippi/ccusage) is a Node-distributed Rust binary
that reads the same ~/.claude transcripts and emits deduplicated, billing-accurate
JSON. We wrap it as an OPTIONAL data source: when Node/npx is available we ingest
its 5-hour "blocks" (billing windows, burn rate, projections) and per-day totals
into separate SQLite tables. When it isn't, everything degrades gracefully and the
native scanner is unaffected.

Nothing here is required for the core tool to work.
"""

import json
import os
import shutil
import sqlite3
import statistics
import subprocess
from datetime import datetime, timezone

from scanner import DB_PATH, get_db, init_db

# Pin via env if you want reproducible output; defaults to latest. The JSON shape
# is stable across the 20.0.x line (field names verified against 20.0.9).
CCUSAGE_SPEC = os.environ.get("CCUSAGE_SPEC", "ccusage@latest")
_TIMEOUT_S = 90


def detect_runtime():
    """Locate an npx/bunx runner. On Windows shutil.which returns npx.cmd, which
    must be invoked with its full path (a bare 'npx' raises FileNotFoundError
    under shell=False), so we probe the .cmd name first."""
    for name in ("npx.cmd", "npx", "bunx"):
        path = shutil.which(name)
        if path:
            return {"available": True, "runner": path,
                    "kind": "bunx" if name == "bunx" else "npx"}
    return {
        "available": False, "runner": None, "kind": None,
        "reason": "Node/npx not found in PATH — install Node.js from "
                  "https://nodejs.org to enable ccusage billing-window data.",
    }


def _build_argv(rt, sub_args):
    # npx needs -y to auto-install; bunx takes the spec directly.
    if rt["kind"] == "bunx":
        return [rt["runner"], CCUSAGE_SPEC, *sub_args]
    return [rt["runner"], "-y", CCUSAGE_SPEC, *sub_args]


def run_ccusage(sub_args, rt=None):
    """Run a ccusage subcommand and return parsed JSON, or None on any failure.

    Always decodes as UTF-8 (Windows' default cp1252 mangles non-ASCII output),
    suppresses ccusage's progress chatter via LOG_LEVEL=0, and never raises.
    """
    rt = rt or detect_runtime()
    if not rt["available"]:
        return None
    env = {**os.environ, "LOG_LEVEL": "0"}
    try:
        proc = subprocess.run(
            _build_argv(rt, sub_args),
            capture_output=True, env=env, timeout=_TIMEOUT_S,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Pure transforms (unit-testable without Node) ────────────────────────────────

def blocks_to_rows(data, ingested_at):
    """Map ccusage `blocks --json` output to billing_windows rows. Skips gaps.

    ccusage fields are camelCase; burnRate/projection are nested objects present
    only on the active block (null otherwise)."""
    rows = []
    for b in (data or {}).get("blocks", []):
        if not isinstance(b, dict) or b.get("isGap"):
            continue
        if not b.get("id"):
            continue
        tc = b.get("tokenCounts") or {}
        br = b.get("burnRate") or {}
        pj = b.get("projection") or {}
        rows.append({
            "block_id": b.get("id"),
            "start_time": b.get("startTime"),
            "end_time": b.get("endTime"),
            "actual_end_time": b.get("actualEndTime"),
            "is_active": 1 if b.get("isActive") else 0,
            "input_tokens": tc.get("inputTokens", 0) or 0,
            "output_tokens": tc.get("outputTokens", 0) or 0,
            "cache_read_tokens": tc.get("cacheReadInputTokens", 0) or 0,
            "cache_creation_tokens": tc.get("cacheCreationInputTokens", 0) or 0,
            "total_tokens": b.get("totalTokens", 0) or 0,
            "cost_usd": b.get("costUSD", 0) or 0,
            "models": json.dumps(b.get("models") or []),
            "burn_rate_tpm": br.get("tokensPerMinute"),
            "burn_rate_cost_per_hour": br.get("costPerHour"),
            "projected_total_tokens": pj.get("totalTokens"),
            "projected_cost_usd": pj.get("totalCost"),
            "remaining_minutes": pj.get("remainingMinutes"),
            "ingested_at": ingested_at,
        })
    return rows


def daily_to_rows(data, source, ingested_at):
    """Map ccusage `daily --json` output to ccusage_daily_cache rows.

    `ccusage daily` uses period/totalCost/cacheReadTokens; source-prefixed
    variants (e.g. `ccusage codex daily`) use date/costUSD — accept both."""
    rows = []
    for r in (data or {}).get("daily", []):
        if not isinstance(r, dict):
            continue
        day = r.get("period") or r.get("date")
        if not day:
            continue
        rows.append({
            "day": day,
            "source": source,
            "input_tokens": r.get("inputTokens", 0) or 0,
            "output_tokens": r.get("outputTokens", 0) or 0,
            "cache_read_tokens": r.get("cacheReadTokens", 0) or 0,
            "cache_creation_tokens": r.get("cacheCreationTokens", 0) or 0,
            "total_tokens": r.get("totalTokens", 0) or 0,
            "cost_usd": r.get("totalCost", r.get("costUSD", 0)) or 0,
            "models": json.dumps(r.get("modelsUsed") or r.get("models") or []),
            "ingested_at": ingested_at,
        })
    return rows


# ── DB writes ──────────────────────────────────────────────────────────────────

_BW_COLS = ("block_id", "start_time", "end_time", "actual_end_time", "is_active",
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "total_tokens", "cost_usd", "models",
            "burn_rate_tpm", "burn_rate_cost_per_hour", "projected_total_tokens",
            "projected_cost_usd", "remaining_minutes", "ingested_at")


def upsert_billing_windows(conn, rows):
    if not rows:
        return
    placeholders = ", ".join("?" for _ in _BW_COLS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _BW_COLS if c != "block_id")
    conn.executemany(
        f"INSERT INTO billing_windows ({', '.join(_BW_COLS)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(block_id) DO UPDATE SET {updates}",
        [tuple(r[c] for c in _BW_COLS) for r in rows],
    )


_DC_COLS = ("day", "source", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "total_tokens", "cost_usd", "models",
            "ingested_at")


def upsert_ccusage_daily(conn, rows):
    if not rows:
        return
    placeholders = ", ".join("?" for _ in _DC_COLS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _DC_COLS if c not in ("day", "source"))
    conn.executemany(
        f"INSERT INTO ccusage_daily_cache ({', '.join(_DC_COLS)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(day, source) DO UPDATE SET {updates}",
        [tuple(r[c] for c in _DC_COLS) for r in rows],
    )


# ── Plan-limit baseline (Monitor-style P90 of your own history) ─────────────────

def compute_p90_limit(window_totals, floor=0):
    """The 90th-percentile of your completed 5h-window totals — a personal
    'typical heavy window' baseline. Anthropic's hard token limit isn't exposed
    anywhere, so (like Claude-Code-Usage-Monitor) we use your own history as the
    yardstick. Returns `floor` when there isn't enough history."""
    vals = [int(v) for v in window_totals if v and v > 0]
    if not vals:
        return floor
    if len(vals) == 1:
        return max(vals[0], floor)
    p90 = statistics.quantiles(vals, n=10)[8]  # 9 cut points; [8] = 90th pct
    return max(int(p90), floor)


def summarize_billing(conn):
    """Read billing_windows into a dashboard-ready summary. Returns
    {'available': False} when ccusage has never populated the table (no Node),
    so the UI can show an install prompt instead of an empty card."""
    try:
        rows = conn.execute(
            "SELECT * FROM billing_windows ORDER BY start_time"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"available": False}
    if not rows:
        return {"available": False}

    windows = [dict(r) for r in rows]
    completed_totals = [w["total_tokens"] for w in windows if not w["is_active"]]
    active = next((w for w in windows if w["is_active"]), None)
    return {
        "available": True,
        "window_count": len(windows),
        "plan_limit_estimate": compute_p90_limit(completed_totals),
        "active": active,
        "recent": windows[-30:],
    }


# ── Orchestration ───────────────────────────────────────────────────────────────

def ingest(db_path=DB_PATH, verbose=True, rt=None):
    """Fetch ccusage blocks + daily and upsert into the DB. Safe to call always:
    returns {'available': False} (no error) when Node/ccusage is missing."""
    rt = rt or detect_runtime()
    if not rt["available"]:
        if verbose:
            print(f"[ccusage] {rt.get('reason', 'unavailable')}")
        return {"available": False}

    ingested_at = _now_iso()
    blocks = run_ccusage(["blocks", "--json", "--offline"], rt)
    daily = run_ccusage(["daily", "--json", "--offline"], rt)
    if blocks is None and daily is None:
        if verbose:
            print("[ccusage] runner found but no data returned (first run downloads "
                  "the package; check network/version).")
        return {"available": True, "blocks": 0, "daily": 0}

    conn = get_db(db_path)
    init_db(conn)
    bw = blocks_to_rows(blocks, ingested_at)
    dl = daily_to_rows(daily, "ccusage-all", ingested_at)
    upsert_billing_windows(conn, bw)
    upsert_ccusage_daily(conn, dl)
    conn.commit()
    conn.close()
    if verbose:
        print(f"[ccusage] ingested {len(bw)} billing windows, {len(dl)} daily rows")
    return {"available": True, "blocks": len(bw), "daily": len(dl)}


if __name__ == "__main__":
    print(ingest())
