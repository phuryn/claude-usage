"""
analyzer.py - Usage snapshot, scrubber, waste estimator, and prompt builder
for the Usage Analyzer feature.
"""

import copy
import hashlib
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

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


def _hash(value):
    """SHA1 first 8 chars — used to pseudonymize paths/IDs."""
    if not value:
        return "unknown"
    return hashlib.sha1(str(value).encode()).hexdigest()[:8]


def build_snapshot(conn):
    """Query SQLite and return usage metrics dict (contains raw paths/IDs)."""
    today = date.today()
    thirty_ago  = (today - timedelta(days=30)).isoformat()
    seven_ago   = (today - timedelta(days=7)).isoformat()
    fourteen_ago = (today - timedelta(days=14)).isoformat()

    # ── Overall cache stats (all time) ────────────────────────────────────────
    row = conn.execute("""
        SELECT SUM(input_tokens) AS inp, SUM(output_tokens) AS out,
               SUM(cache_read_tokens) AS cr, SUM(cache_creation_tokens) AS cc
        FROM turns
    """).fetchone()
    total_inp = row["inp"] or 0
    total_cr  = row["cr"]  or 0
    eligible  = total_inp + total_cr
    cache_hit_rate = (total_cr / eligible) if eligible > 0 else 0.0

    # ── Model distribution (last 30d) ──────────────────────────────────────────
    model_rows = conn.execute("""
        SELECT COALESCE(model,'unknown') AS model,
               SUM(input_tokens) AS inp, SUM(output_tokens) AS out,
               SUM(cache_read_tokens) AS cr, SUM(cache_creation_tokens) AS cc,
               COUNT(*) AS turns
        FROM turns
        WHERE substr(timestamp,1,10) >= ?
        GROUP BY model
    """, (thirty_ago,)).fetchall()

    model_dist = []
    total_tokens_30d = 0
    for r in model_rows:
        cost = _calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        tok  = (r["inp"] or 0) + (r["out"] or 0) + (r["cr"] or 0) + (r["cc"] or 0)
        total_tokens_30d += tok
        model_dist.append({
            "model": r["model"], "turns": r["turns"],
            "input": r["inp"] or 0, "output": r["out"] or 0,
            "cache_read": r["cr"] or 0, "cache_creation": r["cc"] or 0,
            "cost": cost,
        })

    total_cost_30d = sum(m["cost"] for m in model_dist)
    for m in model_dist:
        tok = m["input"] + m["output"] + m["cache_read"] + m["cache_creation"]
        m["token_pct"] = (tok / total_tokens_30d * 100) if total_tokens_30d else 0
        m["cost_pct"]  = (m["cost"] / total_cost_30d * 100) if total_cost_30d else 0
    model_dist.sort(key=lambda x: x["cost"], reverse=True)

    # ── Per-project cache rate (top 10 by spend, last 30d) ────────────────────
    proj_rows = conn.execute("""
        SELECT cwd,
               SUM(input_tokens) AS inp, SUM(output_tokens) AS out,
               SUM(cache_read_tokens) AS cr, SUM(cache_creation_tokens) AS cc,
               MAX(model) AS model
        FROM turns
        WHERE substr(timestamp,1,10) >= ?
        GROUP BY cwd
    """, (thirty_ago,)).fetchall()

    projects = []
    for r in proj_rows:
        inp  = r["inp"] or 0
        cr   = r["cr"]  or 0
        cost = _calc_cost(r["model"], inp, r["out"] or 0, cr, r["cc"] or 0)
        e    = inp + cr
        projects.append({
            "cwd": r["cwd"], "input": inp, "cache_read": cr,
            "cache_rate": (cr / e) if e > 0 else 0.0, "cost": cost,
        })
    projects.sort(key=lambda x: x["cost"], reverse=True)
    top_projects = projects[:10]

    # ── Session patterns (last 30d) ────────────────────────────────────────────
    sess_rows = conn.execute("""
        SELECT t.session_id, s.project_name, s.git_branch,
               COUNT(t.id) AS turns,
               SUM(t.input_tokens) AS inp, SUM(t.output_tokens) AS out,
               SUM(t.cache_read_tokens) AS cr, SUM(t.cache_creation_tokens) AS cc,
               MAX(t.model) AS model
        FROM turns t
        JOIN sessions s ON t.session_id = s.session_id
        WHERE substr(t.timestamp,1,10) >= ?
        GROUP BY t.session_id
    """, (thirty_ago,)).fetchall()

    sessions = []
    for r in sess_rows:
        cost = _calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        sessions.append({
            "session_id": r["session_id"],
            "project_name": r["project_name"],
            "git_branch": r["git_branch"],
            "turns": r["turns"], "input": r["inp"] or 0,
            "cache_read": r["cr"] or 0, "cost": cost, "model": r["model"],
        })
    sessions.sort(key=lambda x: x["cost"], reverse=True)
    top_sessions = sessions[:10]

    costs_list = [s["cost"] for s in sessions]
    turns_list = [s["turns"] for s in sessions]
    avg_cost  = sum(costs_list) / len(costs_list) if costs_list else 0
    avg_turns = sum(turns_list) / len(turns_list) if turns_list else 0
    sorted_c  = sorted(costs_list)
    p95_cost  = sorted_c[int(len(sorted_c) * 0.95)] if sorted_c else 0

    # ── Daily cost trend (last 30d) ────────────────────────────────────────────
    day_rows = conn.execute("""
        SELECT substr(timestamp,1,10) AS day, model,
               SUM(input_tokens) AS inp, SUM(output_tokens) AS out,
               SUM(cache_read_tokens) AS cr, SUM(cache_creation_tokens) AS cc
        FROM turns
        WHERE substr(timestamp,1,10) >= ?
        GROUP BY day, model ORDER BY day
    """, (thirty_ago,)).fetchall()

    daily_cost = {}
    for r in day_rows:
        c = _calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        daily_cost[r["day"]] = daily_cost.get(r["day"], 0) + c

    last_7d_cost  = sum(v for k, v in daily_cost.items() if k >= seven_ago)
    prior_7d_cost = sum(v for k, v in daily_cost.items() if fourteen_ago <= k < seven_ago)

    # ── Tool frequency (top 20, last 30d) ──────────────────────────────────────
    tool_rows = conn.execute("""
        SELECT tool_name, COUNT(*) AS cnt
        FROM turns
        WHERE tool_name IS NOT NULL AND tool_name != ''
          AND substr(timestamp,1,10) >= ?
        GROUP BY tool_name ORDER BY cnt DESC LIMIT 20
    """, (thirty_ago,)).fetchall()

    top_tools = [{"tool": r["tool_name"], "count": r["cnt"]} for r in tool_rows]

    return {
        "generated_at":      datetime.now().isoformat(),
        "cache_hit_rate":    cache_hit_rate,
        "total_input_30d":   total_inp,
        "total_cost_30d":    total_cost_30d,
        "model_distribution": model_dist,
        "top_projects":      top_projects,
        "session_patterns":  {
            "count": len(sessions), "avg_cost": avg_cost,
            "avg_turns": avg_turns, "p95_cost": p95_cost,
        },
        "top_sessions":      top_sessions,
        "daily_cost":        daily_cost,
        "last_7d_cost":      last_7d_cost,
        "prior_7d_cost":     prior_7d_cost,
        "top_tools":         top_tools,
        "monthly_input_tokens": total_inp,
    }


