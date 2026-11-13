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


def _sens(verdict, base, costs=("done", None), ticks=("done", None), ckey="costs_x1.5", tkey="slip_plus1"):
    """A SensitivityResult whose 1.5-times-costs and +1-tick rows have the given (status, net) pairs."""
    from stockify.research.sensitivity import SensitivityResult, SensitivityRow
    rows = [SensitivityRow("baseline", "baseline", "Baseline", "r0", "baseline", base, None, None, None)]
    for key, axis, (st, net) in ((ckey, "costs", costs), (tkey, "slippage", ticks)):
        rows.append(SensitivityRow(key, axis, key, None if st != "done" else "x", st, net, None,
                                   None if net is None else net - base, None if st == "done" else "the run failed"))
    return SensitivityResult("r0", "j", rows, base, verdict == "fragile", verdict, [])


class PlainReasonTests(unittest.TestCase):
    """A stored failure text is shown only when the code itself wrote it: exception text never leaves the machine."""

    def test_the_codes_own_sentences_pass_unchanged(self):
        for text in sorted(plain.PLAIN_SENTENCES):
            self.assertEqual(plain.plain_reason(text), text)
        self.assertEqual(plain.plain_reason("the run did not report its folder."), "the run did not report its folder")

    def test_exception_text_is_never_returned(self):
        # none of these has an underscore, a slash, a newline or "Traceback", the old filter's only tests
        for raw in ("ValueError: bad window", "KeyError: 'fast'", "boom", "C:" + chr(92) + "Users" + chr(92) + "x.py not found",
                    "the lab is at D:/data", "RuntimeError: the pool fell over", "exit code 3", "x" * 200,
                    "line1" + chr(10) + "line2", "IndexError: list index out of range"):
            got = plain.plain_reason(raw)
            self.assertEqual(got, plain.GENERIC_FAILURE, raw)

    def test_a_leading_class_name_maps_through_the_fixed_table(self):
        self.assertEqual(plain.plain_reason("HoldoutLocked: the holdout starts 2026-08-01 and this run reaches it"),
                         "the holdout is locked")
        self.assertEqual(plain.plain_reason("RunConfigError: strategy.params.slow must be above fast"),
                         "the settings were not accepted")
        self.assertEqual(plain.plain_reason("SweepSpecError: the pool returned 2 outcomes for 3 runs"),
                         "the run pool returned the wrong number of results")
        for text in set(dict(plain.PLAIN_FAILURES).values()):
            self.assertIn(text, plain.PLAIN_SENTENCES)

    def test_nothing_in_gives_nothing_out_and_a_caller_can_ask_for_no_fallback(self):
        for raw in (None, "", "   ", 5):
            self.assertIsNone(plain.plain_reason(raw))
        self.assertIsNone(plain.plain_reason("ValueError: bad window", fallback=None))
        self.assertEqual(plain.plain_reason("ValueError: bad window", fallback="x"), "x")
        self.assertEqual(plain.plain_reason("HoldoutLocked: x", fallback=None), "the holdout is locked")


class NotRunReasonTests(unittest.TestCase):
    """Why a sensitivity check could not run: the refusal is named by what it is about, a raw exception is generic."""

    def test_the_real_refusals_get_their_own_line(self):
        from stockify import runconfig
        # the validator's actual texts (not copies): the price basis one and the extra ticks one
        self.assertEqual(plain.not_run_reason("price_basis: eod_close runs use NSE files, basis raw"), plain.NOT_RUN_RAW_PRICES)
        self.assertEqual(plain.not_run_reason(f"fill_model.slippage.extra_ticks: must be at most {runconfig.MAX_EXTRA_TICKS}"),
                         plain.NOT_RUN_EXTRA_TICKS)
        self.assertEqual(plain.not_run_reason("no quotes in these packs"), plain.NOT_RUN_NO_QUOTES)
        self.assertEqual(plain.not_run_reason("data.packs[0]: pack abc not found in D:/x"), plain.NOT_RUN_DATA)

    def test_every_not_run_line_is_on_the_allow_list_and_passes_plain_reason(self):
        self.assertEqual(len(set(plain.NOT_RUN_REASONS)), len(plain.NOT_RUN_REASONS))
        for line in plain.NOT_RUN_REASONS:
            self.assertEqual(plain.plain_reason(line), line)
            self.assertEqual(plain.not_run_reason(line), line)

    def test_a_raw_exception_or_unknown_text_is_generic_and_nothing_is_nothing(self):
        for raw in ("ValueError: bad window", "KeyError: 'fast'", "C:" + chr(92) + "x" + chr(92) + "y.py failed", "x" * 200,
                    "line1" + chr(10) + "line2", "RuntimeError: no quotes in these packs"):
            self.assertEqual(plain.not_run_reason(raw), plain.GENERIC_FAILURE, raw)
        for raw in (None, "", "  ", 3):
            self.assertIsNone(plain.not_run_reason(raw))

    def test_no_not_run_line_carries_a_path_a_value_or_a_code_name(self):
        for line in plain.NOT_RUN_REASONS:
            self.assertEqual([c for c in line if c in "_/" + chr(92) + chr(10)], [], line)
            self.assertFalse(any(ch.isdigit() for ch in line), line)


