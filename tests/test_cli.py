"""Tests for cli.py - pricing, formatting, and cost calculation."""

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock
import cli
from cli import get_pricing, calc_cost, fmt, fmt_cost, PRICING, check_budget_alerts


class TestGetPricing(unittest.TestCase):
    def test_exact_model_match(self):
        p = get_pricing("claude-opus-4-6")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_all_known_models_have_pricing(self):
        for model in ("claude-fable-5", "claude-mythos-5",
                       "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
                       "claude-sonnet-4-7", "claude-sonnet-4-6", "claude-sonnet-4-5",
                       "claude-haiku-4-7", "claude-haiku-4-6", "claude-haiku-4-5"):
            p = get_pricing(model)
            self.assertGreater(p["input"], 0, f"Missing input price for {model}")
            self.assertGreater(p["output"], 0, f"Missing output price for {model}")

    def test_fable_and_mythos_have_explicit_entries(self):
        """Regression guard for #136/#137 — Fable 5 and Mythos 5 must be priced
        explicitly at 2x Opus, not fall through to $0/n/a or an Opus rate."""
        for model in ("claude-fable-5", "claude-mythos-5"):
            self.assertIn(model, PRICING)
            p = get_pricing(model)
            self.assertEqual(p["input"], 10.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 50.00, f"{model} output price wrong")
            self.assertEqual(p["cache_read"], 1.00, f"{model} cache_read wrong")
            self.assertEqual(p["cache_write"], 12.50, f"{model} cache_write wrong")

    def test_fable_date_suffix_matches(self):
        """JSONL model strings may carry a date suffix."""
        p = get_pricing("claude-fable-5-20260601")
        self.assertEqual(p["input"], 10.00)
        self.assertEqual(p["output"], 50.00)

    def test_substring_match_fable_and_mythos(self):
        """Unknown future fable/mythos variants resolve to Fable pricing,
        not the generic opus/sonnet/haiku rates or n/a."""
        for model in ("some-fable-variant", "internal-mythos-test"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 10.00, f"{model} should map to Fable pricing")
            self.assertEqual(p["output"], 50.00, f"{model} should map to Fable pricing")

    def test_opus_4_8_has_explicit_entry(self):
        """Regression guard for issue #133 — Opus 4.8 must be present, not just
        resolved via the generic 'opus' substring fallback."""
        self.assertIn("claude-opus-4-8", PRICING)
        p = get_pricing("claude-opus-4-8")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_opus_4_7_has_explicit_entry(self):
        """Regression guard for issue #61 — Opus 4.7 must be present."""
        p = get_pricing("claude-opus-4-7")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_opus_4_7_with_date_suffix(self):
        """Model strings from JSONL often have date suffixes."""
        p = get_pricing("claude-opus-4-7-20260215")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_prefix_match(self):
        # A model name with a suffix should still match the base
        p = get_pricing("claude-sonnet-4-6-20260401")
        self.assertEqual(p["input"], 3.00)
        self.assertEqual(p["output"], 15.00)

    def test_substring_match_opus(self):
        p = get_pricing("new-opus-5-model")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_substring_match_sonnet(self):
        p = get_pricing("custom-sonnet-variant")
        self.assertEqual(p["input"], 3.00)
        self.assertEqual(p["output"], 15.00)

    def test_substring_match_haiku(self):
        p = get_pricing("experimental-haiku-fast")
        self.assertEqual(p["input"], 1.00)
        self.assertEqual(p["output"], 5.00)

    def test_substring_match_case_insensitive(self):
        p = get_pricing("Claude-Opus-Next")
        self.assertEqual(p["input"], 5.00)

    def test_prefix_takes_precedence_over_substring(self):
        # Exact prefix match should win over substring fallback
        p = get_pricing("claude-opus-4-6-preview")
        self.assertEqual(p["input"], 5.00)
        self.assertEqual(p["output"], 25.00)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(get_pricing("glm-5.1"))
        self.assertIsNone(get_pricing("llama-4-scout"))
        self.assertIsNone(get_pricing("some-unknown-model"))

    def test_none_model_returns_none(self):
        self.assertIsNone(get_pricing(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(get_pricing(""))


class TestCalcCost(unittest.TestCase):
    def test_basic_cost_calculation(self):
        # 1M input tokens of Sonnet at $3/MTok = $3.00
        cost = calc_cost("claude-sonnet-4-6", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost, 3.00)

    def test_output_tokens(self):
        # 1M output tokens of Sonnet at $15/MTok = $15.00
        cost = calc_cost("claude-sonnet-4-6", 0, 1_000_000, 0, 0)
        self.assertAlmostEqual(cost, 15.00)

    def test_cache_read_discount(self):
        # Cache read = 10% of input price
        # 1M cache_read of Opus at $5 * 0.10 = $0.50
        cost = calc_cost("claude-opus-4-6", 0, 0, 1_000_000, 0)
        self.assertAlmostEqual(cost, 0.50)

    def test_cache_creation_premium(self):
        # Cache creation = 125% of input price
        # 1M cache_creation of Opus at $5 * 1.25 = $6.25
        cost = calc_cost("claude-opus-4-6", 0, 0, 0, 1_000_000)
        self.assertAlmostEqual(cost, 6.25)

    def test_combined_cost(self):
        cost = calc_cost("claude-haiku-4-5",
                         inp=500_000, out=100_000,
                         cache_read=200_000, cache_creation=50_000)
        expected = (
            500_000 * 1.00 / 1_000_000 +   # input
            100_000 * 5.00 / 1_000_000 +    # output
            200_000 * 1.00 * 0.10 / 1_000_000 +  # cache read
            50_000 * 1.00 * 1.25 / 1_000_000     # cache creation
        )
        self.assertAlmostEqual(cost, expected)

    def test_zero_tokens(self):
        cost = calc_cost("claude-opus-4-6", 0, 0, 0, 0)
        self.assertEqual(cost, 0.0)

    def test_unknown_model_costs_zero(self):
        cost = calc_cost("glm-5.1", 1_000_000, 500_000, 100_000, 50_000)
        self.assertEqual(cost, 0.0)

    def test_non_anthropic_model_costs_zero(self):
        # Truly unknown providers (e.g. local/open-source models) should cost $0.
        cost = calc_cost("llama-4-scout", 1_000_000, 500_000, 0, 0)
        self.assertEqual(cost, 0.0)

    def test_openai_model_costs_nonzero(self):
        cost = calc_cost("gpt-4o", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost, 2.50)

    def test_grok_model_pricing(self):
        cost = calc_cost("grok-3", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost, 3.00)

    def test_gemini_model_pricing(self):
        cost = calc_cost("gemini-2.5-pro", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost, 1.25)

    def test_sonar_model_pricing(self):
        cost = calc_cost("sonar-pro", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost, 3.00)

    def test_abacus_slash_prefix_resolves_to_underlying_model(self):
        # "abacus/claude-sonnet-4-6" should price identically to "claude-sonnet-4-6"
        # at the default markup of 1.0.
        cost_abacus = calc_cost("abacus/claude-sonnet-4-6", 1_000_000, 0, 0, 0)
        cost_direct = calc_cost("claude-sonnet-4-6", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost_abacus, cost_direct)

    def test_abacus_dash_prefix_resolves_to_underlying_model(self):
        # "abacus-gpt-4o" should resolve to gpt-4o pricing.
        cost = calc_cost("abacus-gpt-4o", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost, 2.50)  # same as gpt-4o input rate

    def test_abacus_markup_applied(self):
        import cli as cli_mod
        original = cli_mod.ABACUS_MARKUP
        try:
            cli_mod.ABACUS_MARKUP = 1.5  # 50 % markup
            cost = calc_cost("abacus/gpt-4o", 1_000_000, 0, 0, 0)
            # 1M input tokens of gpt-4o = $2.50 base × 1.5 markup = $3.75
            self.assertAlmostEqual(cost, 3.75)
        finally:
            cli_mod.ABACUS_MARKUP = original  # restore so other tests are unaffected

    def test_abacus_markup_zero_costs_nothing(self):
        # ABACUS_MARKUP=0.0 means Abacus is billed separately (flat/credits);
        # show $0 here to avoid double-counting.
        import cli as cli_mod
        original = cli_mod.ABACUS_MARKUP
        try:
            cli_mod.ABACUS_MARKUP = 0.0
            cost = calc_cost("abacus/claude-opus-4-6", 1_000_000, 0, 0, 0)
            self.assertAlmostEqual(cost, 0.0)
        finally:
            cli_mod.ABACUS_MARKUP = original

    def test_direct_model_unaffected_by_abacus_markup(self):
        # Markup should NOT apply to non-Abacus model IDs.
        import cli as cli_mod
        original = cli_mod.ABACUS_MARKUP
        try:
            cli_mod.ABACUS_MARKUP = 2.0
            cost = calc_cost("gpt-4o", 1_000_000, 0, 0, 0)
            self.assertAlmostEqual(cost, 2.50)  # unchanged
        finally:
            cli_mod.ABACUS_MARKUP = original

    def test_abacus_provider_identified(self):
        from cli import get_provider
        self.assertEqual(get_provider("abacus/claude-sonnet-4-6"), "Abacus")
        self.assertEqual(get_provider("abacus-gpt-4o"), "Abacus")


class TestBudgetGuardrails(unittest.TestCase):
    """Budget threshold detection and alert generation."""

    def _make_db(self, daily_cost_tokens=0, session_cost_tokens=0):
        """Create a temp DB with controllable spend.

        daily_cost_tokens input tokens of claude-sonnet-4-6 today →
        cost = tokens * $3/MTok.
        """
        tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        conn = sqlite3.connect(tf.name)
        conn.row_factory = sqlite3.Row
        import scanner
        scanner.init_db(conn)
        today = date.today().isoformat() + "T12:00:00Z"
        if daily_cost_tokens:
            conn.execute("""
                INSERT INTO turns
                  (session_id, timestamp, model, input_tokens, output_tokens,
                   cache_read_tokens, cache_creation_tokens)
                VALUES ('sess-daily', ?, 'claude-sonnet-4-6', ?, 0, 0, 0)
            """, (today, daily_cost_tokens))
        if session_cost_tokens:
            conn.execute("""
                INSERT INTO turns
                  (session_id, timestamp, model, input_tokens, output_tokens,
                   cache_read_tokens, cache_creation_tokens)
                VALUES ('sess-big', ?, 'claude-sonnet-4-6', ?, 0, 0, 0)
            """, (today, session_cost_tokens))
        conn.commit()
        return conn, Path(tf.name)

    def test_no_alerts_when_no_thresholds(self):
        conn, _ = self._make_db(daily_cost_tokens=1_000_000)
        with mock.patch.object(cli, "BUDGET_DAILY", None), \
             mock.patch.object(cli, "BUDGET_MONTHLY", None), \
             mock.patch.object(cli, "BUDGET_SESSION", None):
            alerts = check_budget_alerts(conn)
        self.assertEqual(alerts, [])

    def test_no_alerts_when_under_80_pct(self):
        # $3 spend vs $10 limit = 30% — below the 80% warning threshold
        conn, _ = self._make_db(daily_cost_tokens=1_000_000)  # $3.00
        with mock.patch.object(cli, "BUDGET_DAILY", 10.0), \
             mock.patch.object(cli, "BUDGET_MONTHLY", None), \
             mock.patch.object(cli, "BUDGET_SESSION", None):
            alerts = check_budget_alerts(conn)
        self.assertEqual(alerts, [])

    def test_warning_at_80_pct(self):
        # $3 spend vs $3.75 limit = 80% → warning (not critical)
        conn, _ = self._make_db(daily_cost_tokens=1_000_000)  # $3.00
        with mock.patch.object(cli, "BUDGET_DAILY", 3.75), \
             mock.patch.object(cli, "BUDGET_MONTHLY", None), \
             mock.patch.object(cli, "BUDGET_SESSION", None):
            alerts = check_budget_alerts(conn)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "warning")
        self.assertEqual(alerts[0]["window"], "daily")

    def test_critical_at_100_pct(self):
        # $3 spend vs $3.00 limit = 100% → critical
        conn, _ = self._make_db(daily_cost_tokens=1_000_000)  # $3.00
        with mock.patch.object(cli, "BUDGET_DAILY", 3.00), \
             mock.patch.object(cli, "BUDGET_MONTHLY", None), \
             mock.patch.object(cli, "BUDGET_SESSION", None):
            alerts = check_budget_alerts(conn)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "critical")

    def test_session_threshold_fires_on_expensive_session(self):
        # 2M tokens of Sonnet = $6.00; session limit = $5 → critical
        conn, _ = self._make_db(session_cost_tokens=2_000_000)  # $6.00
        with mock.patch.object(cli, "BUDGET_DAILY", None), \
             mock.patch.object(cli, "BUDGET_MONTHLY", None), \
             mock.patch.object(cli, "BUDGET_SESSION", 5.0):
            alerts = check_budget_alerts(conn)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["window"], "session")
        self.assertEqual(alerts[0]["level"], "critical")

    def test_multiple_windows_can_alert_simultaneously(self):
        # Breach both daily and session thresholds at once
        conn, _ = self._make_db(daily_cost_tokens=1_000_000,  # $3.00
                                 session_cost_tokens=1_000_000)
        with mock.patch.object(cli, "BUDGET_DAILY", 3.00), \
             mock.patch.object(cli, "BUDGET_MONTHLY", None), \
             mock.patch.object(cli, "BUDGET_SESSION", 2.00):
            alerts = check_budget_alerts(conn)
        windows = {a["window"] for a in alerts}
        self.assertIn("daily",   windows)
        self.assertIn("session", windows)

    def test_parse_budget_invalid_value_returns_none(self):
        with mock.patch.dict("os.environ", {"BUDGET_DAILY": "not-a-number"}):
            val = cli._parse_budget("BUDGET_DAILY")
        self.assertIsNone(val)

    def test_parse_budget_zero_returns_none(self):
        with mock.patch.dict("os.environ", {"BUDGET_DAILY": "0"}):
            val = cli._parse_budget("BUDGET_DAILY")
        self.assertIsNone(val)


