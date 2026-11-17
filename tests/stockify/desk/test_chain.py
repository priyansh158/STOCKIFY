"""HD 6.1: the option chain snapshot (one quote poll) and the gauge read from it."""
from __future__ import annotations

import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import timeutil
from stockify.daemon.clock import ManualClock
from stockify.data import history, instruments, refdata
from stockify.desk import chain
from stockify.fno import black76 as B

DAY = date(2026, 11, 17)
NEAR, NEXT, LATER = "2026-11-24", "2026-12-29", "2027-01-26"
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
          "tick_size,lot_size,instrument_type,segment,exchange")
TAG = {NEAR: "26NOV", NEXT: "26DEC", LATER: "27JAN"}
SPOT = 153_000                                   # ₹1,530.00


def _dumps(strikes: list[int]) -> dict[str, str]:
    """INFY: shares, three monthly futures and options at `strikes` (rupees) for each; TCS: shares only."""
    nfo, token = [], 10_000
    for exp in (NEAR, NEXT, LATER):
        token += 1
        nfo.append(f"{token},{token},INFY{TAG[exp]}FUT,INFY,0,{exp},0,0.1,400,FUT,NFO-FUT,NFO")
        for k in strikes:
            for kind in ("CE", "PE"):
                token += 1
                nfo.append(f"{token},{token},INFY{TAG[exp]}{k}{kind},INFY,0,{exp},{k},0.05,400,{kind},NFO-OPT,NFO")
    nse = (f"{HEADER}\n408065,1,INFY,INFOSYS,0,,0,0.05,1,EQ,NSE,NSE\n"
           f"2953217,2,TCS,TCS,0,,0,0.05,1,EQ,NSE,NSE\n")
    return {"NSE": nse, "NFO": HEADER + "\n" + "\n".join(nfo) + "\n"}


def _q(ltp=None, bid=None, ask=None, oi=0) -> dict:  # noqa: ANN001
    empty = [{"price": 0, "quantity": 0, "orders": 0}]
    return {"last_price": ltp, "oi": oi,
            "depth": {"buy": [{"price": bid, "quantity": 400, "orders": 1}] if bid is not None else empty,
                      "sell": [{"price": ask, "quantity": 400, "orders": 1}] if ask is not None else empty}}


class FakeClient:
    """Answers `quote` from a dict of canned quotes and records each call."""

    def __init__(self, quotes: dict[str, dict] | None = None, default=None):  # noqa: ANN001
        self.quotes, self.default, self.calls = quotes or {}, default, []

    def quote(self, names: list[str]) -> dict:
        self.calls.append(list(names))
        out = {}
        for n in names:
            q = self.quotes.get(n, self.default(n) if self.default else None)
            if q is not None:
                out[n] = q
        return out


class CountingBucket:
    def __init__(self):
        self.n = 0

    def acquire(self) -> None:
        self.n += 1


class ChainTestBase(SandboxedDBTest):
    db_modules = (refdata, history)
    strikes = list(range(1400, 1720, 20))         # 16 strikes, ₹1,400 to ₹1,700

    def setUp(self):
        super().setUp()
        instruments.sync(DAY, _dumps(self.strikes))