def scrub(snapshot):
    """Hash/strip identifying info. Returns new dict safe to send outside machine."""
    s = copy.deepcopy(snapshot)
    for proj in s.get("top_projects", []):
        proj["cwd"] = _hash(proj.get("cwd", ""))
    for sess in s.get("top_sessions", []):
        sess["session_id"]   = _hash(sess.get("session_id", ""))
        sess["project_name"] = _hash(sess.get("project_name", ""))
        sess["git_branch"]   = ""
    return s


def estimate_waste(snapshot):
    """Estimate monthly $ waste vs 80% cache-hit-rate target."""
    cache_rate = snapshot.get("cache_hit_rate", 0)
    target = 0.80
    if cache_rate >= target:
        return {"cache_savings": 0.0, "cache_rate": cache_rate, "target": target}

    monthly_input = snapshot.get("monthly_input_tokens", 0)
    if not monthly_input:
        return {"cache_savings": 0.0, "cache_rate": cache_rate, "target": target}

    model_dist = snapshot.get("model_distribution", [])
    total_inp  = sum(m["input"] for m in model_dist)
    blended_input_price = 3.00  # default Sonnet
    if total_inp:
        blended_input_price = sum(
            (m["input"] / total_inp) * ((_get_pricing(m["model"]) or {}).get("input", 3.00))
            for m in model_dist
        )

    cache_read_price = blended_input_price * 0.10
    savings_per_tok  = (blended_input_price - cache_read_price) / 1_000_000
    cache_savings    = (target - cache_rate) * monthly_input * savings_per_tok

    return {
        "cache_savings": cache_savings,
        "cache_rate":    cache_rate,
        "target":        target,
        "blended_input_price_per_mtok": blended_input_price,
    }


