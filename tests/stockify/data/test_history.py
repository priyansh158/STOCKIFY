"""Kite bar cache (M1.4, U-04): chunks, throttle, limit learning, adjustments."""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths, timeutil
from stockify.daemon.clock import ManualClock
from stockify.data import history, instruments, refdata

TODAY = date(2026, 11, 16)
IST = timezone(timedelta(hours=5, minutes=30))
SEC = 1_000_000_000
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
          "tick_size,lot_size,instrument_type,segment,exchange")


class InputException(Exception):
    """Same class name as kiteconnect's, which is what the cache recognises."""


class FakeKite:
    def __init__(self, clock: ManualClock, max_days: dict | None = None, factor: float = 1.0):
        self.clock, self.max_days, self.factor = clock, max_days or {}, factor
        self.calls: list[dict] = []

    def historical_data(self, token, from_date, to_date, interval, continuous=False, oi=False):  # noqa: ANN001
        self.calls.append({"token": token, "from": from_date, "to": to_date, "interval": interval,
                           "continuous": continuous, "oi": oi, "mono": self.clock.mono_ns()})
        span = (to_date - from_date).total_seconds() / 86400
        limit = self.max_days.get(interval)
        if limit is not None and span > limit:
            raise InputException(f"interval exceeds max limit: {limit} days")
        rows, d = [], from_date.date()
        while d <= to_date.date():
            if d.weekday() < 5:
                if interval == "day":
                    stamps = [datetime(d.year, d.month, d.day, tzinfo=IST)]
                else:
                    stamps = [datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST) + timedelta(minutes=m)
                              for m in range(3)]
                for ts in stamps:
                    if from_date <= ts.replace(tzinfo=None) <= to_date:
                        price = round((100 + d.toordinal() % 7) * self.factor, 2)
                        rows.append({"date": ts, "open": price, "high": price + 1, "low": price - 1,
                                     "close": price, "volume": 10, "oi": 5 if continuous else None})
            d += timedelta(days=1)
        return rows


class HistoryTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        for mod in (refdata, history):
            support.reset_sqlite_singleton(mod)
            self.addCleanup(support.reset_sqlite_singleton, mod)
        instruments.sync(date(2026, 11, 2), self.dump(include_expired=True), underlyings={"NIFTY": "NSE:NIFTY 50"})
        instruments.sync(TODAY, self.dump(include_expired=False), underlyings={"NIFTY": "NSE:NIFTY 50"})
        self.rel = instruments.find("NSE", "RELIANCE")
        self.old_fut = instruments.derivative("NIFTY", "FUT", "2026-11-10")
        self.fut = instruments.derivative("NIFTY", "FUT", "2026-11-24")
        self.clock = ManualClock(timeutil.ist_ns(TODAY, "20:00"))

    @staticmethod
    def dump(include_expired: bool) -> dict:
        nfo = f"{HEADER}\n9002,12,NIFTY26NOV24FUT,NIFTY,0,2026-11-24,0,0.1,65,FUT,NFO-FUT,NFO\n"
        if include_expired:
            nfo += "9001,11,NIFTY26NOV10FUT,NIFTY,0,2026-11-10,0,0.1,65,FUT,NFO-FUT,NFO\n"
        return {"NSE": (f"{HEADER}\n256265,1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n"
                        f"738561,2,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.1,1,EQ,NSE,NSE\n"), "NFO": nfo}

    def fetch(self, kite, interval="day", start=date(2026, 1, 1), end=date(2026, 11, 13), iid=None, **kw):
        return history.fetch(iid or self.rel, interval, start, end, client=kite, clock=self.clock, today=TODAY, **kw)