class SnapshotTests(ChainTestBase):
    def test_snapshot_picks_two_expiries_and_strikes_either_side(self):
        client = FakeClient(default=lambda n: _q(ltp=10.0, bid=9.9, ask=10.1, oi=4000))
        snap = chain.snapshot("INFY", SPOT, DAY, timeutil.ist_ns(DAY, "10:00"), client=client,
                              strikes_each_side=3, bucket=CountingBucket())
        self.assertEqual(snap["expiries"], [NEAR, NEXT])
        for exp in (NEAR, NEXT):
            got = sorted({r["strike_paise"] for r in snap["rows"] if r["expiry"] == exp})
            self.assertEqual(got, [148_000, 150_000, 152_000, 154_000, 156_000, 158_000])
            self.assertEqual(sorted({r["kind"] for r in snap["rows"] if r["expiry"] == exp}), ["CE", "PE"])
        self.assertEqual(len(snap["rows"]), 24)
        names = client.calls[0]
        self.assertIn("NFO:INFY26NOVFUT", names)
        self.assertIn("NFO:INFY26DECFUT", names)
        self.assertNotIn("NFO:INFY27JANFUT", names)
        self.assertEqual(snap["lot_size"], 400)

    def test_snapshot_batches_and_converts_to_paise(self):
        strikes = list(range(790, 2290, 10))       # 75 strikes each side of ₹1,530: 600 options + 2 futures
        with refdata.transaction() as conn:         # replace the fixture's chain with a wider one
            conn.execute("DELETE FROM instrument_tokens")
            conn.execute("DELETE FROM contract_specs")
            conn.execute("DELETE FROM instruments")
        instruments.sync(DAY, _dumps(strikes))
        canned = {"NFO:INFY26NOVFUT": _q(ltp=1534.5, oi=1),
                  "NFO:INFY26NOV1530CE": _q(ltp=22.35, bid=22.3, ask=22.4, oi=120_000),
                  "NFO:INFY26NOV1530PE": _q(ltp=17.0, bid=None, ask=None, oi=80_000)}
        client = FakeClient(canned, default=lambda n: None if n.endswith("FUT") else _q(ltp=1.0, oi=400))
        bucket = CountingBucket()
        now = timeutil.ist_ns(DAY, "10:00")
        snap = chain.snapshot("INFY", SPOT, DAY, now, client=client, strikes_each_side=75, bucket=bucket)
        self.assertEqual([len(c) for c in client.calls], [500, 102])
        flat = [n for c in client.calls for n in c]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertEqual(bucket.n, 2)
        self.assertEqual(len(snap["rows"]), 600)
        self.assertEqual(snap["taken_ns"], now)

        by = {r["tradingsymbol"]: r for r in snap["rows"]}
        ce = by["INFY26NOV1530CE"]
        self.assertEqual(set(ce), {"instrument_id", "tradingsymbol", "kind", "strike_paise", "expiry", "bid_paise",
                                   "ask_paise", "ltp_paise", "oi", "iv", "delta"})
        self.assertEqual((ce["bid_paise"], ce["ask_paise"], ce["ltp_paise"], ce["oi"]), (2230, 2240, 2235, 120_000))
        self.assertEqual((ce["kind"], ce["strike_paise"], ce["expiry"]), ("CE", 153_000, NEAR))
        self.assertEqual(ce["instrument_id"], instruments.find("NFO", "INFY26NOV1530CE"))
        # the IV is read from the mid on the near future's price, and reprices to it
        self.assertEqual(snap["forwards"][NEAR], {"paise": 153_450, "source": "future"})
        self.assertEqual(snap["forwards"][NEXT]["source"], "spot")
        t, r = snap["t_years"][NEAR], snap["rate"]
        self.assertGreater(t, 0)
        self.assertAlmostEqual(B.price(True, 1534.5, 1530.0, t, r, ce["iv"]), 22.35, places=6)
        self.assertAlmostEqual(ce["delta"], B.greeks(True, 1534.5, 1530.0, t, r, ce["iv"]).delta, places=12)
        self.assertTrue(0 < ce["delta"] < 1)
        pe = by["INFY26NOV1530PE"]
        self.assertEqual((pe["bid_paise"], pe["ask_paise"], pe["ltp_paise"]), (None, None, 1700))
        self.assertIsNone(pe["iv"])
        self.assertIsNone(pe["delta"])

    def test_missing_quotes_leave_empty_fields(self):
        snap = chain.snapshot("INFY", SPOT, DAY, timeutil.ist_ns(DAY, "10:00"), client=FakeClient(),
                              strikes_each_side=2, bucket=CountingBucket())
        self.assertEqual(len(snap["rows"]), 16)
        for r in snap["rows"]:
            self.assertEqual((r["bid_paise"], r["ask_paise"], r["ltp_paise"], r["oi"], r["iv"]),
                             (None, None, None, 0, None))

    def test_the_default_bucket_paces_through_the_given_clock(self):
        clock = ManualClock(now_ns=timeutil.ist_ns(DAY, "10:00"), mono_ns=0)
        bucket = history.TokenBucket(1, clock)
        client = FakeClient()
        chain.snapshot("INFY", SPOT, DAY, clock.now_ns(), client=client, strikes_each_side=2, bucket=bucket)
        self.assertEqual(len(client.calls), 1)


class FnoTests(ChainTestBase):
    def test_not_fno_stock_is_detected(self):
        self.assertTrue(chain.is_fno("INFY", DAY))
        self.assertEqual(chain.lot_size("INFY", DAY), 400)
        self.assertFalse(chain.is_fno("TCS", DAY))
        self.assertIsNone(chain.lot_size("TCS", DAY))
        client = FakeClient()
        snap = chain.snapshot("TCS", 400_000, DAY, timeutil.ist_ns(DAY, "10:00"), client=client,
                              bucket=CountingBucket())
        self.assertEqual((snap["expiries"], snap["rows"], client.calls), ([], [], []))
        g = chain.gauge(snap, 400_000, [])
        self.assertIsNone(g["pcr_oi"])
        self.assertIsNone(g["max_pain_paise"])
        self.assertEqual((g["call_walls"], g["put_walls"]), ([], []))
        self.assertTrue(g["sentences"])


