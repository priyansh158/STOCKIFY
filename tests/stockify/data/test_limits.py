"""Price-band snapshots into daily_limits (M1.5)."""
from __future__ import annotations

import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import timeutil
from stockify.daemon.clock import ManualClock
from stockify.daemon.subscriptions import Sub
from stockify.data import instruments, limits, refdata

DAY = date(2026, 11, 17)
SEC = 1_000_000_000
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
          "tick_size,lot_size,instrument_type,segment,exchange")


class FakeQuotes:
    def __init__(self, clock, fail: int = 0):  # noqa: ANN001
        self.clock, self.fail, self.calls = clock, fail, []

    def quote(self, names):  # noqa: ANN001
        self.calls.append((list(names), self.clock.mono_ns()))
        if self.fail:
            self.fail -= 1
            raise RuntimeError("TokenException")
        return {n: {"upper_circuit_limit": 120.55, "lower_circuit_limit": 80.1} for n in names}


class Manager:
    def __init__(self, subs):  # noqa: ANN001
        self.current = {s.instrument_id: s for s in subs}


class LimitsTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        support.reset_sqlite_singleton(refdata)
        self.addCleanup(support.reset_sqlite_singleton, refdata)
        opts = "".join(f"{9000 + i},{i},NIFTY26N17{24000 + 100 * i}CE,NIFTY,0,2026-11-17,{24000 + 100 * i},0.05,65,CE,"
                       f"NFO-OPT,NFO\n" for i in range(1200))
        instruments.sync(date(2026, 11, 16), {
            "NSE": f"{HEADER}\n256265,1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n"
                   f"738561,2,RELIANCE,RELIANCE,0,,0,0.1,1,EQ,NSE,NSE\n",
            "NFO": f"{HEADER}\n" + opts}, underlyings={"NIFTY": "NSE:NIFTY 50"})
        self.clock = ManualClock(timeutil.ist_ns(DAY, "09:00"))
        self.rel = instruments.find("NSE", "RELIANCE")
        self.idx = instruments.find("NSE", "NIFTY 50")
        self.opts = [instruments.find("NFO", f"NIFTY26N17{24000 + 100 * i}CE") for i in range(1200)]

    def rows(self):
        return [tuple(r) for r in refdata._connect().execute(
            "SELECT instrument_id, snapshot_ns, upper_paise, lower_paise, source FROM daily_limits ORDER BY 1, 2")]

    def test_batches_rate_and_paise(self):
        client = FakeQuotes(self.clock)
        n = limits.snapshot(DAY, [self.rel, self.idx, *self.opts], client=client, clock=self.clock)
        self.assertEqual(n, 1201)                                       # the index has no band: skipped
        self.assertEqual([len(c[0]) for c in client.calls], [500, 500, 201])
        monos = [c[1] for c in client.calls]
        self.assertTrue(all(b - a >= SEC for a, b in zip(monos, monos[1:])))     # 1 request/second
        rel = [r for r in self.rows() if r[0] == self.rel][0]
        self.assertEqual(rel[2:], (12055, 8010, "kite_quote"))

    def test_snapshotter_hourly_for_options_and_retry(self):
        subs = [Sub(self.rel, 2, "full", "equity"), Sub(self.opts[0], 9000, "full", "chain"),
                Sub(self.opts[1], 9001, "quote", "chain")]
        client = FakeQuotes(self.clock, fail=1)
        snap = limits.Snapshotter(DAY, Manager(subs), client=client, clock=self.clock)
        self.assertEqual(snap.tick(), 0)                                # first call fails
        self.clock.advance(60 * SEC)
        self.assertEqual(snap.tick(), 0)                                # waits 5 minutes
        self.clock.advance(5 * 60 * SEC)
        self.assertEqual(snap.tick(), 2)                                # FULL only
        self.clock.advance(30 * 60 * SEC)
        self.assertEqual(snap.tick(), 0)
        self.clock.advance(31 * 60 * SEC)
        self.assertEqual(snap.tick(), 1)                                # the option again, after an hour
        self.assertEqual(sorted(client.calls[-1][0]), ["NFO:NIFTY26N1724000CE"])


if __name__ == "__main__":
    unittest.main()
