"""HD 4.1: nightly evidence: oracle backtests of each voting strategy against buy-and-hold, year by year."""
from __future__ import annotations

import json
import math
import time
import unittest
from datetime import date, timedelta
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as lab_store
from stockify import research, timeutil
from stockify.data import history, instruments, refdata
from stockify.desk import evidence, replay
from stockify.desk import store as desk_store
from tests.stockify.test_bundle import HEADER

PKG = "stockify.strategies."


def holding(value):
    """research.settings() with `holdout_from` set to `value`; the shipped file is never touched."""
    return mock.patch.object(research, "settings", return_value={**research.settings(), "holdout_from": value})


class GradeTests(unittest.TestCase):
    def test_grade_thresholds(self):
        cases = [
            # won, total, beat_overall, trades, years -> grade
            ((70, 100, True, 20, 5.0), "strong"),
            ((69, 100, True, 20, 5.0), "some"),
            ((70, 100, False, 20, 5.0), "some"),
            ((55, 100, True, 20, 5.0), "some"),
            ((54, 100, True, 20, 5.0), "weak"),
            ((54, 100, False, 20, 5.0), "weak"),
            ((100, 100, True, 7, 5.0), "none"),
            ((100, 100, True, 8, 5.0), "strong"),
            ((100, 100, True, 20, 2.9), "none"),
            ((100, 100, True, 20, 3.0), "strong"),
            ((0, 0, True, 20, 5.0), "none"),                  # no window long enough to judge
        ]
        for args, want in cases:
            with self.subTest(args=args):
                self.assertEqual(evidence.grade(*args), want)


class EvidenceEndTests(unittest.TestCase):
    def test_evidence_end_respects_the_holdout(self):
        with holding("2026-08-01"):
            self.assertEqual(evidence.evidence_end(date(2026, 11, 17)), date(2026, 7, 31))
            self.assertEqual(evidence.evidence_end(date(2026, 5, 1)), date(2026, 4, 30))
        with holding(None):
            self.assertEqual(evidence.evidence_end(date(2026, 11, 17)), date(2026, 11, 16))

    def test_the_holdout_is_read_from_research_json_not_hard_coded(self):
        with holding("2025-03-10"):
            self.assertEqual(evidence.evidence_end(date(2026, 11, 17)), date(2025, 3, 9))


class SharpeAndWindowTests(unittest.TestCase):
    def test_sharpe_of_daily_returns(self):
        navs = [100, 101, 100, 102]
        r = [0.01, 100 / 101 - 1, 0.02]
        mean = sum(r) / 3
        sd = math.sqrt(sum((x - mean) ** 2 for x in r) / 2)
        self.assertAlmostEqual(evidence.sharpe(navs), mean / sd * math.sqrt(252))
        self.assertIsNone(evidence.sharpe([100, 100, 100]))           # flat: undefined
        self.assertIsNone(evidence.sharpe([100, 101]))                # one return: undefined

    def test_windows_are_calendar_years_with_short_ends_dropped(self):
        def eq(start: str, n: int, step: int):
            d, out, nav = date.fromisoformat(start), [], 1000
            while len(out) < n:
                if d.weekday() < 5:
                    nav += step if len(out) % 2 else -step // 2
                    out.append((d.isoformat(), nav))
                d += timedelta(days=1)
            return out
        strat = eq("2021-08-02", 600, 6)       # 2021 short (~110 days), 2022 full, 2023 partial (~230 days)
        hold = eq("2021-08-02", 600, 3)
        ws = evidence.windows(strat, hold, capital=1000)
        self.assertEqual([w["year"] for w in ws], [2022, 2023])
        for w in ws:
            self.assertEqual(set(w), {"year", "sharpe", "buy_hold_sharpe", "won"})
            self.assertEqual(w["won"], w["sharpe"] is not None and w["buy_hold_sharpe"] is not None
                             and w["sharpe"] > w["buy_hold_sharpe"])

    def test_an_undefined_sharpe_never_wins(self):
        days = [f"2022-01-{d:02d}" for d in range(3, 31)]
        flat = [(d, 1000) for d in days]
        down = [(d, 1000 - i) for i, d in enumerate(days)]
        self.assertFalse(evidence.window_won(None, -5.0))
        self.assertFalse(evidence.window_won(1.0, None))
        self.assertTrue(evidence.window_won(0.1, -5.0))
        self.assertFalse(evidence.window_won(0.1, 0.1))
        ws = evidence.windows(flat, down, capital=1000, min_days=10)
        self.assertEqual([(w["year"], w["sharpe"], w["won"]) for w in ws], [(2022, None, False)])

    def test_note_sentences(self):
        self.assertEqual(evidence.note(5, 8, True, 20, 8.0, "strong"),
                         "Beat holding the stock in 5 of 8 years, and over the whole span.")
        self.assertEqual(evidence.note(5, 8, False, 20, 8.0, "some"),
                         "Beat holding the stock in 5 of 8 years, but not over the whole span.")
        self.assertEqual(evidence.note(1, 1, True, 1, 1.0, "none"),
                         "Too little to judge: 1 trade in 1.0 years (needs 8 trades and 3 years).")
        self.assertEqual(evidence.note(3, 4, True, 30, 2.5, "none"),
                         "Too little to judge: 30 trades in 2.5 years (needs 8 trades and 3 years).")


# ── A synthetic stock with years of daily bars in the sandbox's bar cache ────

FIRST_DAY = date(2021, 1, 4)