def _row(kind: str, strike_rupees: int, oi: int, *, expiry: str = NEAR, bid=100, ask=110, iv=None) -> dict:  # noqa: ANN001
    return {"instrument_id": 0, "tradingsymbol": f"X{strike_rupees}{kind}", "kind": kind,
            "strike_paise": strike_rupees * 100, "expiry": expiry, "bid_paise": bid, "ask_paise": ask,
            "ltp_paise": 105, "oi": oi, "iv": iv, "delta": None}


def _snap(rows: list[dict]) -> dict:
    return {"underlying": "X", "expiries": [NEAR, NEXT], "rows": rows}


class GaugeTests(unittest.TestCase):
    def test_pcr_and_walls(self):
        rows = [_row("CE", 1540, 500), _row("CE", 1560, 900), _row("CE", 1580, 300), _row("CE", 1600, 700),
                _row("CE", 1520, 5000),               # below the price: not a call wall
                _row("PE", 1520, 800), _row("PE", 1500, 1200), _row("PE", 1480, 100), _row("PE", 1460, 400),
                _row("PE", 1560, 5000),               # above the price: not a put wall
                _row("CE", 1700, 99_999, expiry=NEXT)]    # next expiry: not in the near-expiry gauge
        g = chain.gauge(_snap(rows), SPOT, [])
        self.assertAlmostEqual(g["pcr_oi"], (800 + 1200 + 100 + 400 + 5000) / (500 + 900 + 300 + 700 + 5000))
        self.assertEqual(g["call_walls"], [156_000, 160_000, 154_000])
        self.assertEqual(g["put_walls"], [150_000, 152_000, 146_000])
        text = " ".join(g["sentences"])
        self.assertIn("₹1,560.00", text)
        self.assertIn("₹1,500.00", text)
        self.assertNotIn("$", text)

    def test_max_pain_on_a_known_chain(self):
        # Payout at expiry price S = Σ call OI × max(0, S − K) + Σ put OI × max(0, K − S), by hand:
        #   S=90:  calls 0;              puts 200×20 + 50×10 = 4500      → 4500
        #   S=100: calls 100×10 = 1000;  puts 200×10 = 2000              → 3000
        #   S=110: calls 100×20 + 300×10 = 5000; puts 0                  → 5000
        rows = [_row("CE", 90, 100), _row("CE", 100, 300), _row("CE", 110, 50),
                _row("PE", 90, 10), _row("PE", 100, 50), _row("PE", 110, 200)]
        g = chain.gauge(_snap(rows), 10_000, [])
        self.assertEqual(g["max_pain_paise"], 10_000)
        self.assertIn("₹100.00", " ".join(g["sentences"]))

    def test_rows_without_quotes_are_excluded(self):
        rows = [_row("CE", 1560, 900), _row("PE", 1500, 600),
                _row("CE", 1600, 50_000, bid=None, ask=None),    # no quotes: excluded
                _row("PE", 1460, 0),                              # no open interest: excluded
                _row("PE", 1480, 70_000, bid=None, ask=None)]
        g = chain.gauge(_snap(rows), SPOT, [])
        self.assertEqual(g["call_walls"], [156_000])
        self.assertEqual(g["put_walls"], [150_000])
        self.assertAlmostEqual(g["pcr_oi"], 600 / 900)
        self.assertIn(g["max_pain_paise"], (150_000, 156_000))

    def test_atm_iv_is_the_mean_of_the_call_and_put_at_the_strike_nearest_spot(self):
        rows = [_row("CE", 1520, 10, iv=0.30), _row("PE", 1520, 10, iv=0.34),
                _row("CE", 1540, 10, iv=0.20), _row("PE", 1540, 10, iv=0.22),
                _row("CE", 1530, 10, iv=None, bid=None, ask=None)]   # nearest, but no quotes
        g = chain.gauge(_snap(rows), SPOT + 100, [])                  # ₹1,531: 1,540 is 9 away, 1,520 is 11
        self.assertAlmostEqual(g["atm_iv"], 0.21)

    def test_iv_percentile_none_with_short_history(self):
        rows = [_row("CE", 1530, 10, iv=0.25), _row("PE", 1530, 10, iv=0.25)]
        short = [0.1 + i / 1000 for i in range(59)]
        g = chain.gauge(_snap(rows), SPOT, short)
        self.assertAlmostEqual(g["atm_iv"], 0.25)
        self.assertIsNone(g["iv_percentile"])
        self.assertIn("past year", " ".join(g["sentences"]))
        history_ = [0.20] * 30 + [0.30] * 30                           # 60 days: half below 0.25
        g = chain.gauge(_snap(rows), SPOT, history_)
        self.assertAlmostEqual(g["iv_percentile"], 50.0)
        g = chain.gauge(_snap(rows), SPOT, [0.1] * 60 + [float("nan")])
        self.assertAlmostEqual(g["iv_percentile"], 100.0)