class FragileSentenceTests(unittest.TestCase):
    K = dict(fragile_cost_scale="1.5", fragile_extra_ticks=1)

    def test_fragile_sentence_for_each_verdict(self):
        k = self.K
        f = plain.fragile_sentence(_sens("fragile", 4_120_000, costs=("done", -310_000), ticks=("done", 100)), **k)
        self.assertEqual(f, "Breaks if all costs rise by half: net P&L turns from +₹41,200 to -₹3,100.")
        both = plain.fragile_sentence(_sens("fragile", 4_120_000, costs=("done", -310_000), ticks=("done", -5_000)), **k)
        self.assertEqual(both, "Breaks if all costs rise by half: net P&L turns from +₹41,200 to -₹3,100. "
                               "It also breaks with one extra tick of slippage: net P&L turns from +₹41,200 to -₹50.")
        r = plain.fragile_sentence(_sens("robust", 4_120_000, costs=("done", 2_000_000), ticks=("done", 3_000_000)), **k)
        self.assertEqual(r, "Survives 1.5 times all costs and one extra tick of slippage: net P&L does not turn negative "
                            "(+₹20,000 and +₹30,000, from +₹41,200).")
        a = plain.fragile_sentence(_sens("already_losing", -310_000), **k)
        self.assertEqual(a, "Already losing money (-₹3,100), so there is nothing to break; it is not called fragile.")
        i = plain.fragile_sentence(_sens("inconclusive", 4_120_000, costs=("failed", None), ticks=("done", 3_000_000)), **k)
        self.assertEqual(i, "Can't say yet: the run with 1.5 times all costs didn't finish, so it isn't called robust.")

    def test_inconclusive_names_each_unchecked_case_and_says_why(self):
        k = self.K
        na = plain.fragile_sentence(_sens("inconclusive", 4_120_000, costs=("done", 3_000_000),
                                          ticks=("not_applicable", None)), **k)
        self.assertEqual(na, "Can't say yet: one extra tick of slippage couldn't be run for this strategy "
                             "(the run failed), so it isn't called robust.")
        both = plain.fragile_sentence(_sens("inconclusive", 4_120_000, costs=("failed", None),
                                            ticks=("not_applicable", None)), **k)
        self.assertEqual(both, "Can't say yet: the run with 1.5 times all costs didn't finish; and one extra tick "
                               "of slippage couldn't be run for this strategy (the run failed), so it isn't called robust.")
        for raw, why in ((None, None), ("", None), ("bad_key in C:" + chr(92) + "x", plain.GENERIC_FAILURE),
                         ("x" * 200, plain.GENERIC_FAILURE), ("line1" + chr(10) + "line2", plain.GENERIC_FAILURE),
                         ("ValueError: bad window", plain.GENERIC_FAILURE),
                         ("price_basis: eod_close runs use NSE files, basis raw", plain.NOT_RUN_RAW_PRICES)):
            s = _sens("inconclusive", 4_120_000, costs=("done", 3_000_000), ticks=("not_applicable", None))
            s.rows[2] = type(s.rows[2])(**{**s.rows[2].__dict__, "error": raw})
            text = plain.fragile_sentence(s, **k)
            self.assertEqual(text, "Can't say yet: one extra tick of slippage couldn't be run for this strategy"
                                   + (f" ({why})" if why else "") + ", so it isn't called robust.", raw)
            self.assertEqual(_bad_tokens(text), [], text)
        self.assertNotIn("1.5-times", na)

    def test_a_baseline_of_exactly_zero_is_not_called_losing(self):
        z = plain.fragile_sentence(_sens("already_losing", 0), **self.K)
        self.assertEqual(z, "Made no money (₹0), so there is nothing to break; it is not called fragile.")
        self.assertNotIn("losing", z)

    def test_a_robust_result_with_a_negative_check_is_refused(self):
        with self.assertRaises(ValueError):
            plain.fragile_sentence(_sens("robust", 4_120_000, costs=("done", -1), ticks=("done", 5)), **self.K)

    def test_fragile_sentence_names_the_axis_and_both_rupee_figures(self):
        k = self.K
        only_ticks = plain.fragile_sentence(_sens("fragile", 4_120_000, costs=("done", 100_000),
                                                  ticks=("done", -310_000)), **k)
        self.assertEqual(only_ticks, "Breaks with one extra tick of slippage: net P&L turns from +₹41,200 to -₹3,100.")
        self.assertNotIn("costs", only_ticks)
        two = plain.fragile_sentence(_sens("fragile", 4_120_000, costs=("done", -310_000), ticks=("done", 5),
                                              ckey="costs_x2", tkey="slip_plus2"),
                                     fragile_cost_scale="2", fragile_extra_ticks=2)
        self.assertTrue(two.startswith("Breaks if all costs double: "), two)
        for text in (only_ticks, two):
            self.assertNotIn("brokerage", text.lower())
            self.assertNotIn("None", text)
            self.assertNotIn("nan", text.lower())
            self.assertEqual(_bad_tokens(text), [], text)

    def test_unknown_verdict_raises(self):
        with self.assertRaises(ValueError):
            plain.fragile_sentence(_sens("maybe", 1), **self.K)



