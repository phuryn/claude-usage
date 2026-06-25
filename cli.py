"""
cli.py - Command-line interface for the Claude Code usage dashboard.

Commands:
  scan      - Scan JSONL files and update the database
  today     - Print today's usage summary
  stats     - Print all-time usage statistics
  dashboard - Scan + open browser + start dashboard server
"""

import os
import sys
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta

from scanner import VERSION

DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))

# All prices are per million tokens (USD), as of June 2026.
# cache_read / cache_write only apply to providers that support prompt caching;
# leave both as 0.0 for providers that don't (e.g. OpenAI, Grok, Perplexity).
#
# HOW TO ADD A NEW ENTERPRISE LLM
# ─────────────────────────────────
# 1. Add a block to PROVIDER_META with the provider's name, tier, keywords,
#    and the canonical fallback model ID (used when an exact match isn't found).
#    tier must be "enterprise" or "individual".
# 2. Add per-model entries to PRICING keyed by the exact API model ID.
# 3. Add keyword → canonical-key rows to _PROVIDER_FALLBACKS (more-specific first).
# 4. Mirror all three changes in dashboard.py (PROVIDER_META, PRICING, PROVIDER_FALLBACKS).
# 5. Run: python -m unittest discover -s tests -v

# ── Provider metadata ──────────────────────────────────────────────────────────
# Single source of truth for provider identity, tier, and keyword matching.
# "tier" drives the Enterprise / Individual quick-filter in the dashboard and
# the provider breakdown in `cli.py stats`.
#
# To add a new provider: append a new entry here (see HOW TO above).
PROVIDER_META = {
    "Anthropic": {
        "tier":     "individual",   # Claude Code is a personal-developer tool
        "keywords": ["claude", "fable", "mythos", "opus", "sonnet", "haiku"],
        "default":  "claude-sonnet-4-6",
    },
    "OpenAI": {
        "tier":     "enterprise",
        "keywords": ["gpt-", "o3", "o4-mini", "o3-mini"],
        "default":  "gpt-4o",
    },
    "Google": {
        "tier":     "enterprise",
        "keywords": ["gemini", "nanobanana"],
        "default":  "gemini-2.5-flash",
    },
    "xAI": {
        "tier":     "enterprise",
        "keywords": ["grok"],
        "default":  "grok-3",
    },
    "Perplexity": {
        "tier":     "enterprise",
        "keywords": ["sonar"],
        "default":  "sonar-pro",
    },
    # ── Abacus.AI ────────────────────────────────────────────────────────────────
    # Abacus is an enterprise AI platform that routes queries to underlying LLMs
    # (Claude, GPT-4o, Gemini, Grok, Sonar, etc.) based on query type or user
    # selection.  Model IDs in JSONL logs are typically prefixed with "abacus/"
    # (e.g. "abacus/claude-sonnet-4-6", "abacus/gpt-4o").
    #
    # Cost = underlying_model_price × ABACUS_MARKUP
    #
    # Set ABACUS_MARKUP in your environment once your firm confirms the billing
    # structure with Abacus:
    #   export ABACUS_MARKUP=1.0    # passthrough — pay underlying model rates only
    #   export ABACUS_MARKUP=1.15   # 15 % platform markup on top of model rates
    #   export ABACUS_MARKUP=0.0    # Abacus billed separately (flat seat/credit);
    #                                # show $0 in this dashboard to avoid double-count
    "Abacus": {
        "tier":     "enterprise",
        "keywords": ["abacus"],     # matches "abacus/...", "abacus-...", etc.
        "default":  "gpt-4o",       # Abacus default when no underlying model resolved
    },
}

