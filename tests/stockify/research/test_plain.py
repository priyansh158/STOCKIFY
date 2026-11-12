"""Plain-English meanings and sentence builders: one wording, shared by the CLI, report.md and the web pages."""
from __future__ import annotations

import random
import re
import unittest

import tests.support as support  # noqa: F401  — must precede project imports

from stockify.research import bootstrap, plain, registry

RUPEE = "₹"
BANNED_WORDS = ("ddof", "kurtosis", "skew", "skewness", "logit", "nan", "none", "null", "inf", "dsr", "psr", "pbo")
GREEK = re.compile("[Ͱ-Ͽ]")


def _bad_tokens(text: str) -> list[str]:
    """Banned words, raw code names (snake_case) and Greek letters found in a sentence."""
    words = set(re.findall(r"[A-Za-z_]+", text.lower()))
    found = [w for w in BANNED_WORDS if w in words]
    found += [w for w in words if "_" in w]
    if "$" in text:
        found.append("$")
    if GREEK.search(text):
        found.append("greek")
    return found


class MeaningTests(unittest.TestCase):
    def test_every_key_has_a_one_line_meaning(self):
        self.assertEqual(len(plain.KEYS), len(set(plain.KEYS)))
        for key in plain.KEYS:
            text = plain.meaning(key)
            self.assertTrue(text.strip(), key)
            self.assertNotIn("\n", text, key)
            self.assertLessEqual(len(text), 110, f"{key}: {len(text)}")

    def test_keys_are_the_ones_the_plan_names(self):
        expected = (
            "net_pnl return cagr sharpe sortino max_drawdown calmar trades win_rate costs cost_share "
            "dsr psr expected_best_luck trials bootstrap_sharpe bootstrap_mean_return bootstrap_max_drawdown "
            "holdout_unlocks regime_month regime_weekday regime_expiry regime_vix regime_gap "
            "fragile cost_scale extra_ticks latency wf_ratio wf_stability pbo"
        ).split()
        self.assertEqual(set(plain.KEYS), set(expected))

    def test_meanings_have_no_jargon_symbols(self):
        for key in plain.KEYS:
            text = plain.meaning(key)
            self.assertEqual(_bad_tokens(text), [], f"{key}: {text}")

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            plain.meaning("no_such_statistic")

    def test_money_meanings_name_rupees(self):
        for key in ("net_pnl", "costs"):
            self.assertIn(RUPEE, plain.meaning(key))

    def test_objective_labels_cover_every_registry_objective(self):
        self.assertEqual(set(plain.OBJECTIVE_LABELS), set(registry.OBJECTIVES))
        self.assertEqual(plain.OBJECTIVE_LABELS["sharpe"], "steadiness of returns (Sharpe)")
        self.assertEqual(plain.OBJECTIVE_LABELS["net_pnl"], "profit after costs")


