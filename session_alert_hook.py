#!/usr/bin/env python3
"""
session_alert_hook.py - PostToolUse hook for in-conversation session alerts.
Registered via: python cli.py install-hook
Prints warning if active session crosses configured thresholds.
"""

import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

# Allow importing alert_config from the same directory as this script
sys.path.insert(0, str(Path(__file__).parent))
from alert_config import load_config

DB_PATH = Path.home() / ".claude" / "usage.db"
COOLDOWN_PATH = Path.home() / ".claude" / "usage_hook_cooldown.json"
MODEL_CONTEXT_LIMIT = 200_000

PRICING = {
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00},
    "claude-haiku-4-6":  {"input": 1.00, "output":  5.00},
}


def _get_pricing(model):
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for k, v in PRICING.items():
        if model.startswith(k):
            return v
    m = model.lower()
    if "opus"   in m: return PRICING["claude-opus-4-6"]
    if "sonnet" in m: return PRICING["claude-sonnet-4-6"]
    if "haiku"  in m: return PRICING["claude-haiku-4-5"]
    return None


def _calc_cost(model, inp, out, cr, cc):
    p = _get_pricing(model)
    if not p:
        return 0.0
    return (
        inp * p["input"]  / 1_000_000 +
        out * p["output"] / 1_000_000 +
        cr  * p["input"]  * 0.10 / 1_000_000 +
        cc  * p["input"]  * 1.25 / 1_000_000
    )


def _check_cooldown(cooldown_minutes):
    if not COOLDOWN_PATH.exists():
        return True
    try:
        data = json.loads(COOLDOWN_PATH.read_text())
        last = datetime.fromisoformat(data.get("last_alert", "2000-01-01T00:00:00"))
        elapsed = (datetime.now() - last).total_seconds() / 60
        return elapsed >= cooldown_minutes
    except Exception:
        return True


def _update_cooldown():
    try:
        COOLDOWN_PATH.write_text(json.dumps({"last_alert": datetime.now().isoformat()}))
    except Exception:
        pass


def _get_active_session():
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        sess = conn.execute("""
            SELECT session_id, project_name, first_timestamp, last_timestamp, model,
                   total_input_tokens, total_output_tokens,
                   total_cache_read, total_cache_creation, turn_count
            FROM sessions ORDER BY last_timestamp DESC LIMIT 1
        """).fetchone()
        if not sess:
            conn.close()
            return None
        last_turn = conn.execute("""
            SELECT input_tokens FROM turns
            WHERE session_id = ? ORDER BY timestamp DESC LIMIT 1
        """, (sess["session_id"],)).fetchone()
        conn.close()
    except Exception:
        return None

    context_tokens = last_turn["input_tokens"] if last_turn else 0
    context_pct = round(context_tokens / MODEL_CONTEXT_LIMIT * 100, 1)

    try:
        t1 = datetime.fromisoformat(sess["first_timestamp"].replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(sess["last_timestamp"].replace("Z", "+00:00"))
        duration_min = round((t2 - t1).total_seconds() / 60, 1)
    except Exception:
        duration_min = 0.0

    return {
        "session_id":     sess["session_id"],
        "project":        sess["project_name"] or "unknown",
        "model":          sess["model"] or "unknown",
        "turns":          sess["turn_count"] or 0,
        "cost":           _calc_cost(
            sess["model"],
            sess["total_input_tokens"]  or 0,
            sess["total_output_tokens"] or 0,
            sess["total_cache_read"]    or 0,
            sess["total_cache_creation"] or 0,
        ),
        "duration_min":   duration_min,
        "context_tokens": context_tokens,
        "context_pct":    context_pct,
    }


def _get_today_cost():
    if not DB_PATH.exists():
        return 0.0
    try:
        today = date.today().isoformat()
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT COALESCE(model,'unknown') as model,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
            FROM turns WHERE substr(timestamp,1,10) = ?
            GROUP BY model
        """, (today,)).fetchall()
        conn.close()
        return sum(
            _calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
            for r in rows
        )
    except Exception:
        return 0.0


def check_thresholds(sess, cfg):
    """Return list of triggered alert strings. Pure function for testability."""
    scfg = cfg.get("session", {})
    alerts = []
    if sess["turns"] >= scfg.get("turns", 50):
        alerts.append(f"{sess['turns']} turns")
    if sess["cost"] >= scfg.get("cost_usd", 1.00):
        alerts.append(f"${sess['cost']:.2f}")
    if sess["duration_min"] >= scfg.get("duration_minutes", 60):
        alerts.append(f"{sess['duration_min']:.0f}min")
    if sess["context_pct"] >= scfg.get("context_fill_percent", 80):
        ctx_k = sess["context_tokens"] // 1000
        lim_k = MODEL_CONTEXT_LIMIT // 1000
        alerts.append(f"context {sess['context_pct']:.0f}% full ({ctx_k}K/{lim_k}K)")
    return alerts


def main():
    cfg = load_config()
    cooldown = cfg.get("notification_cooldown_minutes", 10)

    if not _check_cooldown(cooldown):
        return

    sess = _get_active_session()
    if not sess:
        return

    alerts = check_thresholds(sess, cfg)
    if not alerts:
        return

    today_cost = _get_today_cost()
    budget = cfg.get("daily", {}).get("budget_usd", 10.00)

    now = datetime.now()
    elapsed_frac = (now.hour * 3600 + now.minute * 60 + now.second) / 86400
    projected_eod = today_cost / elapsed_frac if elapsed_frac > 0.01 else today_cost

    ctx_k = sess["context_tokens"] // 1000
    lim_k = MODEL_CONTEXT_LIMIT // 1000
    budget_pct = today_cost / budget * 100 if budget > 0 else 0

    print(
        f"⚠️  Session alert: {sess['turns']} turns · "
        f"${sess['cost']:.2f} · {sess['duration_min']:.0f}min · "
        f"context {sess['context_pct']:.0f}% full ({ctx_k}K/{lim_k}K)"
    )
    print(
        f"\U0001f4ca  Today: ${today_cost:.2f} / ${budget:.2f} budget "
        f"({budget_pct:.0f}%) — pacing ~${projected_eod:.2f} by EOD"
    )

    _update_cooldown()


if __name__ == "__main__":
    main()