class WfSentenceTests(unittest.TestCase):
    """WF 6.6: the out-of-sample ratio and the stability of the chosen settings, in plain words."""

    def test_too_few_out_of_sample_days_replace_the_figure_with_the_constant(self):
        want = plain.NOT_ENOUGH_OOS.format(n=5, m=60)
        self.assertEqual(want, "not enough out-of-sample days to judge (5 of 60 needed)")
        text = plain.wf_dsr_sentence(8, 0.00001, oos_days=5, min_oos_days=60)
        self.assertIn(want, text)
        self.assertIn("after allowing for luck over 8 tries", text)
        self.assertNotIn("%", text)
        self.assertNotIn("beats luck", text)
        self.assertIn("% chance the out-of-sample edge beats luck", plain.wf_dsr_sentence(8, 0.7, oos_days=60,
                                                                                          min_oos_days=60))
        self.assertIn("% chance", plain.wf_dsr_sentence(8, 0.7))                  # no day count given: unchanged

    @staticmethod
    def summary(**kw):
        from stockify.research import walkforward as wf
        base = dict(windows=7, is_windows=7, oos_days=120, is_sharpe=2.0, oos_sharpe=0.76, ratio=0.38,
                    chosen_modal_share=4 / 7, n_trials=28, var_sharpe=0.001, dsr=None)
        base.update(kw)
        return wf.WalkforwardSummary(**base)

    def test_wf_sentence_ratio_none_and_stability(self):
        s = plain.wf_sentence(self.summary())
        self.assertIn("kept 38% of the in-sample Sharpe out of sample", s)
        self.assertIn("the same settings won 4 of 7 windows", s)
        self.assertNotIn("\n", s)
        # in-sample not positive: there is no ratio, and the sentence says why
        s = plain.wf_sentence(self.summary(is_sharpe=-0.4, ratio=None))
        self.assertIn("in-sample was not positive, so there is no ratio", s)
        self.assertNotIn("kept", s)
        # nothing to compare at all
        self.assertIn("no out-of-sample days", plain.wf_sentence(self.summary(oos_days=0, oos_sharpe=None, ratio=None,
                                                                              is_sharpe=None, is_windows=0)))
        self.assertIn("no in-sample Sharpe", plain.wf_sentence(self.summary(is_sharpe=None, ratio=None)))

    def test_a_negative_out_of_sample_sharpe_is_not_a_kept_share(self):
        s = plain.wf_ratio_sentence(self.summary(oos_sharpe=-0.5, ratio=-0.25))
        self.assertNotIn("kept ", s.replace("was kept", ""))       # no "kept N%" share for a loss
        self.assertNotIn("%", s)
        self.assertIn("below zero", s)

    def test_stability_counts_the_windows_that_chose_a_setting(self):
        s = self.summary(windows=7, chosen_modal_share=3 / 5)
        self.assertEqual(plain.wf_stability_sentence(s, chosen_windows=5),
                         "the same settings won 3 of 5 windows that picked one")
        self.assertEqual(plain.wf_stability_sentence(self.summary(chosen_modal_share=None)),
                         "no window picked settings, so there is no stability to report")
        # a share that is not a whole count of the windows is shown as a percentage, never a made-up count
        self.assertEqual(plain.wf_stability_sentence(self.summary(windows=7, chosen_modal_share=0.5)),
                         "the same settings won 50% of the windows")

    def test_one_window_has_no_stability_to_speak_of(self):
        self.assertEqual(plain.wf_stability_sentence(self.summary(windows=1, chosen_modal_share=1.0)),
                         "only one window, so there is no stability to report")

    def test_wf_sentences_are_plain_over_seeded_summaries(self):
        rng = random.Random(22)
        for _ in range(200):
            n = rng.randint(1, 9)
            is_sr = rng.choice([None, rng.uniform(-2, 3)])
            oos = rng.choice([None, rng.uniform(-2, 3)])
            ratio = oos / is_sr if is_sr is not None and oos is not None and is_sr > 0 else None
            s = self.summary(windows=n, is_sharpe=is_sr, oos_sharpe=oos, ratio=ratio, oos_days=rng.choice([0, 20]),
                             chosen_modal_share=rng.choice([None, rng.randint(1, n) / n]))
            text = plain.wf_sentence(s)
            self.assertEqual(_bad_tokens(text), [], text)
            self.assertNotIn("\n", text)

    def test_wf_dsr_sentence_says_the_days_were_never_used_to_choose_and_that_it_is_cautious(self):
        text = plain.wf_dsr_sentence(28, 0.61)
        self.assertIn("61% chance", text)
        self.assertIn("28 tries", text)
        self.assertIn("never used to choose", text)
        self.assertIn("cautious", text)
        self.assertNotIn("best of", text)                       # the out-of-sample curve is not "the best of N"
        self.assertEqual(_bad_tokens(text), [])
        self.assertEqual(plain.wf_dsr_sentence(1, 0.9), "only one try, so nothing to allow for")
        self.assertIn(plain.TOO_FEW_DAYS, plain.wf_dsr_sentence(28, None))
        with self.assertRaises(ValueError):
            plain.wf_dsr_sentence(0, 0.5)