def _weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def install_stock(n: int) -> tuple[int, list[date]]:
    """TESTCO on NSE (specs from 2020, so long series resolve) with `n` weekdays of slow waves from 2021-01-04."""
    instruments.sync(date(2020, 1, 1), {"NSE": f"{HEADER}\n500001,1,TESTCO,TESTCO,0,,0,0.05,1,EQ,NSE,NSE\n"},
                     underlyings={})
    iid = instruments.find("NSE", "TESTCO")
    days = _weekdays(FIRST_DAY, n)
    p, prev = 100_000, 100_000
    with history._Tx() as conn:
        for i, d in enumerate(days):
            p = max(40_000, p + int(900 * math.sin(i / 9.0) + 120 * math.sin(i / 2.3) + 300 * math.sin(i / 41.0)
                                    + 40) // 5 * 5)
            conn.execute("INSERT INTO bars VALUES (?, 'day', ?, ?, ?, ?, ?, 10000000, NULL, '2026-01-01')",
                         (iid, timeutil.ist_ns(d, "00:00"), prev, max(prev, p) + 100, min(prev, p) - 100, p))
            prev = p
    return iid, days


TWO = ((PKG + "donchian_breakout:DonchianBreakout", {}), (PKG + "macd_cross:MacdCross", {}))


class RunForTests(SandboxedDBTest):
    db_modules = (refdata, history, lab_store, desk_store)

    def test_run_for_writes_one_row_per_strategy_and_never_a_runs_row(self):
        iid, days = install_stock(1180)                    # 2021-01-04 .. 2025-07-11, about 4.5 years
        today = days[-1] + timedelta(days=3)
        before = len(lab_store.list_runs())
        t0 = time.perf_counter()
        with holding("2026-08-01"):
            rows = evidence.run_for(iid, "TESTCO", today, voters=TWO)
        took = time.perf_counter() - t0
        print(f"\n[timing] run_for: 2 strategies + buy-and-hold over {len(days)} daily bars: {took:.1f} s "
              f"({took / 3 / (len(days) / 252):.2f} s per strategy-year)")
        self.assertEqual(len(lab_store.list_runs()), before)                  # no `runs` row, ever
        self.assertEqual([r["ref"] for r in rows], [ref for ref, _ in TWO])
        stored = desk_store.latest_evidence(iid)
        self.assertEqual(sorted(r["ref"] for r in stored), sorted(ref for ref, _ in TWO))
        for r in rows:
            with self.subTest(ref=r["ref"]):
                self.assertEqual(r["trade_date"], today.isoformat())
                self.assertEqual(r["instrument_id"], iid)
                self.assertEqual((r["from_date"], r["to_date"]), (days[0].isoformat(), days[-1].isoformat()))
                self.assertEqual(r["params"]["symbol"], "NSE:TESTCO")
                self.assertEqual([w["year"] for w in r["windows"]], [2021, 2022, 2023, 2024, 2025])
                self.assertEqual(r["windows_total"], 5)
                self.assertEqual(r["windows_won"], sum(w["won"] for w in r["windows"]))
                self.assertGreater(r["trades"], 0)
                self.assertIn(r["grade"], ("strong", "some", "weak", "none"))
                beat = r["sharpe"] is not None and r["buy_hold_sharpe"] is not None \
                    and r["sharpe"] > r["buy_hold_sharpe"]
                self.assertEqual(r["grade"], evidence.grade(r["windows_won"], r["windows_total"], beat, r["trades"],
                                                            evidence.span_years(days[0], days[-1])))
                self.assertTrue(r["note"].endswith("."), r["note"])
                self.assertIsNotNone(r["buy_hold_cagr"])
                json.dumps(r["windows"])
        # Buy-and-hold's numbers are the same for every strategy on the stock.
        self.assertEqual(len({(r["buy_hold_sharpe"], r["buy_hold_cagr"]) for r in rows}), 1)
        self.assertEqual(len({tuple(w["buy_hold_sharpe"] for w in r["windows"]) for r in rows}), 1)

    def test_backtests_stop_the_day_before_the_holdout(self):
        iid, days = install_stock(900)                     # 2021-01-04 .. 2024-06-14
        hold = days[700]
        with holding(hold.isoformat()):
            rows = evidence.run_for(iid, "NSE:TESTCO", days[-1] + timedelta(days=1), voters=TWO[:1])
        self.assertEqual(rows[0]["to_date"], days[699].isoformat())

    def test_no_bars_before_the_end_gives_no_rows(self):
        instruments.sync(date(2020, 1, 1), {"NSE": f"{HEADER}\n500001,1,TESTCO,TESTCO,0,,0,0.05,1,EQ,NSE,NSE\n"},
                         underlyings={})
        iid = instruments.find("NSE", "TESTCO")
        with holding("2026-08-01"):
            self.assertEqual(evidence.run_for(iid, "TESTCO", date(2026, 11, 17), voters=TWO), [])
        self.assertEqual(desk_store.latest_evidence(iid), [])

    def test_the_default_voters_are_the_replays(self):
        self.assertEqual(evidence.VOTERS, replay.VOTERS)
        self.assertEqual(evidence.BASELINE, replay.BASELINE)
        cfg = evidence.config(PKG + "macd_cross:MacdCross", {}, "NSE:TESTCO", date(2021, 1, 4), date(2025, 5, 30))
        self.assertEqual(cfg["capital_paise"], 100_000_000)
        self.assertEqual(cfg["fill_model"], {"kind": "bar", "limit_touch_policy": "through", "participation": "0.10",
                                             "slippage": {"kind": "bps", "x": "0.0005"}})
        self.assertEqual(cfg["costs"]["broker"], "zerodha")
        self.assertEqual(cfg["data"]["packs"][0]["spec"], {"dataset": "bars", "interval": "day",
                                                           "instruments": ["NSE:TESTCO"],
                                                           "from": "2021-01-04", "to": "2025-05-30"})
        self.assertIsNone(cfg["unlock_holdout"])


if __name__ == "__main__":
    unittest.main()