class FetchTests(HistoryTest):
    def test_bars_stored_as_start_utc_ns_in_paise(self):
        kite = FakeKite(self.clock)
        stats = self.fetch(kite, "minute", date(2026, 11, 13), date(2026, 11, 13))
        rows = history.bars(self.rel, "minute")
        self.assertEqual((stats.rows, len(rows)), (3, 3))
        self.assertEqual(rows[0]["ts_ns"], timeutil.ist_ns(date(2026, 11, 13), "09:15"))   # start, UTC ns
        self.assertEqual(rows[0]["ts_ns"] - rows[0]["ts_ns"] // SEC * SEC, 0)
        self.assertEqual(rows[0]["close_paise"], (100 + date(2026, 11, 13).toordinal() % 7) * 100)
        self.assertEqual(rows[0]["fetched_on"], "2026-11-16")
        self.assertTrue(kite.calls[0]["oi"])

    def test_chunk_boundaries(self):
        kite = FakeKite(self.clock)
        self.fetch(kite, "minute", date(2026, 1, 1), date(2026, 5, 31))     # 151 days, 60-day chunks
        spans = [(c["from"], c["to"]) for c in kite.calls]
        self.assertEqual(len(spans), 3)
        self.assertEqual(spans[0][0], datetime(2026, 1, 1, 0, 0))
        for (_, a_end), (b_start, _) in zip(spans, spans[1:]):
            self.assertLessEqual(b_start - a_end, timedelta(seconds=1))    # contiguous, no overlap
        self.assertEqual(spans[-1][1], datetime(2026, 5, 31, 23, 59, 59))
        self.assertTrue(all((b - a).days < 60 for a, b in spans))

    def test_throttle_three_per_second(self):
        kite = FakeKite(self.clock)
        self.fetch(kite, "minute", date(2025, 1, 1), date(2026, 6, 30))     # 10 chunks
        monos = [c["mono"] for c in kite.calls]
        self.assertGreaterEqual(len(monos), 9)
        for a, b in zip(monos, monos[3:]):
            self.assertGreaterEqual(b - a, SEC - 1_000)                     # any 4 calls span >= ~1 s

    def test_range_limit_learned(self):
        kite = FakeKite(self.clock, max_days={"minute": 30})
        stats = self.fetch(kite, "minute", date(2026, 1, 1), date(2026, 3, 31))
        self.assertGreater(stats.rows, 0)
        learned = json.loads((paths.lab_dir() / "kite_limits_learned.json").read_text())
        self.assertEqual(learned["minute"], 30)
        self.assertEqual(history.max_days("minute"), 30)
        calls = len(kite.calls)
        self.fetch(kite, "minute", date(2026, 4, 1), date(2026, 4, 29), check_adjustments=False)
        self.assertEqual(len(kite.calls) - calls, 1)                        # learned limit used directly

    def test_refetch_overwrites_not_duplicates(self):
        kite = FakeKite(self.clock)
        self.fetch(kite, start=date(2026, 11, 2), end=date(2026, 11, 13))
        self.fetch(kite, start=date(2026, 11, 2), end=date(2026, 11, 13))
        self.assertEqual(len(history.bars(self.rel, "day")), 10)

    def test_expired_contract_has_no_history(self):
        stats = self.fetch(FakeKite(self.clock), iid=self.old_fut)
        self.assertEqual(stats.outcome, "no_token")


class AdjustmentTests(HistoryTest):
    def test_live_instrument_refetched(self):
        self.fetch(FakeKite(self.clock), start=date(2026, 11, 2), end=date(2026, 11, 13))
        self.fetch(FakeKite(self.clock), "minute", date(2026, 11, 13), date(2026, 11, 13), check_adjustments=False)
        split = FakeKite(self.clock, factor=0.5)                             # a 1:2 split, applied retroactively
        stats = self.fetch(split, start=date(2026, 11, 9), end=date(2026, 11, 13))
        self.assertEqual(stats.adjustment, "refetched")
        adj = history.adjustments(self.rel)
        self.assertEqual((adj[0]["action"], round(adj[0]["ratio"], 6)), ("refetched", 2.0))
        self.assertEqual(history.bars(self.rel, "minute"), [])               # whole cache dropped
        days = history.bars(self.rel, "day")
        self.assertEqual(len(days), 5)                                        # only the refetched range
        self.assertEqual(days[0]["close_paise"], round((100 + date(2026, 11, 9).toordinal() % 7) * 0.5, 2) * 100)

    def test_expired_instrument_only_flagged(self):
        # Cached while live: fetched on 2026-11-02, when the 2026-11-10 future still traded.
        kite = FakeKite(self.clock)
        history.fetch(self.old_fut, "day", date(2026, 11, 2), date(2026, 11, 6), client=kite, clock=self.clock,
                      today=date(2026, 11, 2))
        with refdata.transaction() as conn:          # sweep needs a token to re-ask; give it the old one back
            conn.execute("UPDATE instrument_tokens SET valid_to=NULL WHERE instrument_id=?", (self.old_fut,))
        stats = history.fetch(self.old_fut, "day", date(2026, 11, 2), date(2026, 11, 6),
                              client=FakeKite(self.clock, factor=0.5), clock=self.clock, today=TODAY)
        self.assertEqual(stats.adjustment, "flagged_only")
        self.assertEqual(len(history.bars(self.old_fut, "day")), 5)          # nothing deleted

    def test_no_change_no_row(self):
        self.fetch(FakeKite(self.clock), start=date(2026, 11, 2), end=date(2026, 11, 13))
        stats = self.fetch(FakeKite(self.clock), start=date(2026, 11, 9), end=date(2026, 11, 13))
        self.assertIsNone(stats.adjustment)
        self.assertEqual(history.adjustments(), [])

    def test_inconsistent_ratio_is_not_an_adjustment(self):
        self.fetch(FakeKite(self.clock), start=date(2026, 11, 2), end=date(2026, 11, 13))
        with history._Tx() as conn:                                          # one corrupted cached bar only
            conn.execute("UPDATE bars SET close_paise=close_paise*2 WHERE ts_ns=(SELECT MAX(ts_ns) FROM bars)")
        self.assertIsNone(self.fetch(FakeKite(self.clock), start=date(2026, 11, 13), end=date(2026, 11, 13)).adjustment)

    def test_sweep_checks_live_cached_instruments(self):
        self.fetch(FakeKite(self.clock), start=date(2026, 11, 2), end=date(2026, 11, 13))
        out = history.sweep(client=FakeKite(self.clock, factor=0.5), clock=self.clock, today=TODAY)
        self.assertEqual([(s.instrument_id, s.adjustment) for s in out], [(self.rel, "refetched")])


class ContinuousTests(HistoryTest):
    def test_continuous_day_bars_on_synthetic_instrument(self):
        kite = FakeKite(self.clock)
        stats = history.fetch_continuous("NIFTY", date(2026, 1, 1), date(2026, 11, 13), client=kite,
                                         clock=self.clock, today=TODAY)
        info = instruments.get(stats.instrument_id)
        self.assertEqual((info["kind"], info["tradingsymbol"], info["source"]), ("FUT_CONT", "CONT:NIFTY:FUT", "synthetic"))
        self.assertTrue(all(c["continuous"] and c["interval"] == "day" and c["token"] == 9002 for c in kite.calls))
        self.assertEqual(history.bars(self.fut, "day"), [])                  # never mixed into the contract
        self.assertGreater(len(history.bars(stats.instrument_id, "day")), 200)
        self.assertEqual(history.continuous_instrument("NIFTY", TODAY), stats.instrument_id)

    def test_fetch_refuses_continuous_instrument(self):
        iid = history.continuous_instrument("NIFTY", TODAY)
        with self.assertRaises(history.HistoryError):
            self.fetch(FakeKite(self.clock), iid=iid)


if __name__ == "__main__":
    unittest.main()