class DsrSentenceTests(unittest.TestCase):
    def test_dsr_sentence_for_best_of_12(self):
        s = plain.dsr_sentence(12, 1, 0.85)
        self.assertIn("best of 12 tries", s)
        self.assertIn("after allowing for luck", s)
        self.assertIn("85% chance the edge beats luck", s)
        self.assertIn("steadiness of returns (Sharpe)", s)

    def test_dsr_sentence_names_the_objective_and_the_picked_run_for_a_runner_up(self):
        s = plain.dsr_sentence(12, 4, 0.4, objective="net_pnl", picked_name="orb-fast")
        self.assertIn("4th best of 12 tries by profit after costs", s)
        self.assertIn("the job picked orb-fast", s)
        self.assertIn("runner-up", s)
        self.assertNotIn("after allowing for luck", s)

    def test_runner_up_without_a_picked_name(self):
        s = plain.dsr_sentence(12, 2, 0.4)
        self.assertIn("2nd best of 12 tries", s)
        self.assertIn("runner-up", s)

    def test_dsr_sentence_for_a_single_try(self):
        self.assertEqual(plain.dsr_sentence(1, 1, 0.9), "the only try, so nothing to allow for")

    def test_dsr_sentence_for_a_flat_run(self):
        self.assertEqual(plain.dsr_sentence(12, 1, None, flat=True), "no trades, so no Sharpe to judge")

    def test_dsr_sentence_when_there_are_too_few_days(self):
        s = plain.dsr_sentence(12, 1, None)
        self.assertIn("too few days to say", s)
        self.assertIn("best of 12 tries", s)

    def test_ordinals(self):
        expect = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 10: "10th", 11: "11th", 12: "12th", 13: "13th",
                  21: "21st", 22: "22nd", 23: "23rd", 101: "101st", 111: "111th", 112: "112th", 113: "113th"}
        for n, text in expect.items():
            self.assertEqual(plain.ordinal(n), text)
        self.assertIn("12th best of 500 tries", plain.dsr_sentence(500, 12, 0.5))

    def test_percent_rounds_to_whole_numbers(self):
        self.assertIn("85% chance", plain.dsr_sentence(5, 1, 0.8499))
        self.assertIn(": 0% chance", plain.dsr_sentence(5, 1, 0.0))
        self.assertIn(": 100% chance", plain.dsr_sentence(5, 1, 1.0))

    def test_a_near_certain_value_is_not_rounded_to_100_or_0(self):
        self.assertIn("99.9% chance", plain.dsr_sentence(5, 1, 0.9994))
        self.assertIn("0.1% chance", plain.dsr_sentence(5, 1, 0.0006))
        self.assertIn("99.9% confident", plain.psr_sentence(0.9994))

    def test_non_finite_values_say_too_few_days_and_near_misses_are_clamped(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertIn("too few days to say", plain.dsr_sentence(12, 1, bad))
            self.assertEqual(plain.psr_sentence(bad), "too few days to say")
        self.assertIn(": 100% chance", plain.dsr_sentence(12, 1, 1.0000001))
        self.assertIn(": 0% chance", plain.dsr_sentence(12, 1, -1e-9))
        self.assertIn("100% confident", plain.psr_sentence(1.0000001))

    def test_another_ranking_measure_says_the_allowance_is_on_its_sharpe(self):
        s = plain.dsr_sentence(12, 1, 0.85, objective="net_pnl")
        self.assertEqual(s, "best of 12 tries by profit after costs, after allowing for luck on its Sharpe: "
                            "85% chance the edge beats luck")
        self.assertNotIn("on its Sharpe", plain.dsr_sentence(12, 1, 0.85, objective="sharpe"))
        longest = plain.dsr_sentence(500, 1, 1.0, objective="sortino")
        self.assertLessEqual(len(longest), 160, longest)

    def test_bad_rank_is_rejected(self):
        with self.assertRaises(ValueError):
            plain.dsr_sentence(3, 4, 0.5)
        with self.assertRaises(ValueError):
            plain.dsr_sentence(3, 0, 0.5)


class PsrSentenceTests(unittest.TestCase):
    def test_psr_sentence(self):
        s = plain.psr_sentence(0.93)
        self.assertIn("93%", s)
        self.assertIn("above zero", s)
        self.assertNotIn("luck", s)  # PSR makes no allowance for the number of tries

    def test_psr_without_a_value(self):
        self.assertEqual(plain.psr_sentence(None), "too few days to say")


class IntervalSentenceTests(unittest.TestCase):
    def test_interval_sentence_with_and_without_bounds(self):
        s = plain.interval_sentence("Sharpe", bootstrap.Interval(1.0, 0.4, 1.9), "")
        self.assertEqual(s, "between 0.4 and 1.9 in 9 of 10 re-draws of your own days")
        for iv in (bootstrap.Interval(None, None, None), bootstrap.Interval(1.0, None, 2.0),
                   bootstrap.Interval(1.0, 0.1, None)):
            self.assertEqual(plain.interval_sentence("Sharpe", iv, ""), "too few days to say")

    def test_unit_is_applied_and_rupees_are_grouped(self):
        s = plain.interval_sentence("Net profit", bootstrap.Interval(0.0, -1234.0, 1234567.0), RUPEE)
        self.assertIn(f"-{RUPEE}1,234", s)
        self.assertIn(f"{RUPEE}12,34,567", s)
        p = plain.interval_sentence("Max drawdown", bootstrap.Interval(0.05, 0.021, 0.134), "%")
        self.assertIn("between 2.1% and 13.4% in", p)

    def test_ci_never_prints_100_or_0_percent(self):
        iv = bootstrap.Interval(1.0, 0.4, 1.9)
        self.assertIn("in 99.9% of re-draws", plain.interval_sentence("x", iv, "", ci=0.999))
        self.assertIn("in 0.1% of re-draws", plain.interval_sentence("x", iv, "", ci=0.001))
        self.assertIn("in 99.95% of re-draws", plain.interval_sentence("x", iv, "", ci=0.9995))
        with self.assertRaises(ValueError):
            plain.interval_sentence("x", iv, "", ci=1.0)

    def test_rupees_round_before_grouping(self):
        self.assertEqual(plain.rupees(999.996), f"{RUPEE}1,000")
        self.assertEqual(plain.rupees(-999.996), f"-{RUPEE}1,000")
        self.assertEqual(plain.rupees(999.994), f"{RUPEE}999.99")
        self.assertEqual(plain.rupees(1000.4), f"{RUPEE}1,000")
        self.assertEqual(plain.rupees(99999.5), f"{RUPEE}1,00,000")
        self.assertEqual(plain.rupees(-0.001), f"{RUPEE}0")

    def test_sharpe_meaning_does_not_say_higher_is_steadier(self):
        self.assertNotIn("steadier", plain.meaning("sharpe"))
        self.assertIn("more return for the swing taken", plain.meaning("sharpe"))

    def test_ci_keyword_changes_the_wording(self):
        iv = bootstrap.Interval(1.0, 0.4, 1.9)
        self.assertIn("in 19 of 20 re-draws", plain.interval_sentence("x", iv, "", ci=0.95))
        self.assertIn("in 8 of 10 re-draws", plain.interval_sentence("x", iv, "", ci=0.80))
        self.assertIn("in 99% of re-draws", plain.interval_sentence("x", iv, "", ci=0.99))

    def test_rounding_and_no_negative_zero(self):
        s = plain.interval_sentence("x", bootstrap.Interval(0.0, -0.001, 0.004), "")
        self.assertTrue(s.startswith("between -0.001 and 0.004 in"), s)
        s2 = plain.interval_sentence("x", bootstrap.Interval(0.0, -1.23456, 1.98765), "")
        self.assertTrue(s2.startswith("between -1.23 and 1.99 in"), s2)


class SweepTests(unittest.TestCase):
    def test_sentences_have_no_greek_letters_or_raw_codes(self):
        for objective in registry.OBJECTIVES:
            for s in (plain.dsr_sentence(12, 1, 0.85, objective=objective),
                      plain.dsr_sentence(12, 3, 0.5, objective=objective, picked_name="alpha one")):
                self.assertEqual(_bad_tokens(s), [], s)

    def test_seeded_sweep_over_random_inputs(self):
        rng = random.Random(20261112)
        for _ in range(3000):
            n = rng.randint(1, 500)
            rank = rng.randint(1, n)
            dsr = rng.choice([None, rng.random(), 0.0, 1.0])
            flat = rng.random() < 0.2
            objective = rng.choice(registry.OBJECTIVES)
            name = rng.choice([None, "orb-fast", "mean reversion 3"])
            s = plain.dsr_sentence(n, rank, dsr, objective=objective, picked_name=name, flat=flat)
            tag = (n, rank, dsr, flat, objective, name, s)
            self.assertNotIn("\n", s, tag)
            self.assertLessEqual(len(s), 160, tag)
            self.assertEqual(_bad_tokens(s), [], tag)
            # "best of N tries" with no ordinal in front is said exactly for the winner of a real field
            plain_best = s.startswith(f"best of {n} tries")
            self.assertEqual(plain_best, rank == 1 and n > 1 and not flat, tag)
            if rank > 1 and not flat:
                self.assertIn(f"best of {n} tries", s, tag)
            if rank == 1 and n > 1 and not flat and dsr is not None:
                shown = float(re.search(r": ([0-9.]+)% chance", s).group(1))
                self.assertLessEqual(abs(shown - dsr * 100), 0.5, tag)
                if 0.005 < dsr < 0.995:
                    self.assertEqual(shown, float(round(dsr * 100)), tag)
        for _ in range(500):
            iv = bootstrap.Interval(*[rng.choice([None, rng.uniform(-1e7, 1e7)]) for _ in range(3)])
            unit = rng.choice(["", "%", RUPEE])
            s = plain.interval_sentence("x", iv, unit)
            self.assertNotIn("\n", s)
            self.assertLessEqual(len(s), 160, s)
            self.assertEqual(_bad_tokens(s), [], s)
            if iv.lo is None or iv.hi is None:
                self.assertEqual(s, "too few days to say")
            else:
                lo_text, hi_text = plain._with_unit(iv.lo, unit), plain._with_unit(iv.hi, unit)
                self.assertTrue(s.startswith(f"between {lo_text} and {hi_text} in "), s)


class RegimeSentenceSweepTests(unittest.TestCase):
    """The banned-token sweep over regime_sentence: any bucket, any size, never jargon or a stray code name."""

    def test_regime_sentences_are_plain_for_random_buckets(self):
        from stockify.research import regimes
        rng = random.Random(10)
        labels = ["Monday", "Friday", "2026-01", "expiry day", "other days", "VIX below 13", "VIX 13–16",
                  "VIX 20 or more", "gap down more than 0.5%", "flat (within 0.1%)", "gap up 0.5% or more", "no data"]
        for _ in range(500):
            days = rng.choice([0, 1, 2, 9, 10, 41, 400])
            pnl = rng.randint(-10**9, 10**9)
            up = rng.randint(0, days)
            b = regimes.Bucket(rng.choice(labels), days, pnl, None if days == 0 else round(pnl / days), up,
                               None if days == 0 else up / days, days < 10)
            s = plain.regime_sentence(b)
            self.assertNotIn("\n", s, b)
            self.assertLessEqual(len(s), 120, s)
            self.assertEqual(_bad_tokens(s), [], s)
            self.assertNotIn("significan", s)
            self.assertNotIn("better", s)
            if 0 < days < 10:
                self.assertIn("take with care", s)
            else:
                self.assertNotIn("take with care", s)


if __name__ == "__main__":
    unittest.main()
