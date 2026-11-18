"""FS 4: point-in-time figures: what was known on a day, never later (usable from the trading day after the broadcast),
one basis per company, TTM from four filed quarters, shares from the filings, market cap from NSE's raw close."""
from __future__ import annotations

import hashlib
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import timeutil
from stockify.config import loader
from stockify.data import refdata
from stockify.fundamentals import filings, pit, store

SYM = "RELIANCE"
CR = 10_000_000 * 100                                  # ₹1 crore in paise
FV10 = 1000                                            # face value ₹10, paise per share
SHARES = 67_660_000


def ist(d: date, hhmm: str) -> int:
    return timeutil.ist_ns(d, hhmm)


class PitCase(SandboxedDBTest):
    db_modules = (store, refdata)

    def setUp(self):
        super().setUp()
        refdata._connect().execute(
            "INSERT INTO instruments (instrument_id, exchange, kind, tradingsymbol, first_seen, last_seen, source) "
            "VALUES (7, 'NSE', 'EQ', ?, '2021-01-01', '2026-11-17', 'kite_dump')", (SYM,))
        self.n = 0
        self.close(date(2021, 1, 4), 100000)                  # NSE closes on record from 2021 (backfilled)

    def put(self, period_end: str, fields: dict, *, broadcast_ns: int, kind: str = "Q", basis: str = "consolidated",
            symbol: str = SYM, status: str = "parsed", period_start: str | None = None) -> str:
        """A filing as filings.py lists it and xbrl.ingest_pending parses it: a filings row and its figures."""
        self.n += 1
        url = f"https://nsearchives.nseindia.in/corporate/xbrl/INDAS_{self.n}.xml"
        fid = filings.filing_id(symbol, period_end, kind, basis, broadcast_ns, url)
        row = {"filing_id": fid, "symbol": symbol, "period_start": period_start, "period_end": period_end,
               "period_kind": kind, "basis": basis, "broadcast_ns": broadcast_ns,
               "broadcast_source": "broadCastDate", "source": "results", "seq_number": str(self.n), "xbrl_url": url,
               "xbrl_sha256": hashlib.sha256(url.encode()).hexdigest(), "xbrl_file": None, "status": status,
               "reason": None}
        conn = store._connect()
        conn.execute(f"INSERT INTO filings ({', '.join(filings.COLUMNS)}) "
                     f"VALUES ({', '.join('?' * len(filings.COLUMNS))})", tuple(row[c] for c in filings.COLUMNS))
        if status == "parsed":
            conn.executemany("INSERT INTO figures (filing_id, field, value_paise, value_raw) VALUES (?, ?, ?, ?)",
                             [(fid, k, v, f"test {v}") for k, v in fields.items()])
        return fid

    def close(self, day: date, close_paise: int) -> None:
        refdata._connect().execute(
            "INSERT INTO cm_eod (trade_date, instrument_id, series, close_paise, prev_close_paise, volume, "
            "turnover_paise) VALUES (?, 7, 'EQ', ?, 0, 1, 1)", (day.isoformat(), close_paise))


def pnl(revenue_cr: int, profit_cr: int, *, shares: int = SHARES, face: int = FV10, eps: bool = True) -> dict:
    out = {"revenue_ops": revenue_cr * CR, "net_profit": profit_cr * CR, "profit_owners": profit_cr * CR,
           "paid_up_equity": shares * face, "face_value": face}
    if eps:
        out["eps_basic"] = round(profit_cr * CR / shares)
    return out


