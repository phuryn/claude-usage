"""Tests for alert_config, notifier, and session_alert_hook."""

import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import os


class TestAlertConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_path = Path(self.tmp) / "usage_alerts.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self):
        from alert_config import load_config
        return load_config(path=self.cfg_path)

    def _save(self, cfg):
        from alert_config import save_config
        save_config(cfg, path=self.cfg_path)

    def test_defaults_when_no_file(self):
        cfg = self._load()
        self.assertTrue(cfg["os_notifications"])
        self.assertEqual(cfg["plan"], "max")
        self.assertEqual(cfg["daily"]["budget_usd"], 10.00)
        self.assertEqual(cfg["session"]["turns"], 50)
        self.assertEqual(cfg["notification_cooldown_minutes"], 10)

    def test_partial_override_merges_defaults(self):
        self.cfg_path.write_text(json.dumps({"plan": "pro", "session": {"turns": 30}}))
        cfg = self._load()
        self.assertEqual(cfg["plan"], "pro")
        self.assertEqual(cfg["session"]["turns"], 30)
        # defaults preserved for unspecified keys
        self.assertEqual(cfg["session"]["cost_usd"], 1.00)
        self.assertEqual(cfg["daily"]["budget_usd"], 10.00)

    def test_save_and_load_roundtrip(self):
        cfg = self._load()
        cfg["daily"]["budget_usd"] = 25.00
        cfg["session"]["context_fill_percent"] = 90
        self._save(cfg)
        loaded = self._load()
        self.assertEqual(loaded["daily"]["budget_usd"], 25.00)
        self.assertEqual(loaded["session"]["context_fill_percent"], 90)

    def test_corrupt_file_returns_defaults(self):
        self.cfg_path.write_text("not valid json {{")
        cfg = self._load()
        self.assertEqual(cfg["session"]["turns"], 50)

    def test_missing_nested_key_filled_from_defaults(self):
        self.cfg_path.write_text(json.dumps({"daily": {"budget_usd": 5.00}}))
        cfg = self._load()
        self.assertEqual(cfg["daily"]["budget_usd"], 5.00)
        self.assertEqual(cfg["daily"]["warn_at_percent"], 80)


class TestNotifier(unittest.TestCase):
    def setUp(self):
        import notifier
        notifier._last_fired.clear()

    def test_fires_when_no_history(self):
        from notifier import send_notification
        with patch("notifier._dispatch") as mock_dispatch:
            result = send_notification("Title", "Msg", cooldown_minutes=10, alert_key="test1")
        self.assertTrue(result)
        mock_dispatch.assert_called_once()

    def test_blocked_within_cooldown(self):
        from notifier import send_notification, _last_fired
        _last_fired["test2"] = time.time()
        with patch("notifier._dispatch") as mock_dispatch:
            result = send_notification("Title", "Msg", cooldown_minutes=10, alert_key="test2")
        self.assertFalse(result)
        mock_dispatch.assert_not_called()

    def test_fires_after_cooldown_expires(self):
        from notifier import send_notification, _last_fired
        _last_fired["test3"] = time.time() - 700  # 11.6 min ago
        with patch("notifier._dispatch") as mock_dispatch:
            result = send_notification("Title", "Msg", cooldown_minutes=10, alert_key="test3")
        self.assertTrue(result)
        mock_dispatch.assert_called_once()

    def test_different_keys_are_independent(self):
        from notifier import send_notification, _last_fired
        _last_fired["blocked"] = time.time()
        with patch("notifier._dispatch") as mock_dispatch:
            send_notification("T", "M", cooldown_minutes=10, alert_key="blocked")
            send_notification("T", "M", cooldown_minutes=10, alert_key="free")
        mock_dispatch.assert_called_once()


class TestCheckThresholds(unittest.TestCase):
    def _sess(self, turns=10, cost=0.5, duration_min=30, context_pct=50, context_tokens=100_000):
        return {
            "turns": turns,
            "cost": cost,
            "duration_min": duration_min,
            "context_pct": context_pct,
            "context_tokens": context_tokens,
        }

    def _cfg(self, **overrides):
        base = {
            "session": {
                "turns": 50,
                "cost_usd": 1.00,
                "duration_minutes": 60,
                "context_fill_percent": 80,
            }
        }
        base["session"].update(overrides)
        return base

    def test_no_alerts_below_thresholds(self):
        from session_alert_hook import check_thresholds
        alerts = check_thresholds(self._sess(), self._cfg())
        self.assertEqual(alerts, [])

    def test_turns_alert_at_threshold(self):
        from session_alert_hook import check_thresholds
        alerts = check_thresholds(self._sess(turns=50), self._cfg())
        self.assertTrue(any("turns" in a for a in alerts))

    def test_cost_alert(self):
        from session_alert_hook import check_thresholds
        alerts = check_thresholds(self._sess(cost=1.00), self._cfg())
        self.assertTrue(any("$" in a for a in alerts))

    def test_duration_alert(self):
        from session_alert_hook import check_thresholds
        alerts = check_thresholds(self._sess(duration_min=60), self._cfg())
        self.assertTrue(any("min" in a for a in alerts))

    def test_context_alert(self):
        from session_alert_hook import check_thresholds
        alerts = check_thresholds(self._sess(context_pct=80, context_tokens=160_000), self._cfg())
        self.assertTrue(any("context" in a for a in alerts))

    def test_multiple_thresholds_all_reported(self):
        from session_alert_hook import check_thresholds
        sess = self._sess(turns=100, cost=2.0, duration_min=120, context_pct=95, context_tokens=190_000)
        alerts = check_thresholds(sess, self._cfg())
        self.assertEqual(len(alerts), 4)

    def test_custom_thresholds(self):
        from session_alert_hook import check_thresholds
        alerts = check_thresholds(self._sess(turns=25), self._cfg(turns=20))
        self.assertTrue(any("turns" in a for a in alerts))
        alerts2 = check_thresholds(self._sess(turns=19), self._cfg(turns=20))
        self.assertFalse(any("turns" in a for a in alerts2))


if __name__ == "__main__":
    unittest.main()
