"""Tests for cli.py - pricing, formatting, and cost calculation."""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock
import cli
from cli import get_pricing, calc_cost, fmt, fmt_cost, PRICING


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
