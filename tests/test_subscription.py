"""Tests for subscription.py — config loading, week boundaries, pace ratio."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class TestLoadSubscriptionConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data):
        p = self.tmp / "subscription.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_valid_config_loads(self):
        from subscription import load_subscription_config
        p = self._write({
            "plan": "max-20x",
            "monthly_price": 200,
            "weekly_budget_api_equivalent": 200,
            "reset": {"timezone": "America/Chicago", "day": "Tuesday", "time": "17:00"}
        })
        cfg = load_subscription_config(p)
        self.assertEqual(cfg["plan"], "max-20x")
        self.assertEqual(cfg["weekly_budget_api_equivalent"], 200)
        self.assertEqual(cfg["reset"]["day"], "Tuesday")

    def test_missing_file_returns_none(self):
        from subscription import load_subscription_config
        cfg = load_subscription_config(self.tmp / "nonexistent.json")
        self.assertIsNone(cfg)

    def test_malformed_json_returns_none(self):
        from subscription import load_subscription_config
        p = self.tmp / "subscription.json"
        p.write_text("{bad json", encoding="utf-8")
        self.assertIsNone(load_subscription_config(p))

    def test_missing_required_fields_returns_none(self):
        from subscription import load_subscription_config
        p = self._write({"plan": "max-20x"})  # missing reset, weekly_budget
        self.assertIsNone(load_subscription_config(p))

    def test_missing_reset_subfields_returns_none(self):
        from subscription import load_subscription_config
        p = self._write({
            "plan": "max-20x",
            "weekly_budget_api_equivalent": 200,
            "reset": {"timezone": "America/Chicago"}  # missing day, time
        })
        self.assertIsNone(load_subscription_config(p))


class TestGetWeekWindow(unittest.TestCase):
    def _reset_cfg(self):
        return {"timezone": "America/Chicago", "day": "Tuesday", "time": "17:00"}

    def test_midweek_returns_previous_tuesday(self):
        from subscription import get_week_window
        # Thursday 2026-04-09 12:00 Chicago → week started Tue 2026-04-07 17:00
        chicago = ZoneInfo("America/Chicago")
        now = datetime(2026, 4, 9, 12, 0, tzinfo=chicago)
        start, end = get_week_window(self._reset_cfg(), now)
        self.assertEqual(start, datetime(2026, 4, 7, 17, 0, tzinfo=chicago))
        self.assertEqual(end, datetime(2026, 4, 14, 17, 0, tzinfo=chicago))

    def test_exactly_at_reset_starts_new_week(self):
        from subscription import get_week_window
        chicago = ZoneInfo("America/Chicago")
        now = datetime(2026, 4, 7, 17, 0, tzinfo=chicago)  # Tuesday 17:00 exactly
        start, end = get_week_window(self._reset_cfg(), now)
        self.assertEqual(start, datetime(2026, 4, 7, 17, 0, tzinfo=chicago))
        self.assertEqual(end, datetime(2026, 4, 14, 17, 0, tzinfo=chicago))

    def test_just_before_reset_is_previous_week(self):
        from subscription import get_week_window
        chicago = ZoneInfo("America/Chicago")
        now = datetime(2026, 4, 7, 16, 59, tzinfo=chicago)  # Tuesday 16:59
        start, end = get_week_window(self._reset_cfg(), now)
        self.assertEqual(start, datetime(2026, 3, 31, 17, 0, tzinfo=chicago))
        self.assertEqual(end, datetime(2026, 4, 7, 17, 0, tzinfo=chicago))

    def test_monday_is_still_previous_week(self):
        from subscription import get_week_window
        chicago = ZoneInfo("America/Chicago")
        now = datetime(2026, 4, 6, 10, 0, tzinfo=chicago)  # Monday 10:00
        start, end = get_week_window(self._reset_cfg(), now)
        self.assertEqual(start, datetime(2026, 3, 31, 17, 0, tzinfo=chicago))
        self.assertEqual(end, datetime(2026, 4, 7, 17, 0, tzinfo=chicago))

    def test_dst_spring_forward_week(self):
        """Week spanning DST spring-forward (March 8 2026). Window should still be 7 calendar days."""
        from subscription import get_week_window
        chicago = ZoneInfo("America/Chicago")
        # Tuesday March 3 17:00 CST → Tuesday March 10 17:00 CDT
        now = datetime(2026, 3, 5, 12, 0, tzinfo=chicago)  # Thursday in that week
        start, end = get_week_window(self._reset_cfg(), now)
        self.assertEqual(start, datetime(2026, 3, 3, 17, 0, tzinfo=chicago))
        self.assertEqual(end, datetime(2026, 3, 10, 17, 0, tzinfo=chicago))
        # 7 calendar days, but actual hours differ due to DST
        self.assertEqual((end.date() - start.date()).days, 7)


class TestCalcPaceRatio(unittest.TestCase):
    def test_on_track(self):
        from subscription import calc_pace_ratio
        # 50% through week, used 50% of budget → ratio 1.0
        ratio = calc_pace_ratio(cost_used=100, weekly_budget=200, elapsed_fraction=0.5)
        self.assertAlmostEqual(ratio, 1.0)

    def test_ahead_of_pace(self):
        from subscription import calc_pace_ratio
        # 25% through week, used 50% of budget → ratio 2.0
        ratio = calc_pace_ratio(cost_used=100, weekly_budget=200, elapsed_fraction=0.25)
        self.assertAlmostEqual(ratio, 2.0)

    def test_under_pace(self):
        from subscription import calc_pace_ratio
        # 75% through week, used 25% of budget → ratio 0.33
        ratio = calc_pace_ratio(cost_used=50, weekly_budget=200, elapsed_fraction=0.75)
        self.assertAlmostEqual(ratio, 50 / 150, places=2)

    def test_first_hour_clamps_to_1(self):
        from subscription import calc_pace_ratio
        # Less than 1 hour into week (elapsed < 0.006) → clamp to 1.0
        ratio = calc_pace_ratio(cost_used=50, weekly_budget=200, elapsed_fraction=0.003)
        self.assertAlmostEqual(ratio, 1.0)

    def test_zero_cost_returns_zero(self):
        from subscription import calc_pace_ratio
        ratio = calc_pace_ratio(cost_used=0, weekly_budget=200, elapsed_fraction=0.5)
        self.assertAlmostEqual(ratio, 0.0)

    def test_zero_budget_returns_zero(self):
        from subscription import calc_pace_ratio
        ratio = calc_pace_ratio(cost_used=50, weekly_budget=0, elapsed_fraction=0.5)
        self.assertAlmostEqual(ratio, 0.0)


class TestPaceColor(unittest.TestCase):
    def test_green(self):
        from subscription import pace_color
        self.assertEqual(pace_color(0.5), "green")
        self.assertEqual(pace_color(1.0), "green")
        self.assertEqual(pace_color(1.19), "green")

    def test_yellow(self):
        from subscription import pace_color
        self.assertEqual(pace_color(1.2), "yellow")
        self.assertEqual(pace_color(1.49), "yellow")

    def test_red(self):
        from subscription import pace_color
        self.assertEqual(pace_color(1.5), "red")
        self.assertEqual(pace_color(3.0), "red")