def get_provider(model):
    """Return the provider name for a model ID, or None if unknown.

    Abacus-prefixed IDs (e.g. "abacus/gpt-4o") are attributed to Abacus
    regardless of which underlying model they contain, so they appear as a
    distinct provider group in the dashboard rather than being merged into
    the underlying provider (Anthropic, OpenAI, etc.).
    """
    if not model:
        return None
    # Check for Abacus routing prefix before the general keyword scan, so
    # "abacus/claude-sonnet-4-6" is attributed to Abacus, not Anthropic.
    if _strip_abacus_prefix(model) != model:
        return "Abacus"
    m = model.lower()
    for name, meta in PROVIDER_META.items():
        for kw in meta["keywords"]:
            if kw in m:
                return name
    return None


# ── Abacus.AI markup multiplier ───────────────────────────────────────────────
# Abacus routes to underlying LLMs; this multiplier is applied on top of the
# underlying model's per-token price to account for any platform fee.
#
# How to set:
#   export ABACUS_MARKUP=1.0    # default — underlying model rates only
#   export ABACUS_MARKUP=1.15   # 15 % Abacus platform markup
#   export ABACUS_MARKUP=0.0    # Abacus billed separately (flat/credit); show $0 here
#
# Until your firm confirms the Abacus billing structure, leave at 1.0.
ABACUS_MARKUP: float = float(os.environ.get("ABACUS_MARKUP", "1.0"))


def _strip_abacus_prefix(model: str) -> str:
    """Strip 'abacus/' or 'abacus-' prefix from a model ID string.

    Abacus.AI JSONL logs may write model IDs in several forms:
      abacus/claude-sonnet-4-6   → claude-sonnet-4-6
      abacus-gpt-4o              → gpt-4o
      ABACUS/gemini-2.5-pro      → gemini-2.5-pro   (case-normalised)
      claude-sonnet-4-6          → claude-sonnet-4-6 (no-op if no prefix)

    Adjust the prefix list below if your firm's Abacus deployment uses a
    different naming convention (e.g. "abacusai/" or "ax/").
    """
    lower = model.lower()
    for prefix in ("abacus/", "abacus-"):
        if lower.startswith(prefix):
            return model[len(prefix):]   # preserve original casing of model part
    return model

