"""D-79: a bar's volume is in units for every source.

NSE's F&O daily file gives traded volume in contracts (lots); `fo_eod.volume` keeps it as published. The pack
builder converts it once, where it reads `fo_eod`: contracts × lot, the lot from the row's own `lot_size` or, where
that is NULL (old-format rows), from the dated `contract_specs` row in force on the trade date. A row with no lot
anywhere fails the build. Equity and index bars, and `fo_eod` itself, are left as they are. Both runners then cap
a fill at their share of the bar's volume in units.
"""
from __future__ import annotations

import csv
import random
import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, engine, paths, runconfig, timeutil
from stockify.data import history, instruments, packs, refdata
from stockify.oracle import run_bundle as oracle_run

DAY1, DAY2 = date(2026, 11, 16), date(2026, 11, 17)
INT64_MAX = 2**63 - 1
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
          "tick_size,lot_size,instrument_type,segment,exchange")
POOL = range(24300, 25500, 100)                      # twelve more strikes for the random rounds


def _option(token: int, strike: int, lot: int) -> str:
    return f"{token},{token - 9000},NIFTY26N24{strike}CE,NIFTY,0,2026-11-24,{strike},0.05,{lot},CE,NFO-OPT,NFO\n"


def _dumps(dated_lot: int = 75, late: bool = False) -> dict[str, str]:
    nfo = _option(9101, 24000, 75) + _option(9102, 24100, dated_lot)
    nfo += "".join(_option(9200 + n, strike, 75) for n, strike in enumerate(POOL))
    if late:
        nfo += _option(9103, 24200, 75)
    return {"NSE": (f"{HEADER}\n256265,1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n"
                    f"738561,2,RELIANCE,RELIANCE,0,,0,0.1,1,EQ,NSE,NSE\n"),
            "NFO": f"{HEADER}\n{nfo}"}