class UsableDayTests(PitCase):
    def test_not_usable_before_the_day_after_broadcast(self):
        # Thursday 14 Nov 2024, evening; Friday 15 Nov is a holiday (Gurpurb), then the weekend: usable Monday 18.
        thu = ist(date(2024, 11, 14), "21:00")
        self.assertEqual(pit.usable_day(thu), date(2024, 11, 18))
        fid = self.put("2024-09-30", pnl(100, 10), broadcast_ns=thu)
        for d in (date(2024, 11, 14), date(2024, 11, 15), date(2024, 11, 17)):
            self.assertEqual(pit.known_on(SYM, d)["quarters"], [], d)
        q = pit.known_on(SYM, date(2024, 11, 18))["quarters"]
        self.assertEqual([(e["period_end"], e["filing_id"], e["usable_day"]) for e in q],
                         [("2024-09-30", fid, "2024-11-18")])
        self.assertEqual(q[0]["fields"]["revenue_ops"], 100 * CR)

    def test_the_ist_date_decides_not_the_utc_one(self):
        # 01:30 IST on Friday 25 Oct 2024 is still Thursday in UTC: the IST date (Friday) counts, usable Monday 28.
        early = ist(date(2024, 10, 25), "01:30")
        self.assertEqual(timeutil.ns_to_ist(early).date(), date(2024, 10, 25))
        self.assertEqual(pit.usable_day(early), date(2024, 10, 28))
        # Broadcast before the open on a trading day: still the next trading day.
        self.assertEqual(pit.usable_day(ist(date(2024, 10, 28), "08:00")), date(2024, 10, 29))

    def test_midnight_ist(self):
        # 23:59 IST Thursday 24 Oct 2024 is usable Friday 25; two minutes later (00:01 IST Friday) Monday 28.
        self.assertEqual(pit.usable_day(ist(date(2024, 10, 24), "23:59")), date(2024, 10, 25))
        self.assertEqual(pit.usable_day(ist(date(2024, 10, 25), "00:01")), date(2024, 10, 28))

    def test_only_parsed_filings_count(self):
        self.put("2024-09-30", pnl(100, 10), broadcast_ns=ist(date(2024, 10, 14), "20:00"), status="held")
        self.assertEqual(pit.known_on(SYM, date(2025, 1, 1))["quarters"], [])