PRICING = {
    # ── Anthropic ──────────────────────────────────────────────────────────────
    # Fable / Mythos — most capable class, priced at 2× Opus.
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    "claude-mythos-5":   {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    "claude-opus-4-8":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-opus-4-7":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-opus-4-6":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-opus-4-5":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-sonnet-4-7": {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "claude-sonnet-4-6": {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "claude-sonnet-4-5": {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "claude-haiku-4-7":  {"input":  1.00, "output":  5.00, "cache_read": 0.10, "cache_write":  1.25},
    "claude-haiku-4-6":  {"input":  1.00, "output":  5.00, "cache_read": 0.10, "cache_write":  1.25},
    "claude-haiku-4-5":  {"input":  1.00, "output":  5.00, "cache_read": 0.10, "cache_write":  1.25},

    # ── OpenAI ────────────────────────────────────────────────────────────────
    # Reasoning models (o-series)
    "o3":              {"input": 10.00, "output": 40.00, "cache_read": 2.50, "cache_write": 0.0},
    "o4-mini":         {"input":  1.10, "output":  4.40, "cache_read": 0.275,"cache_write": 0.0},
    "o3-mini":         {"input":  1.10, "output":  4.40, "cache_read": 0.55, "cache_write": 0.0},
    # GPT-4.1 family
    "gpt-4.1":         {"input":  2.00, "output":  8.00, "cache_read": 0.50, "cache_write": 0.0},
    "gpt-4.1-mini":    {"input":  0.40, "output":  1.60, "cache_read": 0.10, "cache_write": 0.0},
    "gpt-4.1-nano":    {"input":  0.10, "output":  0.40, "cache_read": 0.025,"cache_write": 0.0},
    # GPT-4o family
    "gpt-4o":          {"input":  2.50, "output": 10.00, "cache_read": 1.25, "cache_write": 0.0},
    "gpt-4o-mini":     {"input":  0.15, "output":  0.60, "cache_read": 0.075,"cache_write": 0.0},

    # ── Google Gemini ─────────────────────────────────────────────────────────
    # Gemini 2.5 — pricing shown for prompts ≤200k tokens; >200k is 2× input.
    "gemini-2.5-pro":         {"input":  1.25, "output": 10.00, "cache_read": 0.31, "cache_write": 0.0},
    "gemini-2.5-flash":       {"input":  0.30, "output":  2.50, "cache_read": 0.075,"cache_write": 0.0},
    "gemini-2.5-flash-lite":  {"input":  0.10, "output":  0.40, "cache_read": 0.025,"cache_write": 0.0},
    # Gemini 2.0
    "gemini-2.0-flash":       {"input":  0.10, "output":  0.40, "cache_read": 0.025,"cache_write": 0.0},
    "gemini-2.0-flash-lite":  {"input":  0.075,"output":  0.30, "cache_read": 0.0,  "cache_write": 0.0},
    # Gemini Nano — on-device / edge; effectively $0 (billed via device, not API).
    "gemini-nano":            {"input":  0.0,  "output":  0.0,  "cache_read": 0.0,  "cache_write": 0.0},
    # Nanobanana — Google's experimental ultra-small model (alias for Nano tier).
    "nanobanana":             {"input":  0.0,  "output":  0.0,  "cache_read": 0.0,  "cache_write": 0.0},

    # ── xAI Grok ──────────────────────────────────────────────────────────────
    "grok-3":          {"input":  3.00, "output": 15.00, "cache_read": 0.0, "cache_write": 0.0},
    "grok-3-fast":     {"input":  5.00, "output": 25.00, "cache_read": 0.0, "cache_write": 0.0},
    "grok-3-mini":     {"input":  0.30, "output":  0.50, "cache_read": 0.0, "cache_write": 0.0},
    "grok-3-mini-fast":{"input":  0.60, "output":  4.00, "cache_read": 0.0, "cache_write": 0.0},
    "grok-2":          {"input":  2.00, "output": 10.00, "cache_read": 0.0, "cache_write": 0.0},
    "grok-2-mini":     {"input":  0.20, "output":  0.40, "cache_read": 0.0, "cache_write": 0.0},

    # ── Perplexity Sonar ──────────────────────────────────────────────────────
    # Sonar models include live web search grounding; per-request search fees
    # (≈$5/1000 requests for sonar-pro) are NOT reflected here — token costs only.
    "sonar-pro":              {"input":  3.00, "output": 15.00, "cache_read": 0.0, "cache_write": 0.0},
    "sonar":                  {"input":  1.00, "output":  1.00, "cache_read": 0.0, "cache_write": 0.0},
    "sonar-reasoning-pro":    {"input":  2.00, "output":  8.00, "cache_read": 0.0, "cache_write": 0.0},
    "sonar-reasoning":        {"input":  1.00, "output":  5.00, "cache_read": 0.0, "cache_write": 0.0},
    "sonar-deep-research":    {"input":  2.00, "output":  8.00, "cache_read": 0.0, "cache_write": 0.0},
}

# ── Provider keyword registry ──────────────────────────────────────────────────
# Maps a substring that appears in model IDs to the canonical fallback key in
# PRICING.  Order matters only within a provider family (more specific first).
# When adding a new enterprise provider, append an entry here instead of
# touching get_pricing()'s if-chain.
_PROVIDER_FALLBACKS = [
    # Abacus.AI — prefix-stripped in get_pricing() before this list is reached,
    # so a bare "abacus" substring here only fires for truly unresolvable Abacus
    # model IDs where the underlying model can't be identified.  Falls back to gpt-4o
    # (Abacus's most common default) so cost is non-zero rather than silently $0.
    ("abacus", "gpt-4o"),
    # Anthropic
    ("fable",          "claude-fable-5"),
    ("mythos",         "claude-mythos-5"),
    ("opus",           "claude-opus-4-8"),
    ("sonnet",         "claude-sonnet-4-6"),
    ("haiku",          "claude-haiku-4-5"),
    # OpenAI — check reasoning models before generic gpt-4 catches them
    ("o3-mini",        "o3-mini"),
    ("o4-mini",        "o4-mini"),
    ("o3",             "o3"),
    ("gpt-4.1-mini",   "gpt-4.1-mini"),
    ("gpt-4.1-nano",   "gpt-4.1-nano"),
    ("gpt-4.1",        "gpt-4.1"),
    ("gpt-4o-mini",    "gpt-4o-mini"),
    ("gpt-4o",         "gpt-4o"),
    # Google Gemini
    ("gemini-2.5-pro",        "gemini-2.5-pro"),
    ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),
    ("gemini-2.5-flash",      "gemini-2.5-flash"),
    ("gemini-2.0-flash-lite", "gemini-2.0-flash-lite"),
    ("gemini-2.0-flash",      "gemini-2.0-flash"),
    ("gemini-nano",           "gemini-nano"),
    ("nanobanana",            "nanobanana"),
    ("gemini",                "gemini-2.5-flash"),   # unknown Gemini → flash tier
    # xAI Grok
    ("grok-3-mini-fast", "grok-3-mini-fast"),
    ("grok-3-mini",      "grok-3-mini"),
    ("grok-3-fast",      "grok-3-fast"),
    ("grok-3",           "grok-3"),
    ("grok-2-mini",      "grok-2-mini"),
    ("grok-2",           "grok-2"),
    ("grok",             "grok-3"),                 # unknown Grok → Grok 3
    # Perplexity Sonar
    ("sonar-reasoning-pro", "sonar-reasoning-pro"),
    ("sonar-reasoning",     "sonar-reasoning"),
    ("sonar-deep-research", "sonar-deep-research"),
    ("sonar-pro",           "sonar-pro"),
    ("sonar",               "sonar"),
]


def get_pricing(model):
    """Return the pricing dict for a model ID, or None if unknown.

    For Abacus-prefixed model IDs (e.g. "abacus/claude-sonnet-4-6"), the
    prefix is stripped and the underlying model's price is returned.
    The ABACUS_MARKUP multiplier is applied separately in calc_cost() so
    that get_pricing() always returns base rates.
    """
    if not model:
        return None
    # Strip Abacus routing prefix before resolving the underlying model price.
    resolved = _strip_abacus_prefix(model)
    if resolved in PRICING:
        return PRICING[resolved]
    for key in PRICING:
        if resolved.startswith(key):
            return PRICING[key]
    m = resolved.lower()
    for keyword, fallback_key in _PROVIDER_FALLBACKS:
        if keyword in m:
            return PRICING[fallback_key]
    return None


def calc_cost(model, inp, out, cache_read, cache_creation):
    """Return the USD cost for a single turn.

    For Abacus-routed models the ABACUS_MARKUP multiplier is applied on top
    of the underlying model's per-token price.  Set ABACUS_MARKUP=0.0 if
    Abacus is billed separately (flat seat or credits) to avoid double-counting.
    """
    p = get_pricing(model)
    if not p:
        return 0.0
    base = (
        inp            * p["input"]       / 1_000_000 +
        out            * p["output"]      / 1_000_000 +
        cache_read     * p["cache_read"]  / 1_000_000 +
        cache_creation * p["cache_write"] / 1_000_000
    )
    # Apply Abacus markup only to Abacus-prefixed model strings.
    if model and _strip_abacus_prefix(model) != model:
        return base * ABACUS_MARKUP
    return base

# ── Budget guardrails ─────────────────────────────────────────────────────────
# Set any of these env vars to enable spend alerts.  Unset (or 0) = no limit.
#
#   BUDGET_DAILY=50        alert when today's spend exceeds $50
#   BUDGET_MONTHLY=500     alert when this calendar month's spend exceeds $500
#   BUDGET_SESSION=10      alert when any single session costs more than $10
#
# The CLI prints a warning and exits with code 2 when a threshold is breached.
# The dashboard surfaces a red banner in the UI.  Both use the same thresholds.

def _parse_budget(env_var: str) -> float | None:
    """Return float threshold from env var, or None if unset/zero/invalid."""
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
        return val if val > 0 else None
    except ValueError:
        print(f"  WARNING: {env_var}={raw!r} is not a valid number — ignored.", file=sys.stderr)
        return None

BUDGET_DAILY:   float | None = _parse_budget("BUDGET_DAILY")
BUDGET_MONTHLY: float | None = _parse_budget("BUDGET_MONTHLY")
BUDGET_SESSION: float | None = _parse_budget("BUDGET_SESSION")


def check_budget_alerts(conn) -> list[dict]:
    """Query the DB and return a list of active budget breach dicts.

    Each dict has keys: level ("warning"/"critical"), window, spent, limit, pct.
    "critical" fires when spend >= 100 % of the limit.
    "warning"  fires when spend >= 80 % of the limit (early heads-up).
    Returns [] when no thresholds are configured or none are breached.
    """
    conn.row_factory = sqlite3.Row
    alerts = []

    def _alert(window: str, limit: float, spent: float) -> None:
        if limit is None or limit <= 0:
            return
        pct = spent / limit * 100
        if pct >= 80:
            alerts.append({
                "level":  "critical" if pct >= 100 else "warning",
                "window": window,
                "spent":  spent,
                "limit":  limit,
                "pct":    pct,
            })

    today = date.today()

    # ── Daily ──────────────────────────────────────────────────────────────────
    if BUDGET_DAILY:
        rows = conn.execute("""
            SELECT COALESCE(model,'unknown') as model,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
            FROM turns WHERE substr(timestamp,1,10) = ?
            GROUP BY model
        """, (today.isoformat(),)).fetchall()
        daily_cost = sum(calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0) for r in rows)
        _alert("daily", BUDGET_DAILY, daily_cost)

    # ── Monthly ────────────────────────────────────────────────────────────────
    if BUDGET_MONTHLY:
        month_start = today.replace(day=1).isoformat()
        rows = conn.execute("""
            SELECT COALESCE(model,'unknown') as model,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
            FROM turns WHERE substr(timestamp,1,10) >= ?
            GROUP BY model
        """, (month_start,)).fetchall()
        monthly_cost = sum(calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0) for r in rows)
        _alert("monthly", BUDGET_MONTHLY, monthly_cost)

    # ── Per-session ────────────────────────────────────────────────────────────
    if BUDGET_SESSION:
        sessions = conn.execute("""
            SELECT session_id, COALESCE(model,'unknown') as model,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
            FROM turns
            GROUP BY session_id, model
        """).fetchall()
        # Aggregate per session (may span multiple models)
        session_costs: dict[str, float] = {}
        for r in sessions:
            session_costs[r["session_id"]] = session_costs.get(r["session_id"], 0.0) + \
                calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        worst_session = max(session_costs.values(), default=0.0)
        _alert("session", BUDGET_SESSION, worst_session)

    return alerts


def _print_budget_alerts(alerts: list[dict]) -> bool:
    """Print budget alerts to stdout. Returns True if any critical threshold breached."""
    if not alerts:
        return False
    any_critical = False
    print()
    for a in alerts:
        icon = "🚨" if a["level"] == "critical" else "⚠️ "
        label = a["window"].capitalize()
        print(f"  {icon}  BUDGET {a['level'].upper()} [{label}]  "
              f"${a['spent']:.2f} of ${a['limit']:.2f} ({a['pct']:.0f}%)")
        if a["level"] == "critical":
            any_critical = True
    print()
    return any_critical


def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(c):
    return f"${c:.4f}"

def hr(char="-", width=60):
    print(char * width)

def require_db():
    if not DB_PATH.exists():
        print("Database not found. Run: python cli.py scan")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(projects_dir=None):
    from scanner import scan
    scan(projects_dir=Path(projects_dir) if projects_dir else None)


def cmd_today():
    conn = require_db()
    conn.row_factory = sqlite3.Row
    today = date.today().isoformat()

    rows = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (today,)).fetchall()

    sessions = conn.execute("""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
    """, (today,)).fetchone()

    subagent = conn.execute("""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
          AND COALESCE(is_subagent, 0) = 1
    """, (today,)).fetchone()

    print()
    hr()
    print(f"  Today's Usage  ({today})")
    hr()

    if not rows:
        print("  No usage recorded today.")
        print()
        return

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0

    for r in rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"  {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"  {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions today:   {sessions['cnt']}")
    print(f"  Subagent tokens:  {fmt(subagent['tokens'] or 0)}  ({fmt(subagent['turns'] or 0)} turns)")
    print(f"  Cache read:       {fmt(total_cr)}")
    print(f"  Cache creation:   {fmt(total_cc)}")
    hr()

    alerts = check_budget_alerts(conn)
    conn.close()
    if _print_budget_alerts(alerts):
        sys.exit(2)   # non-zero so monitoring scripts can act on it
    print()


def cmd_week():
    conn = require_db()
    conn.row_factory = sqlite3.Row

    today_d = date.today()
    start_d = today_d - timedelta(days=6)
    start = start_d.isoformat()
    end = today_d.isoformat()

    by_day_model = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
        GROUP BY day, model
    """, (start, end)).fetchall()

    by_model = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (start, end)).fetchall()

    sessions = conn.execute("""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
    """, (start, end)).fetchone()

    print()
    hr()
    print(f"  Weekly Usage  ({start} to {end})")
    hr()

    if not by_model:
        print("  No usage recorded in the last 7 days.")
        print()
        conn.close()
        return

    # Aggregate per-day across models (with per-turn cost attribution)
    per_day = {}
    for r in by_day_model:
        d = r["day"]
        bucket = per_day.setdefault(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        bucket["turns"] += r["turns"]
        bucket["inp"]   += r["inp"] or 0
        bucket["out"]   += r["out"] or 0
        bucket["cost"]  += calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)

    print("  By Day:")
    for i in range(7):
        d = (start_d + timedelta(days=i)).isoformat()
        b = per_day.get(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        print(f"    {d}  turns={b['turns']:<4}  in={fmt(b['inp']):<8}  out={fmt(b['out']):<8}  cost={fmt_cost(b['cost'])}")

    hr()
    print("  By Model:")

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost  += cost
        total_inp   += r["inp"] or 0
        total_out   += r["out"] or 0
        total_cr    += r["cr"]  or 0
        total_cc    += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"    {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"    {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions this week:  {sessions['cnt']}")
    print(f"  Cache read:          {fmt(total_cr)}")
    print(f"  Cache creation:      {fmt(total_cc)}")
    hr()

    alerts = check_budget_alerts(conn)
    conn.close()
    if _print_budget_alerts(alerts):
        sys.exit(2)
    print()


def cmd_stats():
    conn = require_db()
    conn.row_factory = sqlite3.Row

    # Session-level info (count, date range)
    session_info = conn.execute("""
        SELECT
            COUNT(*)                  as sessions,
            MIN(first_timestamp)      as first,
            MAX(last_timestamp)       as last
        FROM sessions
    """).fetchone()

    # All-time totals from turns (more accurate — per-turn model attribution)
    totals = conn.execute("""
        SELECT
            SUM(input_tokens)             as inp,
            SUM(output_tokens)            as out,
            SUM(cache_read_tokens)        as cr,
            SUM(cache_creation_tokens)    as cc,
            COUNT(*)                      as turns
        FROM turns
    """).fetchone()

    # By model from turns (each turn has the actual model used)
    by_model = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns,
            COUNT(DISTINCT session_id) as sessions
        FROM turns
        GROUP BY model
        ORDER BY inp + out DESC
    """).fetchall()

    # Top 5 projects from turns (join with sessions for project name)
    top_projects = conn.execute("""
        SELECT
            COALESCE(s.project_name, 'unknown') as project_name,
            SUM(t.input_tokens)  as inp,
            SUM(t.output_tokens) as out,
            COUNT(*)             as turns,
            COUNT(DISTINCT t.session_id) as sessions
        FROM turns t
        LEFT JOIN sessions s ON t.session_id = s.session_id
        GROUP BY s.project_name
        ORDER BY inp + out DESC
        LIMIT 5
    """).fetchall()

    # Subagent totals (subagent tokens are included in the all-time totals above)
    subagent = conn.execute("""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE COALESCE(is_subagent, 0) = 1
    """).fetchone()

    # Daily average (last 30 days)
    daily_avg = conn.execute("""
        SELECT
            AVG(daily_inp) as avg_inp,
            AVG(daily_out) as avg_out
        FROM (
            SELECT
                substr(timestamp, 1, 10) as day,
                SUM(input_tokens) as daily_inp,
                SUM(output_tokens) as daily_out
            FROM turns
            WHERE timestamp >= datetime('now', '-30 days')
            GROUP BY day
        )
    """).fetchone()

    # Build total cost and provider/tier aggregates from per-model rows
    provider_totals = {}  # provider_name -> {inp, out, cr, cc, turns, cost, tier}
    total_cost = 0.0
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        prov = get_provider(r["model"]) or "Unknown"
        tier = PROVIDER_META[prov]["tier"] if prov in PROVIDER_META else "unknown"
        if prov not in provider_totals:
            provider_totals[prov] = {"inp": 0, "out": 0, "cr": 0, "cc": 0, "turns": 0, "cost": 0.0, "tier": tier}
        pt = provider_totals[prov]
        pt["inp"]   += r["inp"] or 0
        pt["out"]   += r["out"] or 0
        pt["cr"]    += r["cr"] or 0
        pt["cc"]    += r["cc"] or 0
        pt["turns"] += r["turns"] or 0
        pt["cost"]  += cost

    print()
    hr("=")
    print("  Claude Code Usage - All-Time Statistics")
    hr("=")

    first_date = (session_info["first"] or "")[:10]
    last_date = (session_info["last"] or "")[:10]
    print(f"  Period:           {first_date} to {last_date}")
    print(f"  Total sessions:   {session_info['sessions'] or 0:,}")
    print(f"  Total turns:      {fmt(totals['turns'] or 0)}")
    print(f"  Subagent turns:   {fmt(subagent['turns'] or 0)}")
    print()
    print(f"  Input tokens:     {fmt(totals['inp'] or 0):<12}  (raw prompt tokens)")
    print(f"  Output tokens:    {fmt(totals['out'] or 0):<12}  (generated tokens)")
    print(f"  Cache read:       {fmt(totals['cr'] or 0):<12}  (90% cheaper than input)")
    print(f"  Cache creation:   {fmt(totals['cc'] or 0):<12}  (25% premium on input)")
    print(f"  Subagent tokens:  {fmt(subagent['tokens'] or 0):<12}  (included in totals)")
    print()
    print(f"  Est. total cost:  ${total_cost:.4f}")
    hr()

    # Provider / tier breakdown
    tier_totals = {"enterprise": {"cost": 0.0, "turns": 0}, "individual": {"cost": 0.0, "turns": 0}}
    if provider_totals:
        print("  By Provider:")
        for prov, pt in sorted(provider_totals.items(), key=lambda x: -x[1]["cost"]):
            tier = pt["tier"]
            tier_label = f"[{tier}]"
            print(f"    {prov:<14} {tier_label:<14}  turns={fmt(pt['turns']):<8}  "
                  f"in={fmt(pt['inp']):<8}  out={fmt(pt['out']):<8}  cost={fmt_cost(pt['cost'])}")
            if tier in tier_totals:
                tier_totals[tier]["cost"]  += pt["cost"]
                tier_totals[tier]["turns"] += pt["turns"]
        print()
        print("  By Tier:")
        for tier_name in ("enterprise", "individual"):
            tt = tier_totals[tier_name]
            print(f"    {tier_name.capitalize():<14}  turns={fmt(tt['turns']):<8}  cost={fmt_cost(tt['cost'])}")
    hr()

    print("  By Model:")
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        print(f"    {r['model']:<30}  sessions={r['sessions']:<4}  turns={fmt(r['turns'] or 0):<6}  "
              f"in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print("  Top Projects:")
    for r in top_projects:
        print(f"    {(r['project_name'] or 'unknown'):<40}  sessions={r['sessions']:<3}  "
              f"turns={fmt(r['turns'] or 0):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")

    if daily_avg["avg_inp"]:
        hr()
        print("  Daily Average (last 30 days):")
        print(f"    Input:   {fmt(int(daily_avg['avg_inp'] or 0))}")
        print(f"    Output:  {fmt(int(daily_avg['avg_out'] or 0))}")

    hr("=")

    alerts = check_budget_alerts(conn)
    conn.close()
    if _print_budget_alerts(alerts):
        sys.exit(2)
    print()


def cmd_dashboard(projects_dir=None, host=None, port=None, no_browser=False, surface=None):
    import threading
    import time

    from dashboard import serve

    host = host or os.environ.get("HOST", "localhost")
    port = int(port or os.environ.get("PORT", "8080"))

    # Bind and serve the port *first*, then scan in the background. A cold scan
    # over a large ~/.claude/projects backlog can take well over a minute, and
    # the VS Code extension kills the process if it doesn't answer /api/data
    # within ~10s (see vscode-extension/src/server-manager.ts). Serving up front
    # means the port is live immediately; the dashboard shows whatever's already
    # in the DB and auto-refreshes as the background scan commits new data.
    #
    # Capture cmd_scan into a local so the background thread closes over the
    # current binding — keeps the test suite's mock.patch(cli.cmd_scan) effective
    # and prevents the thread from ever touching the real DB after a patch lifts.
    scan = cmd_scan

    def background_scan():
        print("Scanning in the background...")
        scan(projects_dir=projects_dir)
        print("Background scan complete.")

    threading.Thread(target=background_scan, daemon=True).start()

    # Open a browser for users running this as a script (see README). The VS Code
    # extension passes --no-browser since it embeds the dashboard in a webview.
    if not no_browser:
        import webbrowser

        def open_browser():
            time.sleep(1.0)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=open_browser, daemon=True).start()

    serve(host=host, port=port, surface=surface)


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
Claude Code Usage Dashboard

