"""A landed NSE day for the paper-daemon tests (M5.3): a NIFTY expiry day, 2026-11-17.

    class T(PaperDayTest):
        def test_x(self):
            self.record()                        # landing file: index, near future and 12 CE strikes, 1 s cadence
            sha = packs.build({"dataset": "ticks", "from": ..., "to": ...})

Every strike is inside the universe's `n_full`, so the subscriptions are the same before and after a restart.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import sessionconfig, store, timeutil
from stockify.daemon import kite_packets as kp
from stockify.daemon.clock import ManualClock
from stockify.daemon.landing import LandingDB
from stockify.daemon.recorder import Recorder
from stockify.data import compact, history, instruments, refdata
from tests.stockify.daemon import packets_fixture as pf

DAY = date(2026, 11, 17)
EXPIRY = DAY.isoformat()
SEC = 1_000_000_000
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
          "tick_size,lot_size,instrument_type,segment,exchange")
FUT_TOKEN = 9001
STRIKES = tuple(range(23_500, 24_700, 100))            # twelve calls
CE_TOKEN = 90_000                                       # + index in STRIKES
HEARTBEAT = b"\x00"

# The recorder's universe: one chain wide enough to hold every fixture strike, so it never re-centres.
UNIVERSE = {"indices_full": ["NSE:NIFTY 50"], "futures_full": [], "equities_full": [],
            "stock_futures_full": [], "equities_quote": [],
            "chains": [{"underlying": "NIFTY", "expiry": "weekly:0", "n_full": 50, "n_quote": 0,
                        "recenter_threshold": 1, "hysteresis": 0}],
            "limits": {"max_per_connection": 3000, "warn_fraction": "0.8"}}


def t(hhmmss: str) -> int:
    """UTC epoch ns of an IST wall time on DAY."""
    return timeutil.ist_ns(DAY, hhmmss)


class PaperDayTest(SandboxedDBTest):
    db_modules = (store, refdata, history)

    def setUp(self):
        super().setUp()
        strikes = "".join(
            f"{CE_TOKEN + i},{20 + i},NIFTY26N17{s}CE,NIFTY,0,{EXPIRY},{s},0.05,65,CE,NFO-OPT,NFO\n"
            for i, s in enumerate(STRIKES))
        instruments.sync(date(2026, 11, 16), {
            "NSE": f"{HEADER}\n{pf.NIFTY_INDEX},1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n",
            "NFO": (f"{HEADER}\n{FUT_TOKEN},3,NIFTY26NOVFUT,NIFTY,0,2026-11-24,0,0.1,65,FUT,NFO-FUT,NFO\n" + strikes),
        }, underlyings={"NIFTY": "NSE:NIFTY 50"})
        self.idx = instruments.find("NSE", "NIFTY 50")
        self.fut = instruments.find("NFO", "NIFTY26NOVFUT")
        self.ces = [instruments.find("NFO", f"NIFTY26N17{s}CE") for s in STRIKES]
        patcher = patch.object(compact, "session_over", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def t(self, hhmmss: str) -> int:
        return t(hhmmss)

    def record(self, seconds: int = 1200) -> None:
        """Land DAY from 09:15:00: every second a frame with the index (FULL), the near future (QUOTE) and the
        twelve calls (FULL), and a heartbeat between frames. Then the day's end-of-day rows: `fo_eod` settle
        prices for the future and a call, and the index's final settlement price."""
        landing = LandingDB(DAY)
        clock = ManualClock(t("09:14:59"))
        tokens = {pf.NIFTY_INDEX: self.idx, FUT_TOKEN: self.fut,
                  **{CE_TOKEN + i: iid for i, iid in enumerate(self.ces)}}
        rec = Recorder(landing, tokens.get, clock)
        rec.begin()
        landing.log_subscriptions(
            [(t("09:15:00"), "subscribe", self.idx, pf.NIFTY_INDEX, "full", "start"),
             (t("09:15:00"), "subscribe", self.fut, FUT_TOKEN, "quote", "start"),
             *[(t("09:15:00"), "subscribe", iid, CE_TOKEN + i, "full", "start") for i, iid in enumerate(self.ces)]])
        base = t("09:15:00")
        for s in range(seconds):
            clock.set(base + s * SEC)
            exch = base // SEC + s
            frame = [pf.index_full(pf.NIFTY_INDEX, 2_400_000 + s, 2_401_000, 2_399_000, 2_400_000, 2_390_000,
                                   1000 + s, exch),
                     pf.quote(FUT_TOKEN, 2_410_000 + s, 65, 2_409_000, 100 + s, 10, 20, 1, 2, 3, 4)]
            for i in range(len(STRIKES)):
                frame.append(pf.full(CE_TOKEN + i, 12_000 - 100 * i + s, vol=1_000 + s, exch_ts=exch))
            rec.on_message(None, kp.build_frame(frame), True)
            rec.process(rec.queue.get_nowait())
            clock.set(base + s * SEC + SEC // 2)
            rec.on_message(None, HEARTBEAT, True)
            rec.process(rec.queue.get_nowait())
        rec.flush()
        landing.close()
        with refdata.transaction() as conn:
            conn.execute("INSERT INTO fo_eod (trade_date, instrument_id, settle_paise) VALUES (?, ?, 2410500)",
                         (EXPIRY, self.fut))
            conn.execute("INSERT INTO fo_eod (trade_date, instrument_id, settle_paise) VALUES (?, ?, 1234)",
                         (EXPIRY, self.ces[4]))
            conn.execute("INSERT INTO settlement_prices VALUES (?, ?, 2400500, 'fo_eod', 'ok')", (self.idx, EXPIRY))

    def session_cfg(self, **over) -> dict:
        """A normalised session config whose universe is the whole NIFTY chain of DAY's expiry."""
        raw = {"schema": "stockify.session/1", "name": "s1",
               "strategy": {"kind": "python", "ref": "stockify.runconfig:RunConfigError"},
               "capital_paise": 10_000_000, "costs": {"allow_unverified": True},
               "universe": [{"chain": {"underlying": "NIFTY", "expiry": "nearest", "strikes": 2}}]}
        raw.update(over)
        return sessionconfig.load(raw)
