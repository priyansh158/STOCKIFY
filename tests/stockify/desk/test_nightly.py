"""HD 8.1: the nightly desk job. Each Kite holding's daily bars, grades and closing view, after the close.

Offline: the holdings are a fake snapshot, `history.fetch` is recorded (the bars are seeded in the sandbox's bar
cache), and the grading backtests are a fake that writes evidence rows as `evidence.run_for` does."""
from __future__ import annotations

import json
import math
import unittest
from datetime import date, timedelta
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import portfolio_sync
from stockify import strategy as S
from stockify import timeutil
from stockify.daemon import scheduler
from stockify.data import history, instruments, refdata
from stockify.desk import evidence, nightly, replay, thesis
from stockify.desk import store as desk_store
from tests.stockify.test_bundle import HEADER

DAY = date(2026, 11, 16)                 # a Monday, after the close
GRADES = ("strong", "some", "weak", "none")

# symbol, token, isin, weekdays of daily bars ending on DAY
STOCKS = (
    ("LONGCO", 500001, "INE000A01011", 400),
    ("SHORTCO", 500002, "INE000B01011", 150),
    ("SGBMAR29", 500003, "IN0020230001", 400),
    ("MOVEDCO", 500004, "INE000C01011", 300),
    ("ALSOCO", 500005, "INE000D01011", 300),
)