class OverviewSentenceTests(unittest.TestCase):
    """Task 33: the /research overview's two sentences, written once on the server."""

    def test_family_best_sentence_covers_no_tries_one_try_and_a_missing_figure(self):
        self.assertEqual(plain.family_best_sentence(0, None, "sharpe"), "no finished tries yet")
        self.assertEqual(plain.family_best_sentence(1, 0.5, "sharpe"), "the only try, so nothing to allow for")
        text = plain.family_best_sentence(12, 0.874, "sharpe")
        self.assertIn("best of 12 tries", text)
        self.assertIn("87% chance the edge beats luck", text)
        self.assertEqual(_bad_tokens(text), [])
        self.assertIn(plain.TOO_FEW_DAYS, plain.family_best_sentence(12, None, "sharpe"))
        self.assertIn("profit after costs", plain.family_best_sentence(3, 0.5, "net_pnl"))
        with self.assertRaises(ValueError):
            plain.family_best_sentence(3, 0.5, "luck")

    def test_job_status_sentence_says_running_finished_or_why_it_failed(self):
        self.assertEqual(plain.job_status_sentence("running", None), "still running")
        self.assertEqual(plain.job_status_sentence("done", None), "finished")
        self.assertEqual(plain.job_status_sentence("failed", "no data for that day."), "failed: no data for that day")
        for raw in (None, "", "KeyError: run_id", "Traceback (most recent call last)"):
            self.assertEqual(plain.job_status_sentence("failed", raw), "failed, and no readable reason was saved")
        self.assertEqual(plain.job_status_sentence("running", "ignored"), "still running")
        self.assertEqual(plain.job_status_sentence("weird", None), "in an unrecognised state (weird)")
        self.assertEqual(plain.job_status_sentence("a/b c", None), "in an unrecognised state")


if __name__ == "__main__":
    unittest.main()
