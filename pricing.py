"""
Anthropic API pricing — single source of truth for both the Python CLI cost
calculator and the JavaScript dashboard.

USD per million tokens. Updated April 2026.
Source: https://docs.claude.com/en/docs/about-claude/pricing
"""

PRICING = {
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-4-7": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-7":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-6":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
}


def get_pricing(model):
    """Look up per-MTok pricing for a model name.

    Tries exact match, then prefix match, then keyword fallback (any name
    containing 'opus'/'sonnet'/'haiku' falls back to the latest of that family).
    Returns None for unknown / non-Anthropic models so callers can show 'n/a'.
    """
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if model.startswith(key):
            return PRICING[key]
    m = model.lower()
    if "opus" in m:
        return PRICING["claude-opus-4-7"]
    if "sonnet" in m:
        return PRICING["claude-sonnet-4-6"]
    if "haiku" in m:
        return PRICING["claude-haiku-4-5"]
    return None


def calc_cost(model, inp, out, cache_read, cache_creation):
    """Cost in USD for one batch of token usage. Returns 0 for unknown models."""
    p = get_pricing(model)
    if p is None:
        return 0.0
    return (
        (inp or 0)            * p["input"]       / 1_000_000
        + (out or 0)          * p["output"]      / 1_000_000
        + (cache_read or 0)   * p["cache_read"]  / 1_000_000
        + (cache_creation or 0) * p["cache_write"] / 1_000_000
    )
