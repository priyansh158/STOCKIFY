"""D-55 (WP 4.3): positions_eod.csv's last column, underlying_mark_paise: a derivative's underlying mark at DAY_END
(the run's mark rule), empty for anything else. Written by both engines, byte-identical."""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden
from stockify.oracle import run_bundle as oracle_run

HEADER = ["trade_date", "instrument_id", "product", "qty", "avg_price_paise", "mark_paise", "mark_source",
          "unrealised_paise", "realised_paise", "underlying_mark_paise"]
SPEC = [{"from": "2026-01-01", "lot": 1, "tick_paise": 5}]
OPT = [{"from": "2026-01-01", "lot": 75, "tick_paise": 5}]
DAYS = ["2026-03-02", "2026-03-03"]


def _bar(iid: int, day: str, c: int) -> dict:
    return {"kind": "BAR", "instrument": iid, "interval_s": 86400, "start": f"{day}T00:00:00+05:30",
            "t": f"{day}T19:00:00+05:30", "o": c, "h": c, "l": c, "c": c, "v": 100_000}


def world() -> dict:
    """An equity, an index, its future and a call; all four bought on day 1 and held over two day ends."""
    inst = [{"id": 1, "kind": "EQ", "tradingsymbol": "TEST", "specs": SPEC},
            {"id": 2, "kind": "INDEX", "tradingsymbol": "NIFTY 50"},
            {"id": 3, "kind": "FUT", "tradingsymbol": "NFUT", "underlying": "NIFTY", "underlying_id": 2,
             "expiry": "2026-03-30", "specs": OPT},
            {"id": 4, "kind": "CE", "tradingsymbol": "NCE", "underlying": "NIFTY", "underlying_id": 2,
             "expiry": "2026-03-30", "strike_paise": 2_500_000, "specs": OPT}]
    events = []
    for day, eq, idx, fut, ce in zip(DAYS, (10_000, 10_100), (2_500_000, 2_512_000), (2_501_000, 2_513_000),
                                     (40_000, 40_500)):
        events += [_bar(1, day, eq), _bar(3, day, fut), _bar(4, day, ce), _bar(2, day, idx)]
    actions = [{"on_event": 1, "place": {"instrument": 1, "side": "BUY", "qty": 10, "product": "CNC", "type": "MARKET"}},
               {"on_event": 2, "place": {"instrument": 3, "side": "BUY", "qty": 75, "product": "NRML", "type": "MARKET"}},
               {"on_event": 3, "place": {"instrument": 4, "side": "BUY", "qty": 75, "product": "NRML", "type": "MARKET"}}]
    return {"id": "D55", "bundle": {"dates": DAYS, "instruments": inst},
            "run": {"fill_model": {"kind": "eod_close", "half_spread_ticks": 0}, "capital_paise": 10**9,
                    "price_basis": "raw"},
            "events": events, "strategy": {"actions": actions}}


def _rows(folder: Path) -> tuple[list[str], list[dict]]:
    with (folder / "positions_eod.csv").open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


class UnderlyingMarkTests(unittest.TestCase):
    def _run(self, impl: str, folder: Path) -> Path:
        g = world()
        pack, b = golden.build_pack(g, folder), golden.build_bundle(g)
        out = folder / impl
        (engine.run_bundle if impl == "engine" else oracle_run)(b, [pack], out, report=False)
        return out

    def test_the_column_on_the_oracle(self):
        with tempfile.TemporaryDirectory(prefix="d55-") as tmp:
            header, rows = _rows(self._run("oracle", Path(tmp)))
        self.assertEqual(header, HEADER)
        by = {(r["trade_date"], r["instrument_id"]): r for r in rows}
        self.assertEqual(by[("2026-03-03", "1")]["underlying_mark_paise"], "")              # an equity: empty
        self.assertEqual(by[("2026-03-03", "3")]["underlying_mark_paise"], "2512000")       # the index at day end
        self.assertEqual(by[("2026-03-03", "4")]["underlying_mark_paise"], "2512000")
        self.assertIn(("2026-03-03", "4"), by)

    @unittest.skipUnless(engine.available(), "stockify._engine is not built")
    def test_byte_identical_on_the_engine(self):
        with tempfile.TemporaryDirectory(prefix="d55-") as tmp:
            o = self._run("oracle", Path(tmp))
            e = self._run("engine", Path(tmp))
            self.assertEqual((e / "positions_eod.csv").read_bytes(), (o / "positions_eod.csv").read_bytes())


if __name__ == "__main__":
    unittest.main()