class VersionTests(PitCase):
    def test_restatement_replaces_from_its_own_day(self):
        v1 = self.put("2024-09-30", pnl(100, 10), broadcast_ns=ist(date(2024, 10, 14), "20:00"))
        v2 = self.put("2024-09-30", pnl(110, 12), broadcast_ns=ist(date(2025, 1, 16), "20:15"))
        on = lambda d: pit.known_on(SYM, d)["quarters"]  # noqa: E731
        self.assertEqual([e["filing_id"] for e in on(date(2024, 10, 15))], [v1])
        self.assertEqual([e["filing_id"] for e in on(date(2025, 1, 16))], [v1])     # broadcast day: not yet
        got = on(date(2025, 1, 17))
        self.assertEqual([e["filing_id"] for e in got], [v2])
        self.assertEqual(got[0]["fields"]["revenue_ops"], 110 * CR)
        # Looking back from later never changes what was known on the earlier day.
        self.assertEqual(on(date(2024, 12, 2))[0]["fields"]["revenue_ops"], 100 * CR)

    def test_identical_later_filing_is_the_same_version(self):
        v1 = self.put("2024-09-30", pnl(100, 10), broadcast_ns=ist(date(2024, 10, 14), "20:00"))
        self.put("2024-09-30", pnl(100, 10), broadcast_ns=ist(date(2024, 10, 20), "20:00"))
        self.assertEqual([e["filing_id"] for e in pit.known_on(SYM, date(2025, 1, 1))["quarters"]], [v1])

    def test_basis_is_point_in_time_and_never_mixed(self):
        # Ruling R8. Standalone quarters from 2022; consolidated first filed for the June 2025 quarter (broadcast
        # Friday 18 Jul 2025, usable Monday 21 Jul): standalone before that day, consolidated from it.
        stand = {}
        for end, b in (("2022-03-31", date(2022, 4, 22)), ("2022-06-30", date(2022, 7, 22)),
                       ("2022-09-30", date(2022, 10, 21)), ("2022-12-31", date(2023, 1, 20)),
                       ("2025-03-31", date(2025, 4, 25)), ("2025-06-30", date(2025, 7, 18))):
            stand[end] = self.put(end, {**pnl(10, 1), "total_equity": 500 * CR}, broadcast_ns=ist(b, "20:00"),
                                  basis="standalone")
        cons = {}
        for end, b in (("2025-06-30", date(2025, 7, 18)), ("2025-09-30", date(2025, 10, 17)),
                       ("2025-12-31", date(2026, 1, 16)), ("2026-03-31", date(2026, 4, 24))):
            cons[end] = self.put(end, pnl(20, 2, shares=2 * SHARES), broadcast_ns=ist(b, "20:30"))
        on = date(2023, 2, 1)
        self.assertEqual(pit.basis_on(SYM, on), "standalone")
        k = pit.known_on(SYM, on)
        self.assertEqual(k["basis"], "standalone")
        self.assertEqual([e["filing_id"] for e in k["quarters"]],
                         [stand[e] for e in ("2022-12-31", "2022-09-30", "2022-06-30", "2022-03-31")])
        self.assertEqual(pit.ttm(SYM, on, "revenue_ops"), 40 * CR)                 # four standalone quarters
        self.assertEqual(pit.shares(SYM, on), SHARES)
        self.assertEqual(pit.basis_on(SYM, date(2025, 7, 18)), "standalone")       # broadcast day: not yet
        self.assertEqual(pit.latest(SYM, date(2025, 7, 18), "total_equity"), 500 * CR)
        switched = date(2025, 7, 21)
        self.assertEqual(pit.basis_on(SYM, switched), "consolidated")
        k = pit.known_on(SYM, switched)
        self.assertEqual([e["filing_id"] for e in k["quarters"]], [cons["2025-06-30"]])   # no standalone mixed in
        self.assertIsNone(pit.ttm(SYM, switched, "revenue_ops"))                    # one consolidated quarter only
        self.assertIsNone(pit.latest(SYM, switched, "total_equity"))               # standalone balance not used
        self.assertEqual(pit.shares(SYM, switched), 2 * SHARES)
        self.assertIsNone(pit.ttm(SYM, date(2026, 4, 24), "revenue_ops"))          # March not usable yet
        self.assertEqual(pit.ttm(SYM, date(2026, 4, 27), "revenue_ops"), 80 * CR)  # four consolidated quarters
        # Looking back later never changes the earlier view.
        self.assertEqual(pit.ttm(SYM, on, "revenue_ops"), 40 * CR)

    def test_basis_without_consolidated_filings_and_a_held_one(self):
        self.put("2024-09-30", pnl(10, 1), broadcast_ns=ist(date(2024, 10, 14), "20:00"), basis="standalone",
                 symbol="VSTIND")
        self.assertEqual(pit.basis_on("VSTIND", date(2026, 1, 1)), "standalone")
        self.assertEqual(len(pit.known_on("VSTIND", date(2024, 11, 1))["quarters"]), 1)
        self.assertEqual(pit.basis_on("NOBODY", date(2026, 1, 1)), "standalone")
        # A held consolidated filing does not switch the basis (ruling R8 amended): standalone figures stay until a
        # parsed consolidated filing is usable.
        sa = self.put("2024-06-30", pnl(10, 1), broadcast_ns=ist(date(2024, 7, 19), "20:00"), basis="standalone",
                      symbol="ABB")
        self.put("2024-09-30", {}, broadcast_ns=ist(date(2024, 10, 14), "20:00"), symbol="ABB", status="held")
        self.assertEqual(pit.basis_on("ABB", date(2024, 10, 15)), "standalone")
        self.assertEqual([e["filing_id"] for e in pit.known_on("ABB", date(2024, 10, 15))["quarters"]], [sa])
        cons = self.put("2024-12-31", pnl(12, 1), broadcast_ns=ist(date(2025, 1, 16), "20:00"), symbol="ABB")
        self.assertEqual(pit.basis_on("ABB", date(2025, 1, 16)), "standalone")
        self.assertEqual(pit.basis_on("ABB", date(2025, 1, 17)), "consolidated")
        self.assertEqual([e["filing_id"] for e in pit.known_on("ABB", date(2025, 1, 17))["quarters"]], [cons])

    def test_kinds_are_kept_apart_and_quarters_capped_at_eight(self):
        d, ends = date(2021, 6, 30), []
        for n in range(10):
            end = pit.shift_quarter(d, n)
            ends.append(end)
            self.put(end.isoformat(), pnl(100 + n, 10), broadcast_ns=ist(end, "23:00") + 30 * 86400 * 10**9)
        a = self.put("2022-03-31", {**pnl(400, 40), "operating_cash_flow": 55 * CR,
                                    "total_equity": 1000 * CR, "total_assets": 2000 * CR},
                     broadcast_ns=ist(date(2022, 5, 6), "20:00"), kind="A", period_start="2021-04-01")
        k = pit.known_on(SYM, date(2026, 1, 1))
        self.assertEqual([e["period_end"] for e in k["quarters"]], [e.isoformat() for e in ends[::-1][:8]])
        self.assertEqual([(e["filing_id"], e["period_kind"]) for e in k["annual"]], [(a, "A")])
        self.assertEqual(k["annual"][0]["fields"]["operating_cash_flow"], 55 * CR)
        self.assertEqual(k["half_years"], [])