def _weekdays_to(last: date, n: int) -> list[date]:
    out, d = [], last
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def install() -> dict[str, int]:
    """The STOCKS on NSE with their daily bars in the bar cache; {symbol: instrument_id}."""
    rows = "".join(f"{tok},1,{sym},{sym},0,,0,0.05,1,EQ,NSE,NSE\n" for sym, tok, _isin, _n in STOCKS)
    instruments.sync(date(2020, 1, 1), {"NSE": f"{HEADER}\n{rows}"}, underlyings={})
    ids = {}
    for k, (sym, _tok, _isin, n) in enumerate(STOCKS):
        iid = instruments.find("NSE", sym)
        ids[sym] = iid
        p, prev = 100_000 + 10_000 * k, 100_000 + 10_000 * k
        with history._Tx() as conn:
            for i, d in enumerate(_weekdays_to(DAY, n)):
                p = max(40_000, p + int(900 * math.sin(i / 9.0) + 120 * math.sin(i / 2.3) + 40) // 5 * 5)
                conn.execute("INSERT INTO bars VALUES (?, 'day', ?, ?, ?, ?, ?, 10000000, NULL, '2026-01-01')",
                             (iid, timeutil.ist_ns(d, "00:00"), prev, max(prev, p) + 100, min(prev, p) - 100, p))
                prev = p
    return ids


def holding(sym: str, token: int, *, exchange: str = "NSE", isin: str = "", qty: int = 10):
    return portfolio_sync.Holding(tradingsymbol=sym, exchange=exchange, quantity=qty, average_price=1234.5,
                                  last_price=1300.0, pnl=655.0, day_change_pct=0.4, instrument_token=token, isin=isin)


def snapshot(holdings=(), error=None):
    return portfolio_sync.PortfolioSnapshot(holdings=list(holdings), fetched_at=1, error=error)


def the_holdings():
    by = {s[0]: s for s in STOCKS}
    return [
        holding("LONGCO", 500001, isin=by["LONGCO"][2]),
        holding("SHORTCO", 500002, isin=by["SHORTCO"][2]),
        holding("SGBMAR29", 500003, isin=by["SGBMAR29"][2]),
        holding("BSEONLY", 900001, exchange="BSE", isin="INE999Z01011"),
        holding("MOVEDCO", 123456, isin=by["MOVEDCO"][2]),           # a token the refdata does not know: by symbol
        holding("ALSOCO", 800005, exchange="BSE", isin=by["ALSOCO"][2]),   # bought on BSE, listed on NSE too
    ]


def fake_run_for(grades=None, calls=None):
    """`evidence.run_for` without the backtests: one row per voter, graded from `grades` (cycled), stored."""
    def run_for(instrument_id, symbol, today, *, voters=None):  # noqa: ANN001, ANN202
        if calls is not None:
            calls.append((instrument_id, symbol, today))
        end = evidence.evidence_end(today)
        bars = replay.daily_bars(instrument_id, until_ns=timeutil.ist_ns(end, "00:00"))
        first = timeutil.ns_to_ist(bars[0].start_ns).date().isoformat()
        last = timeutil.ns_to_ist(bars[-1].start_ns).date().isoformat()
        rows = []
        for k, (ref, extra) in enumerate(evidence.VOTERS):
            g = (grades or GRADES)[k % len(grades or GRADES)]
            rows.append({"trade_date": today.isoformat(), "instrument_id": instrument_id, "ref": ref,
                         "params": {"symbol": symbol, **extra}, "from_date": first, "to_date": last, "trades": 12,
                         "cagr": 0.1, "buy_hold_cagr": 0.08, "sharpe": 1.0, "buy_hold_sharpe": 0.8,
                         "max_drawdown": 0.2, "win_rate": 0.5, "windows": [], "windows_won": 1, "windows_total": 2,
                         "grade": g, "note": "Fake."})
        desk_store.put_evidence(rows)
        return rows
    return run_for


class NightlyBase(SandboxedDBTest):
    db_modules = (refdata, history, desk_store)

    def setUp(self):
        super().setUp()
        self.ids = install()
        self.fetches = []

        def fetch(iid, interval, from_day, to_day, **kw):  # noqa: ANN001, ANN003, ANN202
            self.fetches.append((iid, interval, from_day, to_day))
            return history.FetchStats(iid, interval)
        p = mock.patch.object(history, "fetch", side_effect=fetch)
        p.start()
        self.addCleanup(p.stop)
        hold = mock.patch.object(evidence, "evidence_end", side_effect=lambda today: min(
            today - timedelta(days=1), date(2026, 7, 31)))
        hold.start()
        self.addCleanup(hold.stop)

    def run_job(self, holdings=None, *, grades=None, calls=None, error=None, **kw):
        snap = snapshot(self.default_holdings() if holdings is None else holdings, error)
        with mock.patch.object(portfolio_sync, "fetch_snapshot", return_value=snap), \
                mock.patch.object(evidence, "run_for", side_effect=fake_run_for(grades, calls)):
            return nightly.run(DAY, client=object(), **kw)

    def default_holdings(self):
        return the_holdings()


class SkipTests(NightlyBase):
    def test_nightly_skips_non_nse_and_short_history_with_reasons(self):
        res = self.run_job()
        self.assertEqual(res["holdings"], 6)
        self.assertEqual(res["analysed"], 3)
        self.assertTrue(res["complete"])
        self.assertEqual(sorted(res["skipped"], key=lambda s: s["symbol"]), [
            {"symbol": "BSEONLY", "reason": "BSE only"},
            {"symbol": "SGBMAR29", "reason": "not an NSE stock (e.g. SGB)"},
            {"symbol": "SHORTCO", "reason": "less than a year of daily prices"},
        ])
        for sym in ("SHORTCO", "SGBMAR29"):
            self.assertIsNone(desk_store.latest_view(self.ids[sym]), sym)
            self.assertEqual(desk_store.latest_evidence(self.ids[sym]), [], sym)

    def test_six_years_of_daily_bars_are_fetched_first(self):
        self.run_job()
        fetched = {iid: (iv, a, b) for iid, iv, a, b in self.fetches}
        for sym in ("LONGCO", "SHORTCO", "MOVEDCO", "ALSOCO"):
            self.assertEqual(fetched[self.ids[sym]], ("day", date(2020, 11, 16), DAY), sym)
        self.assertNotIn(self.ids["SGBMAR29"], fetched)              # not a stock: nothing fetched

    def test_a_fetched_history_is_topped_up_from_its_last_bar(self):
        iid = self.ids["LONGCO"]
        with history._Tx() as conn:                                 # the 6-year range was fetched once before
            conn.execute("INSERT INTO bar_fetch_log VALUES (?, 'day', ?, ?, 0, 0, 'ok')",
                         (iid, timeutil.ist_ns(date(2020, 11, 16), "00:00"), timeutil.ist_ns(DAY, "00:00")))
        self.run_job([holding("LONGCO", 500001)])
        self.assertEqual(self.fetches, [(iid, "day", DAY, DAY)])

    def test_kite_trouble_with_one_holding_does_not_stop_the_others(self):
        def fetch(iid, interval, from_day, to_day, **kw):  # noqa: ANN001, ANN003, ANN202
            if iid == self.ids["LONGCO"]:
                raise RuntimeError("Kite said no")
            return history.FetchStats(iid, interval)
        with mock.patch.object(history, "fetch", side_effect=fetch):
            res = self.run_job()
        self.assertFalse(res["complete"])
        self.assertEqual([f["symbol"] for f in res["failed"]], ["LONGCO"])
        self.assertEqual(res["analysed"], 2)
        self.assertIsNotNone(desk_store.latest_view(self.ids["MOVEDCO"]))

    def test_a_holding_whose_lookup_raises_does_not_stop_the_others(self):
        real = nightly.resolve

        def resolve(h, day):  # noqa: ANN001, ANN202
            if h.tradingsymbol == "LONGCO":
                raise RuntimeError("refdata could not be read")
            return real(h, day)
        with mock.patch.object(nightly, "resolve", side_effect=resolve):
            res = self.run_job([holding("LONGCO", 500001), holding("MOVEDCO", 123456)])
        self.assertFalse(res["complete"])
        self.assertEqual([f["symbol"] for f in res["failed"]], ["LONGCO"])
        self.assertEqual(res["analysed"], 1)
        self.assertIsNotNone(desk_store.latest_view(self.ids["MOVEDCO"]))
        self.assertIsNone(desk_store.latest_view(self.ids["LONGCO"]))


class ViewTests(NightlyBase):
    def test_nightly_writes_a_view_per_holding(self):
        res = self.run_job()
        self.assertEqual(res["analysed"], 3)
        for sym in ("LONGCO", "MOVEDCO", "ALSOCO"):
            with self.subTest(sym=sym):
                iid = self.ids[sym]
                row = desk_store.latest_view(iid)
                self.assertEqual((row["trade_date"], row["symbol"]), (DAY.isoformat(), sym))
                v = row["view"]
                bars = replay.daily_bars(iid)
                self.assertEqual(v["close_paise"], bars[-1].close)
                self.assertEqual(v["close_date"], DAY.isoformat())
                atr = S.atr([(b.high, b.low, b.close) for b in bars], 14)
                self.assertEqual(v["atr_paise"], round(atr))
                ev = desk_store.latest_evidence(iid)
                self.assertEqual(v["evidence_to"], max(r["to_date"] for r in ev))
                self.assertEqual(v["evidence_to"], "2026-07-31")
                # The verdict is the thesis rule on the strategies' "now" at the day's close.
                grades = {r["ref"]: r["grade"] for r in ev}
                sigs = [replay.now(ref, {"symbol": f"NSE:{sym}", **extra}, bars) for ref, extra in replay.VOTERS]
                want = thesis.decide(sigs, grades, bars[-1].close, atr)
                self.assertEqual(row["verdict"], want.label)
                self.assertEqual(v["verdict"], want.label)
                self.assertEqual(v["exit_share"], want.exit_share)
                self.assertEqual(v["nearest_exit_paise"], want.nearest_exit_paise)
                self.assertEqual((v["reasons"], v["agree"], v["disagree"], v["counts"]),
                                 (want.reasons, want.for_, want.against, want.counts))
                self.assertEqual(len(v["signals"]), len(replay.VOTERS))
                for s, got in zip(sigs, v["signals"]):
                    self.assertEqual(got, {"ref": s.ref, "display_name": s.display_name,
                                           "in_position": s.in_position, "buying_today": s.buying_today,
                                           "exit_level_paise": s.exit_level_paise, "note": s.note,
                                           "grade": grades[s.ref]})

    def test_the_view_has_no_cost_or_pnl(self):
        self.run_job()
        views = [r["view"] for r in desk_store.latest_views()]
        self.assertEqual(len(views), 3)

        def keys(x):  # noqa: ANN001, ANN202
            if isinstance(x, dict):
                return set(x) | {k for v in x.values() for k in keys(v)}
            if isinstance(x, list):
                return {k for v in x for k in keys(v)}
            return set()
        for k in keys(views):
            for word in ("average", "avg", "pnl", "cost", "profit", "invested", "quantity", "qty"):
                self.assertNotIn(word, k.lower())
        text = json.dumps(views).lower()
        for price in ("1234.5", "1,234.50", "655", "1300"):             # the holding's cost, P&L and Kite price
            self.assertNotIn(price, text)

    def test_unchanged_bars_reuse_the_stored_grades(self):
        calls = []
        self.run_job(calls=calls)
        self.assertEqual(len(calls), 3)
        made = {iid: desk_store.latest_view(iid)["made_ns"] for iid in (self.ids["LONGCO"],)}
        calls.clear()
        res = self.run_job(calls=calls)                 # the same bars, the same evidence end: no new backtests
        self.assertEqual(calls, [])
        self.assertEqual(res["analysed"], 3)
        self.assertGreaterEqual(desk_store.latest_view(self.ids["LONGCO"])["made_ns"], made[self.ids["LONGCO"]])

    def test_a_new_bar_before_the_evidence_end_regrades(self):
        calls = []
        self.run_job(calls=calls)
        calls.clear()
        iid = self.ids["LONGCO"]
        with history._Tx() as conn:                    # a corrected history: an older first bar
            conn.execute("INSERT INTO bars VALUES (?, 'day', ?, 1, 2, 1, 1, 10, NULL, '2026-01-01')",
                         (iid, timeutil.ist_ns(date(2024, 1, 1), "00:00")))
        self.run_job(calls=calls)
        self.assertEqual([c[0] for c in calls], [iid])

    def test_a_split_refetch_regrades(self):
        calls = []
        self.run_job(calls=calls)
        calls.clear()

        def fetch(iid, interval, from_day, to_day, **kw):  # noqa: ANN001, ANN003, ANN202
            st = history.FetchStats(iid, interval)
            st.adjustment = "refetched" if iid == self.ids["MOVEDCO"] else None
            return st
        with mock.patch.object(history, "fetch", side_effect=fetch):
            self.run_job(calls=calls)
        self.assertEqual([c[0] for c in calls], [self.ids["MOVEDCO"]])


class StoppedRefetchTests(NightlyBase):
    def test_a_split_refetch_cut_short_by_the_deadline_regrades_on_the_next_pass(self):
        """Fix round 1 (6): a split found tonight, then the deadline inside the refill: the run stops; the next pass
        tops up with no new adjustment, but the grades are still remade (the adjustment row is newer than them)."""
        calls = []
        self.run_job(calls=calls)
        calls.clear()
        iid = self.ids["MOVEDCO"]
        seen = []

        def stop_after_split(i, interval, from_day, to_day, **kw):  # noqa: ANN001, ANN003, ANN202
            st = history.FetchStats(i, interval)
            if i == iid:
                with history._Tx() as conn:
                    conn.execute("INSERT OR REPLACE INTO adjustments_detected VALUES (?, ?, 2.0, 0, 'refetched')",
                                 (iid, DAY.isoformat()))
                st.adjustment, st.outcome = "refetched", "stopped"
            return st
        with mock.patch.object(history, "fetch", side_effect=stop_after_split):
            res = self.run_job([holding("MOVEDCO", 123456)], calls=calls, deadline_ns=10 ** 30)
        self.assertFalse(res["complete"])
        self.assertEqual(calls, [])
        self.assertEqual(res["refetched"], ["MOVEDCO"])                  # the signal survives the stop
        self.run_job([holding("MOVEDCO", 123456)], calls=calls)          # no adjustment tonight: still regraded
        self.assertEqual([c[0] for c in calls], [iid])
        calls.clear()
        self.run_job([holding("LONGCO", 500001)], calls=calls)           # an unadjusted stock keeps its grades
        self.assertEqual(calls, [])


class LoginAndDeadlineTests(NightlyBase):
    def setUp(self):
        super().setUp()
        import logging
        was = logging.root.manager.disable                                       # the runner switches logging off
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, was)

    def test_not_logged_in_is_retried_not_marked(self):
        from stockify import daemon
        state = scheduler.State()
        res = self.run_job(error="Kite not authenticated")
        self.assertFalse(res["logged_in"])
        self.assertFalse(res["complete"])
        self.assertEqual(desk_store.latest_views(), [])
        with mock.patch.object(portfolio_sync, "fetch_snapshot", return_value=snapshot(error="no token")),                 self.assertLogs("stockify", "INFO") as logs:
            daemon._run_job("desk", DAY, state, timeutil.ist_ns(DAY, "20:10"))
        self.assertEqual(len(logs.output), 1, logs.output)                     # one line, then tried again later
        self.assertIn("no holdings from Kite", logs.output[0])
        self.assertFalse(state.done("desk", DAY))
        self.assertEqual(self.fetches, [])                                     # nothing asked of Kite

    def test_a_complete_run_is_marked(self):
        from stockify import daemon
        state = scheduler.State()
        done = {"holdings": 1, "analysed": 1, "skipped": [], "failed": [], "complete": True, "logged_in": True}
        with mock.patch.object(nightly, "run", return_value=done):
            daemon._run_job("desk", DAY, state, timeutil.ist_ns(DAY, "20:10"))
        self.assertTrue(state.done("desk", DAY))

    def test_failures_are_retried_until_the_window_ends_then_given_up(self):
        from stockify import daemon
        state = scheduler.State()
        part = {"holdings": 2, "analysed": 1, "skipped": [], "failed": [{"symbol": "X", "error": "boom"}],
                "complete": False, "logged_in": True}
        with mock.patch.object(nightly, "run", return_value=part):
            daemon._run_job("desk", DAY, state, timeutil.ist_ns(DAY, "21:00"))
            self.assertFalse(state.done("desk", DAY))
            with self.assertLogs("stockify.daemon", "WARNING") as logs:
                daemon._run_job("desk", DAY, state, timeutil.ist_ns(DAY, "23:30"))
        self.assertTrue(state.done("desk", DAY))
        self.assertIn("giving up", logs.output[-1])

    def test_a_raised_error_never_escapes_and_is_retried(self):
        from stockify import daemon
        state = scheduler.State()
        with mock.patch.object(nightly, "run", side_effect=RuntimeError("boom")):
            with self.assertLogs("stockify.daemon", "ERROR"):
                daemon._run_job("desk", DAY, state, timeutil.ist_ns(DAY, "23:45"))
        self.assertFalse(state.done("desk", DAY))

    def test_the_job_stops_before_the_next_session(self):
        from stockify import daemon
        seen = {}

        def run(day, **kw):  # noqa: ANN001, ANN003, ANN202
            seen.update(kw)
            return {"holdings": 0, "analysed": 0, "skipped": [], "failed": [], "complete": True, "logged_in": True}
        with mock.patch.object(nightly, "run", side_effect=run):
            daemon._run_job("desk", DAY, scheduler.State(), timeutil.ist_ns(DAY, "20:10"))
        nxt = scheduler.plan(scheduler.session_day(timeutil.ist_ns(DAY, "20:10")))
        prep = min(nxt.refdata_ns, nxt.day_bundles_ns)            # the next session's preparation (08:30 IST)
        self.assertEqual(seen["deadline_ns"], prep - daemon.DESK_MARGIN_NS)
        self.assertEqual(daemon.DESK_MARGIN_NS, 10 * 60 * 1_000_000_000)

    def test_the_deadline_follows_the_schedule_not_a_fixed_time(self):
        from stockify import daemon
        from stockify.config import loader
        seen = {}
        cfg = loader.load_json("schedule.json")
        cfg = {**cfg, "jobs": {**cfg["jobs"], "refdata": "07:40", "day_bundles": "07:50"}}

        def run(day, **kw):  # noqa: ANN001, ANN003, ANN202
            seen.update(kw)
            return {"holdings": 0, "analysed": 0, "skipped": [], "failed": [], "complete": True, "logged_in": True}
        with mock.patch.object(scheduler, "schedule", return_value=cfg),                 mock.patch.object(nightly, "run", side_effect=run):
            daemon._run_job("desk", DAY, scheduler.State(), timeutil.ist_ns(DAY, "20:10"))
        nxt = scheduler.session_day(timeutil.ist_ns(DAY, "20:10"))
        self.assertEqual(seen["deadline_ns"], timeutil.ist_ns(nxt, "07:30"))

    def test_a_passed_deadline_leaves_the_rest_for_later(self):
        res = self.run_job(deadline_ns=1, now=lambda: 2)
        self.assertFalse(res["complete"])
        self.assertEqual(res["analysed"], 0)
        self.assertEqual(desk_store.latest_views(), [])

    def test_a_passed_deadline_asks_nothing_of_kite(self):
        """A pass after the deadline (say 08:40, before the session) returns at once: no holdings call, no bars."""
        with mock.patch.object(portfolio_sync, "fetch_snapshot") as snap:
            res = nightly.run(DAY, client=object(), deadline_ns=1, now=lambda: 2)
        snap.assert_not_called()
        self.assertEqual(self.fetches, [])
        self.assertFalse(res["complete"])
        self.assertTrue(res["logged_in"])

    def test_the_deadline_reaches_the_bar_top_up_and_a_stop_there_is_not_a_failure(self):
        """CW 1.2: the top-up gets the desk's deadline (checked between chunks); a stop inside it ends the run for
        later, with nothing graded on a part-fetched history and nothing listed as failed."""
        seen = []

        def fetch(iid, interval, from_day, to_day, **kw):  # noqa: ANN001, ANN003, ANN202
            seen.append(kw.get("deadline_ns"))
            st = history.FetchStats(iid, interval)
            st.outcome = "stopped"
            return st
        with mock.patch.object(history, "fetch", side_effect=fetch):
            res = self.run_job(deadline_ns=10 ** 30)
        self.assertEqual(seen, [10 ** 30])                         # the first holding's top-up, then the stop
        self.assertFalse(res["complete"])
        self.assertEqual((res["analysed"], res["failed"]), (0, []))
        self.assertEqual(desk_store.latest_views(), [])


if __name__ == "__main__":
    unittest.main()