Usage:
  python cli.py scan [--projects-dir PATH]   Scan JSONL files and update database
  python cli.py today                        Show today's usage summary
  python cli.py week                         Show last 7 days (per-day + by-model)
  python cli.py stats                        Show all-time statistics
  python cli.py dashboard [--projects-dir PATH] [--host HOST] [--port PORT] [--no-browser] [--surface SURFACE]
                                                 Scan + start dashboard (opens a browser unless --no-browser)
  python cli.py --version                    Print the version and exit
"""

COMMANDS = {
    "scan": cmd_scan,
    "today": cmd_today,
    "week": cmd_week,
    "stats": cmd_stats,
    "dashboard": cmd_dashboard,
}

def parse_named_arg(args, flag):
    """Extract a --flag VALUE pair from an argument list."""
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
    return None

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V", "version"):
        print(VERSION)
        sys.exit(0)

    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(USAGE)
        sys.exit(0)

    command = sys.argv[1]
    rest = sys.argv[2:]
    projects_dir = parse_named_arg(rest, "--projects-dir")

    if command == "dashboard":
        cmd_dashboard(
            projects_dir=projects_dir,
            host=parse_named_arg(rest, "--host"),
            port=parse_named_arg(rest, "--port"),
            no_browser="--no-browser" in rest,
            surface=parse_named_arg(rest, "--surface"),
        )
    elif command == "scan" and projects_dir:
        cmd_scan(projects_dir=projects_dir)
    else:
        COMMANDS[command]()
