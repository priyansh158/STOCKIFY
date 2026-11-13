"""M6.9 (IR 6.2): a run's P&L by month, weekday, expiry day, VIX level and opening gap."""
from __future__ import annotations

import json
import random
import unittest
from datetime import date, timedelta
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import paths, timeutil
from stockify.data import calendar, refdata, ticks
from stockify.research import bootstrap, plain, regimes, report
from tests.stockify.research.fixtures import make_run, weekdays

CAPITAL = 100_000_000
START = "2026-01-28"                                   # Wed; six days run to Wed 2026-02-04
NAVS6 = [CAPITAL + 1000, CAPITAL + 600, CAPITAL + 900, CAPITAL + 1400, CAPITAL + 1200, CAPITAL + 1300]
PNL6 = [1000, -400, 300, 500, -200, 100]
FAM = "strat/ds-1m-RELIANCE/sharpe"


def navs_from(pnls: list[int], capital: int = CAPITAL) -> list[int]:
    out, nav = [], capital
    for p in pnls:
        nav += p
        out.append(nav)
    return out


class FakeDay:
    """A recorded day: only the question `vix_close` asks of it."""

    def __init__(self, ltps: list[int], at: str = "15:29"):
        self.ltps, self.asked = ltps, []
        self.recv_ns = timeutil.ist_ns(date(2026, 1, 27), at)          # when the last VIX print arrived

    def packets(self, columns=None, instrument_ids=None, kinds=()):
        self.asked.append((tuple(columns or ()), set(instrument_ids or ()), tuple(kinds)))
        for i, v in enumerate(self.ltps):
            yield {"seq": i, "ltp": v, "recv_ns": self.recv_ns, "instrument_id": next(iter(instrument_ids or {0}))}

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class RegimeTest(SandboxedDBTest):
    db_modules = (store, refdata)

    def sql(self, text: str, args=()):
        conn = refdata._connect()
        conn.execute(text, args)

    def index(self, symbol: str, kind: str = "INDEX") -> int:
        conn = refdata._connect()
        conn.execute("INSERT INTO instruments (exchange, kind, tradingsymbol, first_seen, last_seen, source) "
                     "VALUES ('NSE', ?, ?, '2020-01-01', '2026-12-31', 'kite_dump')", (kind, symbol))
        return conn.execute("SELECT instrument_id FROM instruments WHERE tradingsymbol = ?", (symbol,)).fetchone()[0]

    def eod(self, iid: int, day: str, open_: int | None, close: int | None) -> None:
        self.sql("INSERT OR REPLACE INTO index_eod (trade_date, instrument_id, open_paise, high_paise, low_paise, "
                 "close_paise) VALUES (?, ?, ?, ?, ?, ?)", (day, iid, open_, open_, open_, close))

    def future(self, expiry: str, actual: str | None = None, underlying: str = "NIFTY") -> None:
        self.sql("INSERT INTO instruments (exchange, kind, tradingsymbol, underlying, expiry, actual_expiry, "
                 "first_seen, last_seen, source) VALUES ('NFO', 'FUT', ?, ?, ?, ?, '2025-01-01', '2026-12-31', "
                 "'kite_dump')", (f"{underlying}{expiry}", underlying, expiry, actual))

    def underlying(self, key: str = "NIFTY", symbol: str = "NSE:NIFTY 50") -> None:
        self.sql("INSERT OR IGNORE INTO index_underlyings (key, index_symbol, effective_from) VALUES (?, ?, "
                 "'2020-01-01')", (key, symbol))

    def mk(self, pnls: list[int], start: str = START, rid: str = "r1", **kw) -> str:
        make_run(rid, FAM, navs=navs_from(pnls), capital_paise=CAPITAL, start=start, **kw)
        return rid

    @staticmethod
    def by_label(buckets):
        return {b.label: b for b in buckets}