class TtmTests(PitCase):
    QUARTERS = (("2024-06-30", date(2024, 7, 19)), ("2024-09-30", date(2024, 10, 14)),
                ("2024-12-31", date(2025, 1, 16)), ("2025-03-31", date(2025, 4, 25)))

    def test_ttm_needs_four_consecutive_quarters(self):
        for n, (end, b) in enumerate(self.QUARTERS):
            self.put(end, pnl(100 + n, 10 + n), broadcast_ns=ist(b, "20:00"))
        self.assertEqual(pit.ttm(SYM, date(2025, 4, 28), "revenue_ops"), (100 + 101 + 102 + 103) * CR)
        self.assertEqual(pit.ttm(SYM, date(2025, 4, 28), "net_profit"), (10 + 11 + 12 + 13) * CR)
        self.assertIsNone(pit.ttm(SYM, date(2025, 4, 25), "revenue_ops"))     # March not usable yet: three only
        self.assertIsNone(pit.ttm(SYM, date(2025, 4, 28), "interest_earned"))  # a field not filed
        with self.assertRaises(ValueError):
            pit.ttm(SYM, date(2025, 4, 28), "total_equity")                    # a balance, not a flow

    def test_a_gap_gives_no_ttm(self):
        for n, (end, b) in enumerate(self.QUARTERS):
            if end != "2024-12-31":
                self.put(end, pnl(100, 10), broadcast_ns=ist(b, "20:00"))
        self.put("2024-03-31", pnl(100, 10), broadcast_ns=ist(date(2024, 4, 22), "20:00"))
        self.assertIsNone(pit.ttm(SYM, date(2025, 5, 1), "revenue_ops"))

    def test_half_year_minus_quarter_fills_a_missing_quarter_but_not_ttm(self):
        self.put("2024-03-31", pnl(90, 9), broadcast_ns=ist(date(2024, 4, 22), "20:00"))
        self.put("2023-12-31", pnl(90, 9), broadcast_ns=ist(date(2024, 1, 19), "20:00"))
        q1 = self.put("2024-06-30", pnl(100, 10), broadcast_ns=ist(date(2024, 7, 19), "20:00"))
        h = self.put("2024-09-30", {**pnl(230, 25), "total_equity": 900 * CR},
                     broadcast_ns=ist(date(2024, 10, 14), "20:00"), kind="H", period_start="2024-04-01")
        k = pit.known_on(SYM, date(2024, 11, 1))
        self.assertEqual([e["filing_id"] for e in k["half_years"]], [h])
        sep = k["quarters"][0]
        self.assertEqual((sep["period_end"], sep["derived_from"]), ("2024-09-30", [h, q1]))
        self.assertEqual(sep["fields"]["revenue_ops"], 130 * CR)
        self.assertEqual(sep["fields"]["profit_owners"], 15 * CR)
        self.assertEqual(sep["fields"]["total_equity"], 900 * CR)                # the balance sheet at the H's end
        self.assertNotIn("eps_basic", sep["fields"])                             # EPS does not subtract
        # Trailing figures need four filed quarters: no half-year substitutes (spec call 6).
        self.assertIsNone(pit.ttm(SYM, date(2024, 11, 1), "revenue_ops"))
        # A filed quarter always wins over the derivation.
        q2 = self.put("2024-09-30", pnl(131, 15), broadcast_ns=ist(date(2024, 10, 14), "20:01"))
        sep = pit.known_on(SYM, date(2024, 11, 1))["quarters"][0]
        self.assertEqual((sep["filing_id"], sep["derived_from"]), (q2, None))
        self.assertEqual(pit.ttm(SYM, date(2024, 11, 1), "revenue_ops"), (90 + 90 + 100 + 131) * CR)

    def test_latest_balance_comes_from_the_newest_quarter_that_has_it(self):
        self.put("2024-09-30", {**pnl(100, 10), "total_equity": 900 * CR, "borrowings_current": 5 * CR},
                 broadcast_ns=ist(date(2024, 10, 14), "20:00"))
        self.put("2024-12-31", pnl(100, 10), broadcast_ns=ist(date(2025, 1, 16), "20:00"))
        self.assertEqual(pit.latest(SYM, date(2025, 2, 1), "total_equity"), 900 * CR)
        self.assertEqual(pit.latest(SYM, date(2025, 2, 1), "borrowings_current"), 5 * CR)
        self.assertIsNone(pit.latest(SYM, date(2025, 2, 1), "borrowings_noncurrent"))
        self.put("2025-03-31", {**pnl(100, 10), "total_equity": 950 * CR},
                 broadcast_ns=ist(date(2025, 4, 25), "20:00"))
        self.assertEqual(pit.latest(SYM, date(2025, 4, 28), "total_equity"), 950 * CR)


