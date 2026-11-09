"""MEZ 4.11: `margin check` (a basket priced by the staircase from the archived NSE files, offline) and
`margin scorecard` (every step against every stored Kite answer).

The archive holds the trimmed real 2026-11-09 i1 file (created 2026-11-08 20:26 IST) as the day's i1 zip and a small
ELM file, as in test_span_validate.py. Expected numbers are worked from the fixture.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import zipfile
from datetime import date
from fractions import Fraction
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import __main__ as cli
from stockify import paths, timeutil
from stockify.data import instruments, refdata
from stockify.fno import margin_check as MC

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "span" / "nsccl.20261109.i1.trim.spn"
DAY = date(2026, 11, 9)
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
          "instrument_type,segment,exchange")
WALL = timeutil.ist_ns(DAY, "09:49")
SPOT_PAISE = 2494125                       # NIFTY 50 LTP 24,941.25 in the stored snapshot
FUT_PAISE = 2495900

ELM_CSV = ("OPTIDX,NIFTY,23-NOV-2026,25000,CE,0,2\n"
           "OPTIDX,NIFTY,23-NOV-2026,25000,PE,0,2\n"
           "FUTIDX,NIFTY,23-NOV-2026,-1,XX,0,2\n")


def dumps() -> dict[str, str]:
    nfo = [HEADER,
           "9001,1,NIFTY26NOVFUT,NIFTY,0,2026-11-23,0,0.1,65,FUT,NFO-FUT,NFO",
           "9003,3,NIFTY26NOV25000CE,NIFTY,0,2026-11-23,25000,0.05,65,CE,NFO-OPT,NFO",
           "9004,4,NIFTY26NOV25000PE,NIFTY,0,2026-11-23,25000,0.05,65,PE,NFO-OPT,NFO"]
    return {"NSE": f"{HEADER}\n256265,1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n", "NFO": "\n".join(nfo) + "\n"}


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class ArchiveTest(SandboxedDBTest):
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


class MarginCheckTests(ArchiveTest):
    STRADDLE = ["--leg", "SELL 65 NIFTY26NOV25000CE", "--leg", "SELL 65 NIFTY26NOV25000PE"]

    def test_check_prints_rupees_and_step(self):
        # Short straddle 25000 23-Nov, 65 each, priced by the i1 file (in force from 2026-11-08 20:26):
        # SPAN 154,742.25 -> ₹1,54,743 (up to the rupee); exposure 130 x the file's spot 24,986.40 x 2 % =
        # 64,964.64 -> ₹64,965; total ₹2,19,708.
        code, out, err = run_cli(["margin", "check", *self.STRADDLE, "--date", "2026-11-09", "--at", "10:00"])
        self.assertEqual(code, 0, err)
        self.assertIn("B1_span_nse_elm", out)
        self.assertIn("₹1,54,743.00", out)
        self.assertIn("₹64,965.00", out)
        self.assertIn("₹2,19,708.00", out)
        self.assertIn("nsccl.20261109.i1.zip", out)

    def test_check_defaults_to_the_latest_archived_day(self):
        self.assertEqual(MC.latest_archived_day(), DAY)
        code, out, err = run_cli(["margin", "check", *self.STRADDLE, "--at", "10:00"])
        self.assertEqual(code, 0, err)
        self.assertIn("2026-11-09", out)

    def test_check_never_uses_a_file_created_later(self):
        # The manifest says the day's only file was created at 10:30 IST: at 10:00 nothing is in force yet.
        (paths.nse_margin_dir("manifest") / "20261109.json").write_text(
            json.dumps({"nsccl.20261109.i1.zip": {"created": "202611091030"}}), encoding="utf-8")
        with self.assertRaises(MC.CheckError) as ctx:
            MC.check([("SELL", 65, "NIFTY26NOV25000CE")], DAY, "10:00")
        self.assertIn("no SPAN file in force", str(ctx.exception))
        self.assertEqual(MC.check([("SELL", 65, "NIFTY26NOV25000CE")], DAY, "10:30").file,
                         "nsccl.20261109.i1.zip")

    def test_check_rejects_unknown_symbol(self):
        code, out, err = run_cli(["margin", "check", "--leg", "SELL 65 NIFTY26NOV99999CE", "--date", "2026-11-09"])
        self.assertNotEqual(code, 0)
        self.assertIn("NIFTY26NOV99999CE", err)
        self.assertIn("refdata", err)
        self.assertEqual(out, "")

    def test_check_rejects_a_malformed_leg(self):
        code, _, err = run_cli(["margin", "check", "--leg", "SHORT 65 NIFTY26NOV25000CE", "--date", "2026-11-09"])
        self.assertEqual(code, 2)
        self.assertIn("BUY or SELL", err)


def answer(span: str, exposure: str) -> dict:
    return {"final": {"span": float(span), "exposure": float(exposure), "option_premium": 0, "additional": 0},
            "orders": []}


class ScorecardTests(ArchiveTest):
    def add(self, kind: str, legs: list[tuple[str, int]], kite: dict) -> int:
        market = {"underlying": {"ltp": SPOT_PAISE, "bid": None, "ask": None, "oi": None}}
        orders = []
        for sym, units in legs:
            iid = instruments.find("NFO", sym)
            market[str(iid)] = {"ltp": FUT_PAISE if sym.endswith("FUT") else 30000, "bid": None, "ask": None,
                                "oi": None}
            orders.append({"exchange": "NFO", "tradingsymbol": sym, "quantity": abs(units),
                           "transaction_type": "BUY" if units > 0 else "SELL", "product": "NRML"})
        return store.add_margin_calibration({
            "wall_ns": WALL, "underlying": "NIFTY",
            "portfolio_json": json.dumps({"kind": kind, "legs": orders}), "market_json": json.dumps(market),
            "kite_json": json.dumps(kite), "t0_paise": 0, "t1_paise": 0, "t1_breakdown_json": "[]"})

    def test_scorecard_prices_every_step(self):
        # SPAN 154,742.25 (Kite's), exposure 130 x 24,941.25 x 2 % = 64,847.25 (Kite's): B1 is ₹1,54,743 +
        # ₹64,848 = ₹2,19,591, ₹1.50 above Kite's ₹2,19,589.50 (each part rounded up to the rupee).
        self.add("straddle", [("NIFTY26NOV25000CE", -65), ("NIFTY26NOV25000PE", -65)],
                 answer("154742.25", "64847.25"))
        card = MC.scorecard()
        [row] = card.rows
        self.assertEqual(row.kite_total, 21_958_950)
        self.assertEqual(row.steps["B1_span_nse_elm"].total, 21_959_100)
        self.assertEqual(row.steps["B1_span_nse_elm"].span, 15_474_300)
        self.assertEqual(row.steps["B1_span_nse_elm"].exposure, 6_484_800)
        self.assertEqual(row.steps["B2_span_rule_elm"].span, 15_474_300)
        self.assertIn("B4_spanlite_placeholder", row.steps)
        self.assertIn("B5_flat", row.steps)
        self.assertNotIn("B3_spanlite_calibrated", row.steps)          # no calibrated row is adopted
        text = MC.scorecard_text(card)
        self.assertIn("price drift", text)
        self.assertIn("B3", text)

    def test_scorecard_flags_inversion(self):
        def row(n: int, b1: int, b4: int) -> MC.ScoreRow:
            return MC.ScoreRow(n, "NIFTY", "straddle", DAY, 100_000, 60_000, 40_000,
                               {"B1_span_nse_elm": MC.StepValue(b1, None, None),
                                "B4_spanlite_placeholder": MC.StepValue(b4, None, None)})
        card = MC.Scorecard([row(1, 110_000, 101_000), row(2, 90_000, 99_000), row(3, 120_000, 100_500)], [])
        [inv] = card.inversions()
        self.assertEqual((inv.group, inv.better, inv.worse), (("NIFTY", "straddle"), "B4_spanlite_placeholder",
                                                             "B1_span_nse_elm"))
        text = MC.scorecard_text(card)
        self.assertIn("Inversion", text)
        self.assertIn("B4", text)

    def test_no_inversion_when_the_higher_step_is_closer(self):
        row = MC.ScoreRow(1, "NIFTY", "straddle", DAY, 100_000, 60_000, 40_000,
                          {"B1_span_nse_elm": MC.StepValue(100_100, None, None),
                           "B5_flat": MC.StepValue(150_000, None, None)})
        self.assertEqual(MC.Scorecard([row], []).inversions(), [])

    def test_quantile_interpolates_between_ranks(self):
        # rank (n - 1) q: [0, 10, 20, 30] -> median at 1.5 = 15, p90 at 2.7 = 27.
        self.assertEqual(MC.quantile([30, 0, 20, 10], Fraction(1, 2)), Fraction(15))
        self.assertEqual(MC.quantile([30, 0, 20, 10], Fraction(9, 10)), Fraction(27))
        self.assertIsNone(MC.quantile([], Fraction(1, 2)))
