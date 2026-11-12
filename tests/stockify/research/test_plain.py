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
        self.assertIn("85% confident the edge is real", s)
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
        self.assertIn("85% confident", plain.dsr_sentence(5, 1, 0.8499))
        self.assertIn("0% confident", plain.dsr_sentence(5, 1, 0.0))
        self.assertIn("100% confident", plain.dsr_sentence(5, 1, 1.0))

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
        self.assertEqual(s, "between 0.4 and 1.9 in 9 of 10 re-shuffles of your own days")
        for iv in (bootstrap.Interval(None, None, None), bootstrap.Interval(1.0, None, 2.0),
                   bootstrap.Interval(1.0, 0.1, None)):
            self.assertEqual(plain.interval_sentence("Sharpe", iv, ""), "too few days to say")

    def test_unit_is_applied_and_rupees_are_grouped(self):
        s = plain.interval_sentence("Net profit", bootstrap.Interval(0.0, -1234.0, 1234567.0), RUPEE)
        self.assertIn(f"-{RUPEE}1,234", s)
        self.assertIn(f"{RUPEE}12,34,567", s)
        p = plain.interval_sentence("Max drawdown", bootstrap.Interval(0.05, 0.021, 0.134), "%")
        self.assertIn("between 2.1% and 13.4% in", p)

    def test_ci_keyword_changes_the_wording(self):
        iv = bootstrap.Interval(1.0, 0.4, 1.9)
        self.assertIn("in 19 of 20 re-shuffles", plain.interval_sentence("x", iv, "", ci=0.95))
        self.assertIn("in 8 of 10 re-shuffles", plain.interval_sentence("x", iv, "", ci=0.80))
        self.assertIn("in 99% of re-shuffles", plain.interval_sentence("x", iv, "", ci=0.99))

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
        for _ in range(500):
            iv = bootstrap.Interval(*[rng.choice([None, rng.uniform(-1e7, 1e7)]) for _ in range(3)])
            s = plain.interval_sentence("x", iv, rng.choice(["", "%", RUPEE]))
            self.assertNotIn("\n", s)
            self.assertLessEqual(len(s), 160, s)
            self.assertEqual(_bad_tokens(s), [], s)


if __name__ == "__main__":
    unittest.main()