class SharesTests(PitCase):
    def test_shares_from_paid_up_over_face_value_carried_forward(self):
        self.put("2024-06-30", pnl(100, 10), broadcast_ns=ist(date(2024, 7, 19), "20:00"))
        self.assertIsNone(pit.shares(SYM, date(2024, 7, 19)))
        self.assertEqual(pit.shares(SYM, date(2024, 7, 22)), SHARES)
        self.assertEqual(pit.shares(SYM, date(2024, 12, 31)), SHARES)          # carried forward
        # A later filing without paid-up data keeps the last count.
        self.put("2024-09-30", {"revenue_ops": 100 * CR, "net_profit": 10 * CR},
                 broadcast_ns=ist(date(2024, 10, 14), "20:00"))
        detail = pit.shares_detail(SYM, date(2024, 11, 1))
        self.assertEqual((detail["shares"], detail["period_end"], detail["source"]),
                         (SHARES, "2024-06-30", "paid_up"))

    def test_eps_implied_shares_only_without_paid_up(self):
        f = {"revenue_ops": 100 * CR, "net_profit": 10 * CR, "profit_owners": 10 * CR, "eps_basic": 148}
        self.put("2024-06-30", f, broadcast_ns=ist(date(2024, 7, 19), "20:00"))
        detail = pit.shares_detail(SYM, date(2024, 8, 1))
        self.assertEqual((detail["shares"], detail["source"]), (round(10 * CR / 148), "eps"))
        self.put("2024-03-31", pnl(100, 10), broadcast_ns=ist(date(2024, 4, 22), "20:00"))
        detail = pit.shares_detail(SYM, date(2024, 8, 1))
        self.assertEqual((detail["shares"], detail["source"], detail["period_end"]), (SHARES, "paid_up", "2024-03-31"))

    def test_paid_up_that_disagrees_with_eps_is_not_used(self):
        good = self.put("2024-03-31", pnl(100, 10), broadcast_ns=ist(date(2024, 4, 22), "20:00"))
        bad = {**pnl(100, 10), "paid_up_equity": SHARES * FV10 * 10}          # a scale slip: ten times the shares
        self.put("2024-06-30", bad, broadcast_ns=ist(date(2024, 7, 19), "20:00"))
        detail = pit.shares_detail(SYM, date(2024, 8, 1))
        self.assertEqual((detail["shares"], detail["filing_id"]), (SHARES, good))

    def test_split_keeps_market_cap_continuous(self):
        # Face value ₹10 → ₹5 (ex-date Monday 5 Aug 2024): the shares double, the close halves. The June quarter
        # (pre-split count) is the latest filing until the September quarter is usable on 15 Oct.
        self.put("2024-06-30", pnl(100, 10), broadcast_ns=ist(date(2024, 7, 19), "20:00"))
        self.close(date(2024, 6, 28), 301000)                                  # the close before each window
        self.close(date(2024, 8, 1), 301000)
        self.close(date(2024, 8, 2), 300000)
        self.close(date(2024, 8, 5), 150500)                                   # the split, and a small move
        self.close(date(2024, 8, 6), 151000)
        self.close(date(2024, 9, 30), 151000)
        self.close(date(2024, 10, 15), 152000)
        before = pit.market_cap(SYM, date(2024, 8, 2))
        self.assertEqual(before, SHARES * 300000)
        self.assertEqual(pit.shares(SYM, date(2024, 8, 5)), 2 * SHARES)
        self.assertEqual(pit.market_cap(SYM, date(2024, 8, 5)), 2 * SHARES * 150500)
        self.assertEqual(pit.market_cap(SYM, date(2024, 8, 6)), 2 * SHARES * 151000)
        self.assertLess(abs(pit.market_cap(SYM, date(2024, 8, 5)) - before), before // 100)
        detail = pit.shares_detail(SYM, date(2024, 8, 6))
        self.assertEqual(detail["adjustments"], [{"day": "2024-08-05", "ratio": "2"}])
        # The September filing carries the new face value: its own count, no adjustment any more.
        self.put("2024-09-30", pnl(100, 10, shares=2 * SHARES, face=500), broadcast_ns=ist(date(2024, 10, 14), "20:00"))
        detail = pit.shares_detail(SYM, date(2024, 10, 15))
        self.assertEqual((detail["shares"], detail["period_end"], detail["adjustments"]),
                         (2 * SHARES, "2024-09-30", []))
        self.assertEqual(pit.market_cap(SYM, date(2024, 10, 15)), 2 * SHARES * 152000)
        # Before the split nothing is adjusted (the price after it is not known yet).
        self.assertEqual(pit.shares(SYM, date(2024, 8, 2)), SHARES)

    def test_an_ordinary_fall_is_not_a_split(self):
        self.put("2024-06-30", pnl(100, 10), broadcast_ns=ist(date(2024, 7, 19), "20:00"))
        self.close(date(2024, 6, 28), 300000)
        self.close(date(2024, 8, 2), 300000)
        self.close(date(2024, 8, 5), 240000)                                   # 20 % down: a price move
        self.assertEqual(pit.shares(SYM, date(2024, 8, 5)), SHARES)
        self.assertEqual(pit.market_cap(SYM, date(2024, 8, 5)), SHARES * 240000)

    def test_a_fall_no_ratio_explains_holds_the_count_until_the_next_filing(self):
        # A third off in a day (a 1:2 bonus, or a crash like ZEEL's on 2024-01-23: the price cannot tell): no count.
        self.put("2024-06-30", pnl(100, 10), broadcast_ns=ist(date(2024, 7, 19), "20:00"))
        self.close(date(2024, 6, 28), 300000)
        self.close(date(2024, 8, 2), 300000)
        self.close(date(2024, 8, 5), 199000)
        self.close(date(2024, 9, 30), 200000)
        self.assertEqual(pit.market_cap(SYM, date(2024, 8, 2)), SHARES * 300000)
        self.assertIsNone(pit.shares(SYM, date(2024, 8, 5)))
        self.assertIsNone(pit.market_cap(SYM, date(2024, 8, 5)))
        detail = pit.shares_detail(SYM, date(2024, 8, 5))
        self.assertEqual((detail["shares"], detail["base_shares"], detail["held"]),
                         (None, SHARES, [{"day": "2024-08-05", "fall": "1.508"}]))
        # The September filing (period after the fall) gives the count again.
        self.put("2024-09-30", pnl(100, 10, shares=SHARES * 3 // 2), broadcast_ns=ist(date(2024, 10, 14), "20:00"))
        self.close(date(2024, 10, 15), 200000)
        self.assertEqual(pit.market_cap(SYM, date(2024, 10, 15)), SHARES * 3 // 2 * 200000)

    def test_a_split_across_a_missing_close_is_still_seen(self):
        # No close on Friday 2 Aug (the day before the ex-date): the 5 Aug close is compared with 1 Aug's.
        self.put("2024-06-30", pnl(100, 10), broadcast_ns=ist(date(2024, 7, 19), "20:00"))
        self.close(date(2024, 6, 28), 300000)
        self.close(date(2024, 8, 1), 300000)
        self.close(date(2024, 8, 5), 150500)
        detail = pit.shares_detail(SYM, date(2024, 8, 5))
        self.assertEqual((detail["shares"], detail["adjustments"], detail["held"]),
                         (2 * SHARES, [{"day": "2024-08-05", "ratio": "2"}], []))
        self.assertEqual(pit.market_cap(SYM, date(2024, 8, 5)), 2 * SHARES * 150500)

    def test_a_fall_across_a_long_gap_holds(self):
        # 1 Aug, then nothing for 8 trading days: a halving across that gap may be a split or a price move: held.
        self.put("2024-06-30", pnl(100, 10), broadcast_ns=ist(date(2024, 7, 19), "20:00"))
        self.close(date(2024, 6, 28), 300000)
        self.close(date(2024, 8, 1), 300000)
        self.close(date(2024, 8, 14), 150000)
        detail = pit.shares_detail(SYM, date(2024, 8, 14))
        self.assertEqual((detail["shares"], detail["adjustments"]), (None, []))
        self.assertEqual(detail["held"], [{"day": "2024-08-14", "fall": "2.000", "gap_days": 8}])
        self.assertIsNone(pit.market_cap(SYM, date(2024, 8, 14)))
        # The same gap with no fall is fine.
        refdata._connect().execute("UPDATE cm_eod SET close_paise = 299000 WHERE trade_date = '2024-08-14'")
        self.assertEqual(pit.market_cap(SYM, date(2024, 8, 14)), SHARES * 299000)

    def test_no_close_before_the_window_holds(self):
        self.put("2024-06-30", pnl(100, 10), broadcast_ns=ist(date(2024, 7, 19), "20:00"))
        self.close(date(2024, 8, 1), 300000)
        # Closes on record (from 2021) but none in the 5 trading days before 1 Jul: nothing to compare with.
        detail = pit.shares_detail(SYM, date(2024, 8, 1))
        self.assertIsNone(detail["shares"])
        self.assertEqual(detail["held"], [{"day": "2024-08-01",
                                           "reason": "no NSE close in the 5 trading days before 2024-07-01"}])
        self.assertIsNone(pit.market_cap(SYM, date(2024, 8, 1)))
        # Close coverage starting after the period end (cm_eod begins 2024-07-15): a split before it is invisible.
        refdata._connect().execute("DELETE FROM cm_eod WHERE trade_date < '2024-07-15'")
        self.close(date(2024, 7, 15), 300000)
        detail = pit.shares_detail(SYM, date(2024, 8, 1))
        self.assertEqual((detail["shares"], detail["held"]),
                         (None, [{"day": "2024-07-01", "reason": "no NSE close on record before 2024-07-15"}]))

    def test_ratios_come_from_config(self):
        cfg = loader.load_json("fundamentals.json")
        self.assertEqual(cfg["corporate_action_ratios"], ["2", "2.5", "3", "4", "5", "6", "10", "20"])
        self.assertEqual((cfg["corporate_action_tolerance"], cfg["corporate_action_hold_fall"]), ("0.04", "1.3"))


class MarketCapTests(PitCase):
    def test_close_on_the_day_else_the_latest_within_five_trading_days(self):
        self.put("2024-06-30", pnl(100, 10), broadcast_ns=ist(date(2024, 7, 19), "20:00"))
        self.close(date(2024, 6, 28), 300000)
        self.close(date(2024, 8, 1), 300000)
        self.assertEqual(pit.market_cap(SYM, date(2024, 8, 1)), SHARES * 300000)
        # 2, 5, 6, 7, 8 Aug have no row: the 1st is within five trading days of the 8th, not of the 9th.
        self.assertEqual(pit.market_cap(SYM, date(2024, 8, 8)), SHARES * 300000)
        self.assertIsNone(pit.market_cap(SYM, date(2024, 8, 9)))
        self.assertEqual(pit.market_cap(SYM, date(2024, 8, 4)), SHARES * 300000)   # a Sunday: Thursday's close
        self.assertIsNone(pit.market_cap(SYM, date(2024, 7, 19)))                   # no shares known yet
        self.assertIsNone(pit.market_cap("NOBODY", date(2024, 8, 1)))
