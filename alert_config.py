"""
alert_config.py - Load/save/validate alert config with defaults.
Config lives at ~/.claude/usage_alerts.json
"""

import json
from pathlib import Path

CONFIG_PATH = Path.home() / ".claude" / "usage_alerts.json"

DEFAULTS = {
    "os_notifications": True,
    "plan": "max",
    "daily": {
        "budget_usd": 10.00,
        "warn_at_percent": 80
    },
    "session": {
        "cost_usd": 1.00,
        "turns": 50,
        "duration_minutes": 60,
        "context_fill_percent": 80
    },
    "notification_cooldown_minutes": 10
}


def load_config(path=None):
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        return _deep_copy(DEFAULTS)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return _merge(DEFAULTS, data)
    except Exception:
        return _deep_copy(DEFAULTS)


def save_config(cfg, path=None):
    p = Path(path) if path else CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _merge(defaults, override):
    result = {}
    for k, v in defaults.items():
        if k in override:
            if isinstance(v, dict) and isinstance(override[k], dict):
                result[k] = _merge(v, override[k])
            else:
                result[k] = override[k]
        else:
            result[k] = _deep_copy(v)
    return result


def _deep_copy(obj):
    return json.loads(json.dumps(obj))