class TestFmt(unittest.TestCase):
    def test_millions(self):
        self.assertEqual(fmt(1_500_000), "1.50M")
        self.assertEqual(fmt(1_000_000), "1.00M")

    def test_thousands(self):
        self.assertEqual(fmt(1_500), "1.5K")
        self.assertEqual(fmt(1_000), "1.0K")

    def test_small_numbers(self):
        self.assertEqual(fmt(999), "999")
        self.assertEqual(fmt(0), "0")


class TestFmtCost(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(fmt_cost(3.0), "$3.0000")
        self.assertEqual(fmt_cost(0.0001), "$0.0001")
        self.assertEqual(fmt_cost(0), "$0.0000")


class TestPricingConsistency(unittest.TestCase):
    """Ensure CLI pricing matches known Anthropic API rates."""

    def test_opus_pricing(self):
        for model in ("claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 5.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 25.00, f"{model} output price wrong")

    def test_sonnet_pricing(self):
        for model in ("claude-sonnet-4-7", "claude-sonnet-4-6", "claude-sonnet-4-5"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 3.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 15.00, f"{model} output price wrong")

    def test_haiku_pricing(self):
        for model in ("claude-haiku-4-7", "claude-haiku-4-6", "claude-haiku-4-5"):
            p = get_pricing(model)
            self.assertEqual(p["input"], 1.00, f"{model} input price wrong")
            self.assertEqual(p["output"], 5.00, f"{model} output price wrong")


class TestDashboardNoBrowser(unittest.TestCase):
    """The VS Code extension passes --no-browser; CLI users get a browser."""

    def test_no_browser_suppresses_webbrowser(self):
        with mock.patch.object(cli, "cmd_scan"), \
             mock.patch("dashboard.serve") as mock_serve, \
             mock.patch("webbrowser.open") as mock_open, \
             redirect_stdout(io.StringIO()):
            cli.cmd_dashboard(host="127.0.0.1", port=9999, no_browser=True)
            mock_open.assert_not_called()
            mock_serve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
