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
            z.writestr(zipfile.ZipInfo("nsccl.20261109.i1.spn", date_time=(2026, 11, 9, 0, 0, 0)),
                       FIXTURE.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        (span_dir / "nsccl.20261109.i1.zip").write_bytes(buf.getvalue())
        elm_dir = paths.nse_margin_dir("elm") / "2026"
        elm_dir.mkdir(parents=True, exist_ok=True)
        (elm_dir / "F_AEL_OPT_CONTRACTS_09112026.csv.gz").write_bytes(gzip.compress(ELM_CSV.encode(), mtime=0))

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

    def add_future(self) -> int:
        """A future in the same run: its snapshot price (24,959.00) is the run's futures reference."""
        return self.add("future", [("NIFTY26NOVFUT", 65)], answer("152055.8", "32446.7", {"NIFTY26NOVFUT": "32446.7"}))

    def test_the_kite_exposure_convention_check(self):
        # Kite's per-order exposure / (units x ELM %) must be one price per group, on the 0.05 tick, near the right
        # snapshot price (index for sold options, future for futures), and the orders must add up to final.exposure.
        self.add_future()
        self.add("straddle", self.STRADDLE, answer("154742.25", "64850", {"NIFTY26NOV25000CE": "32425",
                                                                         "NIFTY26NOV25000PE": "32425"}))
        self.add("straddle", self.STRADDLE, answer("154742.25", "64847.25", {"NIFTY26NOV25000CE": "32423.625",
                                                                            "NIFTY26NOV25000PE": "32423.625"}))
        fut, off_tick, at_spot = SV.validate(DAY, ["NIFTY"]).samples
        self.assertIs(off_tick.convention_ok, False)              # 32425 / 65 / 0.02 = 24,942.307...: off tick
        self.assertEqual(at_spot.kite_implied_prices, {"OPT": Decimal("24941.25")})
        self.assertIs(at_spot.convention_ok, True)                # the index price exactly
        self.assertEqual(fut.kite_implied_prices, {"FUT": Decimal("24959")})
        self.assertIs(fut.convention_ok, True)                    # the future's own price

    def test_option_exposure_on_the_futures_price_fails_the_convention(self):
        # 65 x 24,959.00 x 2 % = 32,446.70 per leg: on the tick, legs agree, within 0.2 % of the index (24,941.25),
        # but nearer the future (24,959.00) than the index -> not Kite's index convention.
        self.add_future()
        self.add("straddle", self.STRADDLE, answer("154742.25", "64893.4", {"NIFTY26NOV25000CE": "32446.7",
                                                                           "NIFTY26NOV25000PE": "32446.7"}))
        s = SV.validate(DAY, ["NIFTY"]).samples[1]
        self.assertEqual(s.kite_implied_prices, {"OPT": Decimal("24959")})
        self.assertIs(s.convention_ok, False)

    def test_futures_exposure_on_the_index_price_fails_the_convention(self):
        # 65 x 24,941.25 x 2 % = 32,423.625: nearer the index than the future's own price.
        self.add("future", [("NIFTY26NOVFUT", 65)], answer("152055.8", "32423.625", {"NIFTY26NOVFUT": "32423.625"}))
        self.assertIs(SV.validate(DAY, ["NIFTY"]).samples[0].convention_ok, False)

    def test_option_price_far_from_the_index_fails_the_convention(self):
        # Implied 25,000.00 (the strike; 32,500 / 65 / 0.02): on the tick, but 0.24 % from the index, beyond 0.2 %.
        self.add_future()
        self.add("straddle", self.STRADDLE, answer("154742.25", "65000", {"NIFTY26NOV25000CE": "32500",
                                                                         "NIFTY26NOV25000PE": "32500"}))
        self.assertIs(SV.validate(DAY, ["NIFTY"]).samples[1].convention_ok, False)

    def test_legs_on_the_tick_that_disagree_fail_the_convention(self):
        # 24,941.25 and 24,942.25: both on the tick and near the index, but not one price.
        self.add_future()
        self.add("straddle", self.STRADDLE, answer("154742.25", "64848.55", {"NIFTY26NOV25000CE": "32423.625",
                                                                            "NIFTY26NOV25000PE": "32424.925"}))
        s = SV.validate(DAY, ["NIFTY"]).samples[1]
        self.assertEqual(s.kite_implied_prices, {})
        self.assertIs(s.convention_ok, False)

    def test_orders_that_do_not_add_up_to_the_final_exposure_fail_the_convention(self):
        self.add_future()
        self.add("straddle", self.STRADDLE, answer("154742.25", "64850", {"NIFTY26NOV25000CE": "32423.625",
                                                                         "NIFTY26NOV25000PE": "32423.625"}))
        self.assertIs(SV.validate(DAY, ["NIFTY"]).samples[1].convention_ok, False)

    def test_without_a_futures_price_in_the_run_options_cannot_pass_the_convention(self):
        # Index or future cannot be told apart with no future in the run: the check fails, it is not assumed.
        self.add("straddle", self.STRADDLE, answer("154742.25", "64847.25", {"NIFTY26NOV25000CE": "32423.625",
                                                                            "NIFTY26NOV25000PE": "32423.625"}))
        s = SV.validate(DAY, ["NIFTY"]).samples[0]
        self.assertIs(s.convention_ok, False)
        self.assertIn("no futures price", s.error)

    def test_a_missing_kite_figure_is_a_miss_not_zero(self):
        # A long call is 0 on our side; a Kite answer without final.span / final.exposure must not "match" it.
        self.add("single", [("NIFTY26NOV25000CE", 65)], {"final": {}, "orders": []})
        rep = SV.validate(DAY, ["NIFTY"])
        s = rep.samples[0]
        self.assertIsNone(s.kite_span)
        self.assertIsNone(s.kite_exposure)
        self.assertIn("Kite's answer has no final.span", s.error)
        self.assertEqual(rep.span_within("with_nov"), (0, 1))
        self.assertEqual(rep.exposure_within(), (0, 1))
        self.assertFalse(rep.gate()["passed"])

    def test_exposure_alone_can_fail_the_gate(self):
        self.add("straddle", self.STRADDLE, answer("154742.25", "64852.25"))     # exposure 5 rupees off
        g = SV.validate(DAY, ["NIFTY"]).gate()
        self.assertEqual((g["span_passed"], g["exposure_passed"], g["passed"]), (True, False, False))

    def test_a_bought_leg_missing_from_the_elm_file_carries_no_exposure(self):
        # NIFTY26NOV26000CE is in refdata but neither in the ELM nor in the trimmed SPAN file.
        self.add("single", [("NIFTY26NOV26000CE", 65)], answer("0", "0"))
        s = SV.validate(DAY, ["NIFTY"]).samples[0]
        self.assertEqual(s.our_exposure, Decimal(0))
        self.assertIsNone(s.our_span)                              # missing from the SPAN file: a miss, never 0

    def test_malformed_and_conflicting_elm_rows_are_not_used(self):
        path = paths.nse_margin_dir("elm") / "2026" / "F_AEL_OPT_CONTRACTS_09112026.csv.gz"
        path.write_bytes(gzip.compress((ELM_CSV + "OPTIDX,NIFTY,BAD-DATE,25000,CE,0,2\n"
                                        "OPTIDX,NIFTY,23-NOV-2026,abc,CE,0,x\n"
                                        "OPTIDX,NIFTY,23-NOV-2026,25000,PE,0,3\n").encode(), mtime=0))
        elm = SV.read_elm(DAY, {"NIFTY"})
        self.assertEqual(elm[("NIFTY", ("CE", "2026-11-23", 2500000))], Decimal(2))
        self.assertIsNone(elm[("NIFTY", ("PE", "2026-11-23", 2500000))])      # 2 and 3: conflicting, unusable
        self.assertEqual(elm[("NIFTY", ("FUT", "2026-11-23", None))], Decimal(2))

    def manifest(self, day: str, name: str, created: str) -> None:
        path = paths.nse_margin_dir("manifest") / f"{day}.json"
        doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        doc[name] = {"created": created}
        path.write_text(json.dumps(doc), encoding="utf-8")

    def test_no_file_in_force_when_the_days_files_were_created_after_the_row(self):
        # The day's i1 is archived but (by its manifest) created at 10:00 IST; the row is 09:49 -> no file in force
        # (no lookahead), and no previous end-of-day file is archived -> a miss.
        self.manifest("20261109", "nsccl.20261109.i1.zip", "202611091000")
        self.add("straddle", self.STRADDLE, answer("154742.25", "64847.25"))
        rep = SV.validate(DAY, ["NIFTY"])
        self.assertIsNone(rep.samples[0].file)
        self.assertIn("no SPAN file", rep.samples[0].error)
        self.assertEqual(rep.span_within("with_nov"), (0, 1))

    def test_previous_trading_days_end_of_day_file_when_none_of_the_days_files_is_in_force(self):
        # Spec 5.1: no D file in force -> the previous trading day's .s (Fri 6 Nov before Mon 9 Nov).
        self.manifest("20261109", "nsccl.20261109.i1.zip", "202611091000")
        span_dir = paths.nse_margin_dir("span") / "2026"
        (span_dir / "nsccl.20261106.s.zip").write_bytes((span_dir / "nsccl.20261109.i1.zip").read_bytes())
        self.manifest("20261106", "nsccl.20261106.s.zip", "202611061700")
        self.add("straddle", self.STRADDLE, answer("154742.25", "64847.25"))
        s = SV.validate(DAY, ["NIFTY"]).samples[0]
        self.assertEqual(s.file, "nsccl.20261106.s.zip")
        self.assertEqual(s.our_span, Decimal("154742.25"))

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
            for conn in (store._conn, refdata._connect()):
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

    def test_cli_exits_non_zero_when_the_gate_fails(self):
        from stockify.__main__ import main
        self.add("straddle", self.STRADDLE, answer("153742.25", "64847.25"))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["margin", "span-validate", "--date", "2026-11-09"])
        self.assertEqual(code, 1)
        report = paths.lab_dir() / "margin_calibration" / "span-validate-2026-11-09.md"
        self.assertIn("FAILED", report.read_text(encoding="utf-8"))   # the report is still written

    def test_cli_prints_rs_on_a_cp1252_console(self):
        from stockify.__main__ import main
        self.add("straddle", self.STRADDLE, answer("154742.25", "64847.25"))
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp1252")
        with contextlib.redirect_stdout(console), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["margin", "span-validate", "--date", "2026-11-09"]), 0)
        console.flush()
        text = raw.getvalue().decode("cp1252")
        self.assertIn("Rs 50", text)
        self.assertNotIn("?", text)
        report = paths.lab_dir() / "margin_calibration" / "span-validate-2026-11-09.md"
        self.assertIn("\u20b950", report.read_text(encoding="utf-8"))  # the file keeps the rupee sign