def build_prompt(snapshot):
    """Build analysis prompt for `claude --print`."""
    waste    = estimate_waste(snapshot)
    scrubbed = scrub(snapshot)

    cache_pct   = scrubbed["cache_hit_rate"] * 100
    last_7      = scrubbed["last_7d_cost"]
    prior_7     = scrubbed["prior_7d_cost"]
    delta_pct   = ((last_7 - prior_7) / prior_7 * 100) if prior_7 else 0
    delta_sign  = "+" if delta_pct >= 0 else ""
    savings_str = f"${waste['cache_savings']:.2f}/mo" if waste["cache_savings"] > 0 else "already at/above target"

    model_table = "| Model | Turns | Input Tok | Cache Read | Cost (30d) | Cost % |\n"
    model_table += "|-------|-------|-----------|------------|------------|--------|\n"
    for m in scrubbed["model_distribution"]:
        model_table += (f"| {m['model']} | {m['turns']:,} | {m['input']:,} | "
                        f"{m['cache_read']:,} | ${m['cost']:.2f} | {m['cost_pct']:.1f}% |\n")

    proj_table = "| Project (hashed) | Cache Rate | Cost (30d) |\n"
    proj_table += "|-----------------|-----------|------------|\n"
    for p in scrubbed["top_projects"][:5]:
        proj_table += f"| {p['cwd']} | {p['cache_rate']*100:.1f}% | ${p['cost']:.2f} |\n"

    sp       = scrubbed["session_patterns"]
    tools_str = ", ".join(f"{t['tool']}({t['count']})" for t in scrubbed["top_tools"][:10])

    return f"""# Claude Code Usage Analysis

## Step 1 — Research (do this first)

Search the web for current best practices before generating suggestions. Run these searches:

1. Search: "Claude Code prompt caching optimization 2025"
2. Search: "Claude Code model selection cost reduction tips"
3. Fetch: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
4. Fetch: https://docs.anthropic.com/en/docs/claude-code/costs

Use findings to inform your Fix recommendations — cite specific techniques you find. If web tools are unavailable, use your training knowledge on these topics.

---

## Step 2 — Snapshot (last 30 days — project paths hashed for privacy)

**Cache Hit Rate:** {cache_pct:.1f}% (target: 80%)
**Est. Savings at 80% Cache Rate:** {savings_str}
**Total Cost (30d):** ${scrubbed['total_cost_30d']:.2f}
**Spend Trend:** Last 7d ${last_7:.2f} vs Prior 7d ${prior_7:.2f} ({delta_sign}{delta_pct:.1f}%)

**Model Distribution (30d):**
{model_table}
**Top Projects by Cost (hashed):**
{proj_table}
**Session Patterns:** {sp['count']} sessions · avg cost ${sp['avg_cost']:.3f} · avg turns {sp['avg_turns']:.1f} · p95 cost ${sp['p95_cost']:.3f}

**Top Tools:** {tools_str}

---

## Step 3 — Generate Suggestions

Rules:
1. Every suggestion MUST cite a specific metric from the snapshot
2. Rank by estimated $/month saved (highest first)
3. Provide EXACTLY 5 suggestions
4. Where web research found a relevant technique, reference it in the Fix
5. Use this format for each:

## [Title]
Impact: ~$X/mo
Metric: [exact number from snapshot]
Fix: [concrete, copy-paste-ready action]

No generic advice. Ground every suggestion in the numbers above.
"""
