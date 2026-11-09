"""MEZ 4.8: exact SPAN and NSE's ELM % checked against stored Kite basket-margin answers (synthetic rows, offline).

The archive holds the trimmed real 2026-11-09 i1 file (created 2026-11-08 20:26 IST) as the day's i1 zip and a small
ELM file. Every expected number is worked from the fixture (see test_span_exact.py for the SPAN arithmetic).
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import sqlite3
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import paths, timeutil
from stockify.data import instruments, refdata
from stockify.fno import span_validate as SV

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "span" / "nsccl.20261109.i1.trim.spn"
DAY = date(2026, 11, 9)
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
          "instrument_type,segment,exchange")
WALL = timeutil.ist_ns(DAY, "09:49")
SPOT_PAISE = 2494125                       # NIFTY 50 LTP 24,941.25
FUT_PAISE = 2495900                        # NIFTY 26NOV future LTP 24,959.00


def dumps() -> dict[str, str]:
    nfo = [HEADER,
           "9001,1,NIFTY26NOVFUT,NIFTY,0,2026-11-23,0,0.1,65,FUT,NFO-FUT,NFO",
           "9002,2,NIFTY26DECFUT,NIFTY,0,2026-12-29,0,0.1,65,FUT,NFO-FUT,NFO",
           "9003,3,NIFTY26NOV25000CE,NIFTY,0,2026-11-23,25000,0.05,65,CE,NFO-OPT,NFO",
           "9004,4,NIFTY26NOV25000PE,NIFTY,0,2026-11-23,25000,0.05,65,PE,NFO-OPT,NFO",
           "9005,5,NIFTY26NOV26000CE,NIFTY,0,2026-11-23,26000,0.05,65,CE,NFO-OPT,NFO"]
    return {"NSE": f"{HEADER}\n256265,1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n", "NFO": "\n".join(nfo) + "\n"}


ELM_CSV = ("OPTIDX,NIFTY,23-NOV-2026,25000,CE,0,2\n"
           "OPTIDX,NIFTY,23-NOV-2026,25000,PE,0,2\n"
           "FUTIDX,NIFTY,23-NOV-2026,-1,XX,0,2\n"
           "FUTIDX,NIFTY,29-DEC-2026,-1,XX,0,2\n")


def answer(span: str, exposure: str, orders: dict[str, str] | None = None) -> dict:
    return {"final": {"span": float(span), "exposure": float(exposure), "option_premium": 0, "additional": 0},
            "orders": [{"tradingsymbol": s, "exposure": float(e)} for s, e in (orders or {}).items()]}


class SpanValidateTests(SandboxedDBTest):
    db_modules = (store, refdata)

    def setUp(self):
        super().setUp()
        instruments.sync(DAY, dumps(), underlyings={"NIFTY": "NSE:NIFTY 50"})
        span_dir = paths.nse_margin_dir("span") / "2026"
        span_dir.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("nsccl.20261109.i1.spn", FIXTURE.read_bytes())
        (span_dir / "nsccl.20261109.i1.zip").write_bytes(buf.getvalue())
        elm_dir = paths.nse_margin_dir("elm") / "2026"
        elm_dir.mkdir(parents=True, exist_ok=True)
        (elm_dir / "F_AEL_OPT_CONTRACTS_09112026.csv.gz").write_bytes(gzip.compress(ELM_CSV.encode()))

    def add(self, kind: str, legs: list[tuple[str, int]], kite: dict, wall_ns: int = WALL) -> int:
        market = {"underlying": {"ltp": SPOT_PAISE, "bid": None, "ask": None, "oi": None}}
        orders = []
        for sym, units in legs:
            iid = instruments.find("NFO", sym)
            market[str(iid)] = {"ltp": FUT_PAISE if sym.endswith("FUT") else 30000, "bid": None, "ask": None,
                                "oi": None}
            orders.append({"exchange": "NFO", "tradingsymbol": sym, "quantity": abs(units),
                           "transaction_type": "BUY" if units > 0 else "SELL", "product": "NRML"})
        return store.add_margin_calibration({
            "wall_ns": wall_ns, "underlying": "NIFTY",
            "portfolio_json": json.dumps({"kind": kind, "legs": orders}), "market_json": json.dumps(market),
            "kite_json": json.dumps(kite), "t0_paise": 0, "t1_paise": 0, "t1_breakdown_json": "[]"})

    # Short straddle 25000 23-Nov, 65 each: scan 110,867.25, short option value 43,875.00 -> span 154,742.25.
    # Exposure: 130 short option units x spot 24,941.25 x 2 % = 64,847.25.
    STRADDLE = [("NIFTY26NOV25000CE", -65), ("NIFTY26NOV25000PE", -65)]

    def test_one_matching_and_one_off_sample_fail_the_gate(self):
        self.add("straddle", self.STRADDLE, answer("154742.25", "64847.25"))
        self.add("straddle", self.STRADDLE, answer("153742.25", "64847.25"))     # ₹1,000 off: beyond ₹773.71
        rep = SV.validate(DAY, ["NIFTY"])
        self.assertEqual(len(rep.samples), 2)
        good, bad = rep.samples
        self.assertEqual((good.kite_span, good.our_span), (Decimal("154742.25"), Decimal("154742.25")))
        self.assertEqual((good.kite_exposure, good.our_exposure), (Decimal("64847.25"), Decimal("64847.25")))
        self.assertEqual(good.file, "nsccl.20261109.i1.zip")
        self.assertEqual(rep.span_within("with_nov"), (1, 2))
        self.assertEqual(rep.exposure_within(), (2, 2))
        self.assertFalse(rep.gate()["passed"])
        self.assertFalse(rep.gate()["span_passed"])

    def test_all_matching_passes_the_gate(self):
        self.add("straddle", self.STRADDLE, answer("154742.25", "64847.25"))
        self.assertTrue(SV.validate(DAY, ["NIFTY"]).gate()["passed"])

    def test_both_net_option_value_compositions_are_reported(self):
        # Long 65 CE 25000: scan 22,646.00, long option value 22,646.00 -> 0 with NOV, 22,646.00 without.
        self.add("single", [("NIFTY26NOV25000CE", 65)], answer("0", "0"))
        self.add("straddle", self.STRADDLE, answer("154742.25", "64847.25"))
        rep = SV.validate(DAY, ["NIFTY"])
        long_call, straddle = rep.samples
        self.assertEqual((long_call.our_span, long_call.our_span_no_nov), (Decimal(0), Decimal("22646.00")))
        self.assertEqual(straddle.our_span_no_nov, Decimal("110867.25"))
        self.assertEqual(rep.span_within("with_nov"), (2, 2))
        self.assertEqual(rep.span_within("no_nov"), (0, 2))
        self.assertEqual(long_call.our_exposure, Decimal(0))              # bought options carry no exposure

    def test_futures_exposure_is_units_times_the_futures_price(self):
        # Long 65 Nov future: 65 x 24,959.00 x 2 % = 32,446.70. Scan 2,339.32 x 65 = 152,055.80.
        self.add("future", [("NIFTY26NOVFUT", 65)], answer("152055.8", "32446.7"))
        s = SV.validate(DAY, ["NIFTY"]).samples[0]
        self.assertEqual(s.our_exposure, Decimal("32446.70"))
        self.assertEqual(s.our_span, Decimal("152055.80"))

    def test_the_kite_exposure_convention_check(self):
        # Kite's per-order exposure / (units x ELM %) must be one price on the 0.05 tick for a sample's options.
        self.add("straddle", self.STRADDLE, answer("154742.25", "64850", {"NIFTY26NOV25000CE": "32425",
                                                                         "NIFTY26NOV25000PE": "32425"}))
        self.add("straddle", self.STRADDLE, answer("154742.25", "64850", {"NIFTY26NOV25000CE": "32425.01",
                                                                         "NIFTY26NOV25000PE": "32424.99"}))
        a, b = SV.validate(DAY, ["NIFTY"]).samples
        self.assertIs(a.convention_ok, False)                     # 32425 / 65 / 0.02 = 24,942.307...: off tick
        self.add("straddle", self.STRADDLE, answer("154742.25", "64847.25", {"NIFTY26NOV25000CE": "32423.625",
                                                                            "NIFTY26NOV25000PE": "32423.625"}))
        c = SV.validate(DAY, ["NIFTY"]).samples[2]
        self.assertEqual(c.kite_implied_prices, {"OPT": Decimal("24941.25")})
        self.assertTrue(c.convention_ok)
        self.assertFalse(b.convention_ok)                         # legs disagree

    def test_no_file_in_force_before_the_first_one_was_created(self):
        # i1 was created 2026-11-08 20:26 IST; a row stamped before it has no file (no lookahead), and is a miss.
        early = timeutil.ist_ns(date(2026, 11, 8), "20:00")
        self.add("straddle", self.STRADDLE, answer("154742.25", "64847.25"), wall_ns=early)
        rep = SV.validate(DAY, ["NIFTY"])
        self.assertEqual(rep.samples, [])                          # not that IST trade date
        rep = SV.validate(date(2026, 11, 8), ["NIFTY"])
        self.assertIsNone(rep.samples[0].file)
        self.assertIn("no SPAN file", rep.samples[0].error)
        self.assertEqual(rep.span_within("with_nov"), (0, 1))

    def test_file_choice_takes_the_latest_created_at_or_before_the_row(self):
        files = [("a.i1.zip", 100), ("a.i2.zip", 200), ("a.i3.zip", 300)]
        self.assertEqual(SV.file_in_force(files, 250), ("a.i2.zip", 200))
        self.assertEqual(SV.file_in_force(files, 200), ("a.i2.zip", 200))
        self.assertEqual(SV.file_in_force(files, 999), ("a.i3.zip", 300))
        self.assertIsNone(SV.file_in_force(files, 99))

    def test_tolerance_is_fifty_rupees_or_half_a_percent(self):
        self.assertTrue(SV.span_ok(Decimal("1050"), Decimal("1000")))
        self.assertFalse(SV.span_ok(Decimal("1050.01"), Decimal("1000")))
        self.assertTrue(SV.span_ok(Decimal("100500"), Decimal("100000")))
        self.assertFalse(SV.span_ok(Decimal("100500.01"), Decimal("100000")))
        self.assertTrue(SV.exposure_ok(Decimal("101"), Decimal("100")))
        self.assertFalse(SV.exposure_ok(Decimal("101.01"), Decimal("100")))

    def test_the_lab_databases_are_only_read(self):
        self.add("straddle", self.STRADDLE, answer("154742.25", "64847.25"))
        own_store, own_refdata = store._conn, refdata._conn
        with SV.read_only():
            self.assertEqual(len(store.margin_calibrations("NIFTY")), 1)
            self.assertIsNotNone(instruments.find("NFO", "NIFTY26NOVFUT"))
            for conn in (store._conn, refdata._conn):
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("CREATE TABLE never_written (x)")
        self.assertIs(store._conn, own_store)                      # the modules' own connections come back
        self.assertIs(refdata._conn, own_refdata)

    def test_cli_writes_the_report_beside_the_calibration_reports(self):
        from stockify.__main__ import main
        self.add("straddle", self.STRADDLE, answer("154742.25", "64847.25"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["margin", "span-validate", "--date", "2026-11-09"])
        self.assertIn("NIFTY", out.getvalue())
        self.assertEqual(code, 0)
        report = paths.lab_dir() / "margin_calibration" / "span-validate-2026-11-09.md"
        text = report.read_text(encoding="utf-8")
        self.assertIn("1/1", text)
        self.assertIn("PASSED", text)