class TotalsTests(RegimeTest):
    def test_weekday_buckets_sum_to_the_run(self):
        rep = regimes.report(self.mk(PNL6))
        for dim in ("month", "weekday", "expiry", "vix", "gap"):
            with self.subTest(dim=dim):
                bs = getattr(rep, dim)
                self.assertEqual(sum(b.days for b in bs), 6)
                self.assertEqual(sum(b.pnl_paise for b in bs), NAVS6[-1] - CAPITAL)
        wk = self.by_label(rep.weekday)
        self.assertEqual([b.label for b in rep.weekday], ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
        self.assertEqual((wk["Wednesday"].days, wk["Wednesday"].pnl_paise), (2, 1100))

    def test_month_buckets_hand_checked(self):
        rep = regimes.report(self.mk(PNL6))
        self.assertEqual([b.label for b in rep.month], ["2026-01", "2026-02"])
        jan, feb = rep.month
        self.assertEqual((jan.days, jan.pnl_paise, jan.mean_pnl_paise, jan.up_days), (3, 900, 300, 2))
        self.assertAlmostEqual(jan.up_share, 2 / 3)
        self.assertEqual((feb.days, feb.pnl_paise, feb.mean_pnl_paise, feb.up_days), (3, 400, 133, 2))
        self.assertTrue(jan.thin and feb.thin)

    def test_mean_rounds_half_away_from_zero_with_integers(self):
        self.assertEqual(regimes._mean_paise(5, 3), 2)
        self.assertEqual(regimes._mean_paise(-5, 3), -2)
        self.assertEqual(regimes._mean_paise(1, 2), 1)
        self.assertEqual(regimes._mean_paise(-1, 2), -1)
        self.assertEqual(regimes._mean_paise(0, 4), 0)
        self.assertEqual(regimes._mean_paise(10**18 + 1, 2), 5 * 10**17 + 1)     # no float rounding at any size
        self.assertIsInstance(regimes._mean_paise(7, 2), int)

    def test_run_with_one_day_has_one_bucket_each(self):
        rep = regimes.report(self.mk([500], start="2026-01-05"))
        for dim in ("month", "weekday", "expiry", "vix", "gap"):
            self.assertEqual(len(getattr(rep, dim)), 1, dim)
        only = rep.weekday[0]
        self.assertEqual((only.label, only.days, only.pnl_paise, only.mean_pnl_paise, only.up_share),
                         ("Monday", 1, 500, 500, 1.0))
        self.assertEqual(rep.vix[0].label, "no data")

    def test_a_run_without_days_has_empty_buckets_and_a_note(self):
        make_run("empty", FAM, navs=[], capital_paise=CAPITAL)
        rep = regimes.report("empty")
        self.assertEqual((rep.month, rep.weekday, rep.expiry, rep.vix, rep.gap), ([], [], [], [], []))
        self.assertTrue(rep.notes)

    def test_thin_buckets_are_flagged(self):
        nine = regimes.report(self.mk([1] * 9, start="2026-01-05"))
        ten = regimes.report(self.mk([1] * 10, start="2026-01-05", rid="r10"))
        self.assertTrue(nine.month[0].thin)                  # 9 days < 10
        self.assertFalse(ten.month[0].thin)                  # 10 days is not thin
        self.assertEqual(regimes._thin_days(), 10)

    def test_property_no_day_is_dropped_and_pnl_is_exact(self):
        """Seeded loop: random NAV series and random refdata; every dimension keeps every day and every paisa."""
        rng = random.Random(20261112)
        nifty, vix = self.index("NIFTY 50"), self.index("INDIA VIX")
        self.underlying()
        d0 = date(2025, 12, 1)
        for i in range(120):
            d = d0 + timedelta(days=i)
            if rng.random() < 0.7:
                self.eod(nifty, d.isoformat(), rng.randint(900_000, 1_100_000), rng.randint(900_000, 1_100_000))
            if rng.random() < 0.6:
                self.eod(vix, d.isoformat(), None, rng.randint(900, 3000))
        for exp in sorted({(d0 + timedelta(days=rng.randint(0, 119))).isoformat() for _ in range(8)}):
            self.future(exp, actual=exp if rng.random() < .5 else None)
        for trial in range(25):
            n = rng.randint(1, 60)
            start = weekdays((d0 + timedelta(days=rng.randint(0, 40))).isoformat(), 1)[0]
            pnls = [rng.randint(-5000, 5000) for _ in range(n)]
            make_run(f"p{trial}", FAM, navs=navs_from(pnls), capital_paise=CAPITAL, start=start)
            rep = regimes.report(f"p{trial}")
            for dim in ("month", "weekday", "expiry", "vix", "gap"):
                bs = getattr(rep, dim)
                self.assertEqual(sum(b.days for b in bs), n, (trial, dim))
                self.assertEqual(sum(b.pnl_paise for b in bs), sum(pnls), (trial, dim))
                self.assertEqual(sum(b.up_days for b in bs), sum(1 for p in pnls if p > 0), (trial, dim))
                for b in bs:
                    self.assertGreater(b.days, 0)
                    self.assertEqual(b.thin, b.days < 10)
                    self.assertEqual(b.mean_pnl_paise, regimes._mean_paise(b.pnl_paise, b.days))


class ExpiryTests(RegimeTest):
    def test_expiry_vs_other_days(self):
        self.underlying()
        self.future("2026-02-03")
        self.future("2026-01-30", actual="2026-01-29")       # expired a day early (D-41): the actual date counts
        self.assertEqual(regimes.expiry_days(date(2026, 1, 28), date(2026, 2, 4)),
                         {date(2026, 1, 29), date(2026, 2, 3)})
        rep = regimes.report(self.mk(PNL6))
        b = self.by_label(rep.expiry)
        self.assertEqual((b["expiry day"].days, b["expiry day"].pnl_paise, b["expiry day"].mean_pnl_paise),
                         (2, -600, -300))
        self.assertEqual((b["expiry day"].up_days, b["expiry day"].up_share), (0, 0.0))
        self.assertEqual((b["other days"].days, b["other days"].pnl_paise, b["other days"].mean_pnl_paise),
                         (4, 1900, 475))

    def test_the_calendar_rules_fill_in_where_refdata_has_no_expiries(self):
        self.underlying()
        got = regimes.expiry_days(date(2026, 1, 1), date(2026, 3, 31))
        want = {e["date"] for e in calendar.generated_expiries("NIFTY", date(2026, 1, 1), date(2026, 3, 31))}
        self.assertTrue(want)
        self.assertEqual(got, want)
        rep = regimes.report(self.mk(PNL6))
        self.assertTrue(any("calendar" in n for n in rep.notes))

    def test_refdata_expiries_win_inside_its_span_and_nothing_is_double_counted(self):
        self.underlying()
        self.future("2026-01-27")
        self.future("2026-02-03", actual="2026-02-02")        # moved a day early: 02-03 must not come back
        got = regimes.expiry_days(date(2026, 1, 27), date(2026, 2, 10))
        self.assertIn(date(2026, 2, 2), got)
        self.assertNotIn(date(2026, 2, 3), got)
        self.assertEqual(got, {date(2026, 1, 27), date(2026, 2, 2), date(2026, 2, 10)})   # 02-10 is past the span

    def test_refdata_covering_only_the_last_weeks_still_marks_the_early_expiry_days(self):
        self.underlying()
        self.future("2026-01-27")
        self.future("2026-02-03")
        lo, hi = date(2026, 1, 5), date(2026, 2, 4)
        early = {e["date"] for e in calendar.generated_expiries("NIFTY", lo, date(2026, 1, 26))}
        self.assertTrue(early)
        self.assertEqual(regimes.expiry_days(lo, hi), early | {date(2026, 1, 27), date(2026, 2, 3)})
        rep = regimes.report(self.mk([1] * 22, start="2026-01-05"))
        self.assertEqual(sum(b.days for b in rep.expiry), 22)
        self.assertEqual(self.by_label(rep.expiry)["expiry day"].days, len(early) + 2)
        self.assertTrue(any("2026-01-05 to 2026-01-26" in n and "calendar" in n for n in rep.notes), rep.notes)


class VixTests(RegimeTest):
    def setUp(self):
        super().setUp()
        self.vix = self.index("INDIA VIX")
        # the previous trading day's close lands on each run day (run days 01-28 .. 02-04)
        closes = {"2026-01-27": 1250, "2026-01-28": 1450, "2026-01-29": 1800, "2026-01-30": 2100,
                  "2026-02-02": 1300, "2026-02-04": 2500}        # nothing on 02-03: 02-04 has no previous close
        for d, v in closes.items():
            self.eod(self.vix, d, None, v)

    def test_vix_bucket_uses_the_previous_close(self):
        rep = regimes.report(self.mk(PNL6))
        self.assertEqual([b.label for b in rep.vix],
                         ["VIX below 13", "VIX 13–16", "VIX 16–20", "VIX 20 or more", "no data"])
        got = {b.label: (b.days, b.pnl_paise) for b in rep.vix}
        self.assertEqual(got["VIX below 13"], (1, 1000))             # 01-28 uses 01-27's 12.50
        self.assertEqual(got["VIX 13–16"], (2, -600))                # 01-29 (14.50) and 02-03 (13.00 exactly: edge goes up)
        self.assertEqual(got["VIX 16–20"], (1, 300))
        self.assertEqual(got["VIX 20 or more"], (1, 500))
        self.assertEqual(got["no data"], (1, 100))                   # 02-04's own 25.00 is not used

    def test_missing_vix_goes_to_no_data_not_dropped(self):
        self.sql("DELETE FROM index_eod")
        rep = regimes.report(self.mk(PNL6))
        self.assertEqual([(b.label, b.days, b.pnl_paise) for b in rep.vix], [("no data", 6, 1300)])

    def test_vix_prefers_recorded_and_labels_the_source(self):
        fake = FakeDay([1111, 1222, 1333])
        with mock.patch.object(ticks, "open_day", return_value=fake):
            self.assertEqual(regimes.vix_close(date(2026, 1, 27)), (1333, "recorded"))
        self.assertEqual(fake.asked[0][1], {self.vix})               # only the VIX instrument is read
        self.assertEqual(fake.asked[0][2], ("index",))
        with mock.patch.object(ticks, "open_day", return_value=fake):
            rep = regimes.report(self.mk(PNL6))
        self.assertEqual(set(rep.vix_source.values()), {"recorded"})
        self.assertIn("2026-01-27", rep.vix_source)

    def test_a_partial_recorded_day_does_not_set_the_vix(self):
        partial = FakeDay([1111, 1333], at="11:00")                    # the recorder stopped mid-session
        with mock.patch.object(ticks, "open_day", return_value=partial):
            self.assertEqual(regimes.vix_close(date(2026, 1, 27)), (1250, "bhavcopy"))
            self.sql("INSERT INTO tick_days (trade_date, status) VALUES ('2026-01-27', 'recording')")
        with mock.patch.object(ticks, "open_day", return_value=FakeDay([1333], at="15:29")):
            self.assertEqual(regimes.vix_close(date(2026, 1, 27)), (1250, "bhavcopy"))    # status says not finished
        self.sql("UPDATE tick_days SET status = 'verified'")
        with mock.patch.object(ticks, "open_day", return_value=partial):
            self.assertEqual(regimes.vix_close(date(2026, 1, 27)), (1333, "recorded"))    # the status decides
        self.sql("DELETE FROM tick_days")

    def test_a_complete_recorded_day_without_a_status_row_sets_the_vix_from_15_25(self):
        for at, want in (("15:24", "bhavcopy"), ("15:25", "recorded"), ("15:31", "recorded")):
            with self.subTest(at=at), mock.patch.object(ticks, "open_day", return_value=FakeDay([1333], at=at)):
                self.assertEqual(regimes.vix_close(date(2026, 1, 27))[1], want)

    def test_the_close_comes_from_the_dated_session_rules_and_the_slack_from_the_setting(self):
        day = date(2026, 1, 27)
        early = timeutil.ist_ns(day, "15:00")                          # a (pretend) session that ends at 15:00
        with mock.patch.object(regimes.calendar, "session_close_ns", return_value=early):
            for at, want in (("14:54", "bhavcopy"), ("14:55", "recorded")):
                with self.subTest(at=at), mock.patch.object(ticks, "open_day", return_value=FakeDay([1333], at=at)):
                    self.assertEqual(regimes.vix_close(day)[1], want)
            cfg = {**regimes.settings(), "regimes": {**regimes.settings()["regimes"], "close_slack_min": 30}}
            with mock.patch.object(regimes, "settings", return_value=cfg),                     mock.patch.object(ticks, "open_day", return_value=FakeDay([1333], at="14:30")):
                self.assertEqual(regimes.vix_close(day)[1], "recorded")
        self.assertFalse(hasattr(regimes, "CLOSE_FROM_IST"))             # no clock time hard-coded in the module

    def test_a_day_the_calendar_has_no_session_rule_for_is_not_complete(self):
        with mock.patch.object(regimes.calendar, "session_close_ns", side_effect=regimes.calendar.CalendarError("x")),                 mock.patch.object(ticks, "open_day", return_value=FakeDay([1333], at="15:31")):
            self.assertEqual(regimes.vix_close(date(2026, 1, 27))[1], "bhavcopy")

    def test_vix_falls_back_to_bhavcopy(self):
        with mock.patch.object(ticks, "open_day", return_value=None):
            self.assertEqual(regimes.vix_close(date(2026, 1, 27)), (1250, "bhavcopy"))
            self.assertIsNone(regimes.vix_close(date(2026, 2, 3)))
        with mock.patch.object(ticks, "open_day", return_value=FakeDay([])):   # recorded, but no VIX packet
            self.assertEqual(regimes.vix_close(date(2026, 1, 27)), (1250, "bhavcopy"))
            rep = regimes.report(self.mk(PNL6))
        self.assertEqual(set(rep.vix_source.values()), {"bhavcopy"})


class GapTests(RegimeTest):
    def test_gap_buckets_hand_checked(self):
        nifty = self.index("NIFTY 50")
        gaps_bp = [-70, -30, 5, 30, 50, 70, None]                     # percent x 100; the last day has no row
        days = weekdays("2026-01-05", 7)
        self.eod(nifty, "2026-01-02", 1_000_000, 1_000_000)           # the Friday before
        for d, g in zip(days, gaps_bp):
            if g is not None:
                self.eod(nifty, d, 1_000_000 + 1_000_000 * g // 10_000, 1_000_000)
        self.assertEqual(regimes.opening_gap(date(2026, 1, 5)), -70)
        self.assertEqual(regimes.opening_gap(date(2026, 1, 8)), 30)
        self.assertIsNone(regimes.opening_gap(date(2026, 1, 13)))
        rep = regimes.report(self.mk([10, 20, 30, 40, 50, 60, 70], start="2026-01-05"))
        self.assertEqual([(b.label, b.days, b.pnl_paise) for b in rep.gap],
                         [("gap down more than 0.5%", 1, 10), ("gap down 0.1% to 0.5%", 1, 20),
                          ("flat (within 0.1%)", 1, 30), ("gap up 0.1% to 0.5%", 1, 40),
                          ("gap up 0.5% or more", 2, 110), ("no data", 1, 70)])

    def test_gap_edge_cases(self):
        nifty = self.index("NIFTY 50")
        self.eod(nifty, "2026-01-02", 1_000_000, 1_000_000)
        self.eod(nifty, "2026-01-05", 995_000, 1_000_000)             # exactly -0.5%: belongs to [-0.5, -0.1)
        self.eod(nifty, "2026-01-06", 1_001_000, 1_000_000)           # exactly +0.1%: belongs to [0.1, 0.5)
        rep = regimes.report(self.mk([1, 2], start="2026-01-05"))
        self.assertEqual([b.label for b in rep.gap], ["gap down 0.1% to 0.5%", "gap up 0.1% to 0.5%"])


class EmptyRefdataTests(RegimeTest):
    def test_empty_refdata_gives_no_data_buckets_and_a_note(self):
        rep = regimes.report(self.mk(PNL6))
        self.assertEqual([(b.label, b.days) for b in rep.vix], [("no data", 6)])
        self.assertEqual([(b.label, b.days) for b in rep.gap], [("no data", 6)])
        self.assertEqual(rep.vix_source, {})
        self.assertTrue(any("refdata" in n for n in rep.notes))
        self.assertEqual(sum(b.days for b in rep.month), 6)


class SentenceTests(unittest.TestCase):
    def test_regime_sentence_plain_and_thin(self):
        b = regimes.Bucket("Monday", 41, 41 * 31_200, 31_200, 23, 23 / 41, False)
        self.assertEqual(plain.regime_sentence(b), "Mondays: 41 days, average +₹312 a day, up on 56% of them")
        thin = regimes.Bucket("VIX 16–20", 4, -4000, -1000, 1, 0.25, True)
        s = plain.regime_sentence(thin)
        self.assertIn("only 4 days, take with care", s)
        self.assertIn("-₹10", s)
        one = regimes.Bucket("expiry day", 1, 500, 500, 1, 1.0, True)
        self.assertIn("only 1 day,", plain.regime_sentence(one))
        flat = regimes.Bucket("2026-01", 12, 0, 0, 0, 0.0, False)
        self.assertIn("up on 0% of them", plain.regime_sentence(flat))
        none = regimes.Bucket("no data", 0, 0, None, 0, None, True)
        self.assertNotIn("None", plain.regime_sentence(none))
        for bucket in (b, thin, one, flat, none):
            text = plain.regime_sentence(bucket)
            self.assertNotIn("\n", text)
            self.assertNotIn("significan", text)


class CacheTests(RegimeTest):
    def setUp(self):
        super().setUp()
        self.nifty = self.index("NIFTY 50")
        self.eod(self.nifty, "2026-01-27", 1_000_000, 1_000_000)
        self.eod(self.nifty, "2026-01-28", 1_002_000, 1_000_000)
        make_run("a", FAM, navs=navs_from(PNL6), capital_paise=CAPITAL, start=START, with_folder=True, seed=3,
                 daily_returns=None)
        self.path = paths.run_path("a") / "research.json"

    def counted(self):
        return (mock.patch.object(report.regimes, "build", side_effect=regimes.build),
                mock.patch.object(report.bootstrap, "bootstrap_ci", side_effect=bootstrap.bootstrap_ci))

    def test_regimes_are_filled_and_cached_apart_from_the_bootstrap(self):
        b, bs = self.counted()
        with b as built, bs as boot:
            first = report.run_research("a")
            second = report.run_research("a")
        self.assertEqual((built.call_count, boot.call_count), (1, 1))
        self.assertEqual(first, second)
        self.assertIsInstance(first.regimes, regimes.RegimeReport)
        self.assertEqual(sum(x.days for x in first.regimes.month), 6)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"key", "bootstrap", "regimes"})
        self.assertEqual(set(data["regimes"]), {"key", "value"})

    def test_regime_cache_is_invalidated_when_bhavcopy_arrives_late(self):
        b, bs = self.counted()
        with b as built, bs as boot:
            first = report.run_research("a")
            before = regimes.refdata_digest(date(2026, 1, 28), date(2026, 2, 4))
            self.eod(self.nifty, "2026-01-29", 1_000_000, 1_000_000)      # a late bhavcopy row
            self.assertNotEqual(before, regimes.refdata_digest(date(2026, 1, 28), date(2026, 2, 4)))
            second = report.run_research("a")
        self.assertEqual(built.call_count, 2)
        self.assertEqual(boot.call_count, 1)                              # the bootstrap is untouched
        self.assertNotEqual(first.regimes.gap, second.regimes.gap)
        self.assertEqual(first.bootstrap, second.bootstrap)

    def test_an_old_cache_with_only_the_bootstrap_stays_valid_for_the_bootstrap(self):
        b, bs = self.counted()
        with b, bs as boot:
            report.run_research("a")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            del data["regimes"]
            self.path.write_text(json.dumps(data), encoding="utf-8")
        with b as built, bs as boot2:
            res = report.run_research("a")
        self.assertEqual((built.call_count, boot2.call_count), (1, 0))
        self.assertIsNotNone(res.regimes)
        self.assertIn("regimes", json.loads(self.path.read_text(encoding="utf-8")))

    def test_a_bad_regime_entry_is_a_miss_and_never_hurts_the_bootstrap(self):
        report.run_research("a")
        for junk in ({"key": "x"}, {"key": "x", "value": []}, [], "no"):
            data = json.loads(self.path.read_text(encoding="utf-8"))
            data["regimes"] = junk
            self.path.write_text(json.dumps(data), encoding="utf-8")
            b, bs = self.counted()
            with b as built, bs as boot:
                res = report.run_research("a")
            self.assertEqual((built.call_count, boot.call_count), (1, 0), junk)
            self.assertIsNotNone(res.regimes)
        data = json.loads(self.path.read_text(encoding="utf-8"))          # a right key with a crafted value
        data["regimes"]["value"]["month"] = "<script>"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        b, bs = self.counted()
        with b as built, bs:
            res = report.run_research("a")
        self.assertEqual(built.call_count, 1)
        self.assertIsInstance(res.regimes.month, list)

    def test_use_cache_false_neither_reads_nor_writes_regimes(self):
        b, bs = self.counted()
        with b as built, bs:
            report.run_research("a", use_cache=False)
            report.run_research("a", use_cache=False)
        self.assertEqual(built.call_count, 2)
        self.assertFalse(self.path.exists())

    def test_the_cache_key_covers_the_calendar_data(self):
        key = regimes.cache_key(["2026-01-28"], [CAPITAL + 1], CAPITAL)
        with mock.patch.object(regimes, "calendar_digest", return_value="other"):
            self.assertNotEqual(key, regimes.cache_key(["2026-01-28"], [CAPITAL + 1], CAPITAL))
        self.assertEqual(len(regimes.calendar_digest()), 64)

    def test_the_digest_covers_the_range_and_the_tick_days(self):
        lo, hi = date(2026, 1, 28), date(2026, 2, 4)
        base = regimes.refdata_digest(lo, hi)
        self.assertEqual(base, regimes.refdata_digest(lo, hi))
        self.eod(self.nifty, "2026-03-01", 1, 1)                          # outside the range: no change
        self.assertEqual(base, regimes.refdata_digest(lo, hi))
        self.sql("INSERT INTO tick_days (trade_date, status, digest) VALUES ('2026-02-02', 'verified', 'abc')")
        withdays = regimes.refdata_digest(lo, hi)
        self.assertNotEqual(base, withdays)
        self.sql("UPDATE tick_days SET digest = 'abd'")
        self.assertNotEqual(withdays, regimes.refdata_digest(lo, hi))
        self.underlying()
        self.future("2026-02-03")
        self.assertNotEqual(withdays, regimes.refdata_digest(lo, hi))


if __name__ == "__main__":
    unittest.main()