class IvHistoryTests(ChainTestBase):
    def _eod(self, day: date, iid: int, settle: int | None, *, close: int | None = None, cm: bool = False) -> None:
        with refdata.transaction() as conn:
            if cm:
                conn.execute("INSERT INTO cm_eod (trade_date, instrument_id, series, close_paise) "
                             "VALUES (?, ?, 'EQ', ?)",
                             (day.isoformat(), iid, close))
            else:
                conn.execute("INSERT INTO fo_eod (trade_date, instrument_id, settle_paise, close_paise, oi) "
                             "VALUES (?, ?, ?, ?, 1)", (day.isoformat(), iid, settle, close))

    def test_no_fo_eod_rows_give_an_empty_history(self):
        self.assertEqual(chain.iv_history("INFY", DAY), [])
        self.assertEqual(chain.iv_history("TCS", DAY), [])

    def test_atm_iv_per_day_from_settle_prices(self):
        days = [date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 17)]
        eq = instruments.find("NSE", "INFY")
        r = B.rate_on(chain._rate_rows(), DAY.isoformat())
        sigma = {days[0]: 0.30, days[1]: 0.25, days[2]: 0.20}
        for d in days:
            close = 153_200                                       # ATM strike ₹1,540 (nearest ₹1,532)
            self._eod(d, eq, None, close=close, cm=True)
            # near expiry 2026-11-24: options and its future; the far 2026-12-29 strikes are decoys
            fut = instruments.derivative("INFY", "FUT", NEAR)
            self._eod(d, fut, 153_500)
            t = B.year_fraction(timeutil.ist_ns(d, "15:30"), chain._last_trading_ns(NEAR))
            for kind in ("CE", "PE"):
                for k in (1520, 1540, 1560):
                    iid = instruments.derivative("INFY", kind, NEAR, k * 100)
                    px = B.price(kind == "CE", 1535.0, float(k), t, r, sigma[d])
                    self._eod(d, iid, round(px * 100))
                far = instruments.derivative("INFY", kind, NEXT, 154_000)
                self._eod(d, far, 1)                              # a price no sane IV fits
        got = chain.iv_history("INFY", DAY)
        self.assertEqual(len(got), 3)
        for v, d in zip(got, days):
            self.assertAlmostEqual(v, sigma[d], delta=0.003)
        self.assertEqual(chain.iv_history("INFY", date(2026, 11, 16))[-1:], got[1:2])
        self.assertEqual(len(chain.iv_history("INFY", date(2027, 12, 1))), 0)   # all over a year old

    def test_a_contract_on_its_last_trading_day_is_skipped(self):
        with refdata.transaction() as conn:                       # NSE moved the near expiry to today
            conn.execute("UPDATE instruments SET actual_expiry=? WHERE expiry=?", (DAY.isoformat(), NEAR))
        self._eod(DAY, instruments.find("NSE", "INFY"), None, close=153_000, cm=True)
        r = B.rate_on(chain._rate_rows(), DAY.isoformat())
        t = B.year_fraction(timeutil.ist_ns(DAY, "15:30"), chain._last_trading_ns(NEXT))
        for kind in ("CE", "PE"):
            self._eod(DAY, instruments.derivative("INFY", kind, NEAR, 154_000), 2_000)
            px = B.price(kind == "CE", 1530.0, 1540.0, t, r, 0.25)
            self._eod(DAY, instruments.derivative("INFY", kind, NEXT, 154_000), round(px * 100))
        got = chain.iv_history("INFY", DAY)                       # read from the next expiry instead
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0], 0.25, delta=0.003)


if __name__ == "__main__":
    unittest.main()