class FoVolumeTest(support.SandboxedDBTest):
    """NIFTY options with lot 75 from DAY1. `second_day()` syncs DAY2: the 24100 call's lot becomes 65 (a second
    dated spec row) and the 24200 call is listed for the first time (no spec in force on DAY1)."""

    db_modules = (refdata, history)

    def setUp(self):
        super().setUp()
        instruments.sync(DAY1, _dumps(), underlyings={"NIFTY": "NSE:NIFTY 50"})
        self.ce = instruments.find("NFO", "NIFTY26N2424000CE")
        self.dated = instruments.find("NFO", "NIFTY26N2424100CE")
        self.idx = instruments.find("NSE", "NIFTY 50")
        self.rel = instruments.find("NSE", "RELIANCE")
        self.pool = [instruments.find("NFO", f"NIFTY26N24{strike}CE") for strike in POOL]

    def second_day(self) -> None:
        instruments.sync(DAY2, _dumps(dated_lot=65, late=True), underlyings={"NIFTY": "NSE:NIFTY 50"})
        self.late = instruments.find("NFO", "NIFTY26N2424200CE")

    def fo_row(self, day: date, iid: int, contracts, lot, close: int = 10_000, oi: int = 0) -> None:  # noqa: ANN001
        with refdata.transaction() as conn:
            conn.execute("INSERT INTO fo_eod (trade_date, instrument_id, open_paise, high_paise, low_paise, "
                         "close_paise, volume, oi, lot_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (day.isoformat(), iid, close, close, close, close, contracts, oi, lot))

    def spec(self, ids: list[int], start: date = DAY1, end: date = DAY1) -> dict:
        return {"dataset": "bhav_eod", "from": start.isoformat(), "to": end.isoformat(),
                "instruments": [{"ids": ids}]}

    def bars(self, sha: str) -> dict[tuple[str, int], dict]:
        """BAR records by (IST trade date, instrument)."""
        _, recs = packs.read(packs.pack_path(sha))
        return {(timeutil.ns_to_ist(r["start_ns"]).date().isoformat(), r["instrument_id"]): r
                for r in recs if r["kind"] == "BAR"}


class ConversionTests(FoVolumeTest):
    def test_a_row_with_its_own_lot_packs_contracts_times_lot(self):
        self.fo_row(DAY1, self.ce, 20_000, 75, oi=1_234_500)
        bar = self.bars(packs.build(self.spec([self.ce])))[("2026-11-16", self.ce)]
        self.assertEqual(bar["volume"], 1_500_000)                       # 20,000 contracts × 75 = 15,00,000 units
        self.assertEqual((bar["oi"], bar["close"], bar["interval_s"]), (1_234_500, 10_000, 86400))   # oi: units already

    def test_the_rows_own_lot_comes_before_the_dated_spec(self):
        self.fo_row(DAY1, self.ce, 1_000, 50)                            # the spec in force says 75
        self.assertEqual(self.bars(packs.build(self.spec([self.ce])))[("2026-11-16", self.ce)]["volume"], 50_000)

    def test_a_null_lot_row_takes_the_spec_in_force_on_its_trade_date(self):
        self.second_day()
        specs = [tuple(r) for r in refdata._connect().execute(
            "SELECT effective_from, lot_size FROM contract_specs WHERE instrument_id=? ORDER BY effective_from",
            (self.dated,))]
        self.assertEqual(specs, [("2026-11-16", 75), ("2026-11-17", 65)])
        self.fo_row(DAY1, self.dated, 1_000, None)
        self.fo_row(DAY2, self.dated, 1_000, None)
        bars = self.bars(packs.build(self.spec([self.dated], DAY1, DAY2)))
        self.assertEqual(bars[("2026-11-16", self.dated)]["volume"], 75_000)
        self.assertEqual(bars[("2026-11-17", self.dated)]["volume"], 65_000)

    def test_a_row_with_no_lot_anywhere_fails_the_build_and_names_instrument_and_day(self):
        self.second_day()
        self.assertIsNone(instruments.spec_at(self.late, DAY1))
        for contracts in (20_000, 0, None):                              # never a guessed lot, whatever the volume
            with self.subTest(contracts=contracts):
                with refdata.transaction() as conn:
                    conn.execute("DELETE FROM fo_eod")
                self.fo_row(DAY1, self.late, contracts, None)
                before = sorted(p.name for p in paths.packs_dir().iterdir())
                with self.assertRaisesRegex(
                        packs.PackError, rf"fo_eod 2026-11-16 instrument {self.late} \(NFO:NIFTY26N2424200CE\): "
                                         r"no lot size in the row and no contract spec in force"):
                    packs.build(self.spec([self.late]))
                self.assertEqual(sorted(p.name for p in paths.packs_dir().iterdir()), before)   # nothing published

    def test_a_lot_that_is_not_positive_fails_the_build(self):
        for lot in (0, -75):
            with self.subTest(lot=lot):
                with refdata.transaction() as conn:
                    conn.execute("DELETE FROM fo_eod")
                self.fo_row(DAY1, self.ce, 20_000, lot)                  # the row's own lot is wrong: no fallback
                with self.assertRaisesRegex(packs.PackError,
                                            rf"fo_eod 2026-11-16 instrument {self.ce} \(NFO:NIFTY26N2424000CE\): "
                                            rf"lot size {lot} \(fo_eod\.lot_size\) is not a positive whole number"):
                    packs.build(self.spec([self.ce]))

    def test_a_volume_that_is_not_a_whole_number_fails_the_build(self):
        self.fo_row(DAY1, self.ce, 20_000.5, 75)                         # was cut down to 20,000 without a word
        with self.assertRaisesRegex(packs.PackError,
                                    rf"fo_eod 2026-11-16 instrument {self.ce} \(NFO:NIFTY26N2424000CE\): "
                                    r"volume 20000\.5 is not a whole number of contracts"):
            packs.build(self.spec([self.ce]))

    def test_fo_eod_is_not_rewritten(self):
        self.fo_row(DAY1, self.ce, 20_000, 75, oi=300)
        packs.build(self.spec([self.ce]))
        row = refdata._connect().execute("SELECT volume, oi, lot_size FROM fo_eod").fetchone()
        self.assertEqual(tuple(row), (20_000, 300, 75))

    def test_equity_and_index_bars_are_packed_as_stored(self):
        t, start = timeutil.ist_ns(DAY1, "19:00"), timeutil.ist_ns(DAY1, "00:00")
        with refdata.transaction() as conn:
            conn.execute("INSERT INTO cm_eod (trade_date, instrument_id, series, open_paise, high_paise, low_paise, "
                         "close_paise, volume) VALUES ('2026-11-16', ?, 'EQ', 1, 4, 0, 3, 123456)", (self.rel,))
            conn.execute("INSERT INTO index_eod VALUES ('2026-11-16', ?, 5, 8, 4, 7)", (self.idx,))
        self.fo_row(DAY1, self.ce, 20_000, 75)                           # in refdata, but not selected
        sha = packs.build(self.spec([self.rel, self.idx]))
        bars = self.bars(sha)
        self.assertEqual(bars[("2026-11-16", self.rel)]["volume"], 123_456)                 # shares, as stored
        self.assertEqual((bars[("2026-11-16", self.idx)]["volume"], len(bars)), (0, 2))
        data = packs.read_bytes(packs.pack_path(sha))
        for iid, row in ((self.rel, (1, 4, 0, 3, 123_456, None)), (self.idx, (5, 8, 4, 7, None, None))):
            rec = packs.enc_bar(iid, t, 86400, start, *row)
            self.assertIn(rec[:16], data)                                # kind, size, instrument and t_event
            self.assertIn(rec[24:], data)                                # the payload, byte for byte

    def test_the_packs_identity_follows_the_volume_in_units(self):
        """A pack is named by its content hash: the same contracts under another lot are another pack."""
        out = []
        for lot in (75, 65):
            with refdata.transaction() as conn:
                conn.execute("DELETE FROM fo_eod")
            self.fo_row(DAY1, self.ce, 20_000, lot)
            sha = packs.build(self.spec([self.ce]))
            out.append((sha, packs.manifest(sha)["sources"][0]["rows_digest"]))
        self.assertNotEqual(out[0][0], out[1][0])
        self.assertNotEqual(out[0][1], out[1][1])


class RandomPairTests(FoVolumeTest):
    def test_random_contracts_and_lots_pack_as_their_exact_product(self):
        """Property: for any (contracts, lot) whose product fits an int64, the packed volume is contracts × lot."""
        rng = random.Random(7901)
        checked = 0
        for _ in range(25):
            with refdata.transaction() as conn:
                conn.execute("DELETE FROM fo_eod")
            want = {}
            for iid in self.pool:
                own = rng.random() < 0.7                                 # else NULL: the dated spec's 75
                lot = rng.choice([1, 15, 25, 65, 75, 1_800, rng.randint(1, 10**6)]) if own else 75
                pick = rng.random()
                if pick < 0.6:
                    contracts = rng.randint(0, 10**7)
                elif pick < 0.8:
                    contracts = INT64_MAX // lot - rng.randint(0, 10)    # at the edge of an int64
                else:
                    contracts = rng.randint(0, INT64_MAX // lot)
                self.fo_row(DAY1, iid, contracts, lot if own else None)
                want[iid] = contracts * lot
            bars = self.bars(packs.build(self.spec(self.pool)))
            for iid, units in want.items():
                got = bars[("2026-11-16", iid)]["volume"]
                self.assertIs(type(got), int)
                self.assertEqual(got, units)
                self.assertLessEqual(got, INT64_MAX)
                checked += 1
        self.assertEqual(checked, 25 * len(self.pool))

    def test_a_product_beyond_an_int64_fails_the_build(self):
        rng = random.Random(7902)
        for _ in range(20):
            lot = rng.choice([2, 75, 1_800, rng.randint(2, 10**6)])
            contracts = INT64_MAX // lot + rng.randint(1, 1_000)
            with refdata.transaction() as conn:
                conn.execute("DELETE FROM fo_eod")
            self.fo_row(DAY1, self.ce, contracts, lot)
            with self.subTest(contracts=contracts, lot=lot), self.assertRaisesRegex(
                    packs.PackError, rf"fo_eod 2026-11-16 instrument {self.ce} \(NFO:NIFTY26N2424000CE\): "
                                     rf"volume {contracts} contracts × lot {lot} does not fit an int64"):
                packs.build(self.spec([self.ce]))


def _fills(folder: Path) -> list[dict]:
    with (folder / "fills.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class RunnerTests(FoVolumeTest):
    """The audit's example (F2): 20,000 contracts of a lot-75 option is 15,00,000 units, so a 10 % participation
    limit allows 1,50,000 units and a BUY of 7,500 fills in full. Read as units, 20,000 allowed floor_to(2,000, 75)
    = 1,950."""

    def bundle(self, kind: str) -> tuple[dict, Path]:
        for day in (DAY1, DAY2):
            self.fo_row(day, self.ce, 20_000, 75)
        sha = packs.build(self.spec([self.ce], DAY1, DAY2))
        place = {"instrument": self.ce, "side": "BUY", "qty": 7_500, "product": "NRML", "type": "MARKET"}
        cfg = runconfig.load({"schema": "stockify.run/1", "name": f"fovolume_{kind}",
                              "strategy": {"kind": "script", "actions": [{"on_event": 1, "place": place}]},
                              "data": {"packs": [{"sha256": sha}]}, "capital_paise": 10**9, "price_basis": "raw",
                              "fill_model": {"kind": kind, "participation": "0.10"},
                              "costs": {"allow_unverified": True}})
        b = bundle_mod.resolve_bundle(cfg)
        self.assertEqual([s["lot"] for i in b["instruments"] if i["id"] == self.ce for s in i["specs"]], [75])
        return b, paths.lab_dir() / b["packs"][0]["path"]

    def test_the_oracle_fills_the_whole_order_within_ten_per_cent_of_the_bar(self):
        for kind in ("eod_close", "bar"):
            with self.subTest(fill_model=kind), tempfile.TemporaryDirectory(prefix="fo-volume-") as tmp:
                with refdata.transaction() as conn:
                    conn.execute("DELETE FROM fo_eod")
                b, pack = self.bundle(kind)
                oracle_run(b, [pack], Path(tmp) / "oracle", report=False)
                fills = _fills(Path(tmp) / "oracle")
                self.assertEqual(sum(int(f["qty"]) for f in fills), 7_500)

    @unittest.skipUnless(engine.available(), "stockify._engine is not built")
    def test_the_engine_fills_the_same_byte_for_byte(self):
        from tests.stockify.test_engine_parity import assert_same_artifacts
        for kind in ("eod_close", "bar"):
            with self.subTest(fill_model=kind), tempfile.TemporaryDirectory(prefix="fo-volume-") as tmp:
                with refdata.transaction() as conn:
                    conn.execute("DELETE FROM fo_eod")
                b, pack = self.bundle(kind)
                oracle_run(b, [pack], Path(tmp) / "oracle", report=False)
                engine.run_bundle(b, [pack], Path(tmp) / "engine", report=False)
                assert_same_artifacts(self, Path(tmp) / "oracle", Path(tmp) / "engine")
                self.assertEqual(sum(int(f["qty"]) for f in _fills(Path(tmp) / "engine")), 7_500)
                self.assertEqual((Path(tmp) / "engine" / "fills.csv").read_bytes(),
                                 (Path(tmp) / "oracle" / "fills.csv").read_bytes())


if __name__ == "__main__":
    unittest.main()
