"""M4.9: the SPAN-lite calibration tool against a FAKE Kite client (no Kite call is ever made; kiteconnect is absent).

The fake prices options with Black-76 on a small smile, and answers basket margins with what SPAN-lite gives
under known "true" settings, so the fit has something to find. It has no order methods at all.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify.data import history, instruments, refdata
from stockify.fno import black76 as B
from stockify.fno import calibrate as CAL

DAY = "2026-11-09"
E1, E2 = "2026-11-11", "2026-11-17"      # trading days (10 and 24 Nov are NSE holidays)
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
          "instrument_type,segment,exchange")
SPOT = 25_000.0
TRUE = {"psr": 0.08, "vsr": 0.30, "somc": 300_000}
TOKEN = "fake-access-token-do-not-store"


def dumps() -> dict[str, str]:
    nfo = [HEADER, "9001,1,NIFTY26NOVFUT,NIFTY,0,2026-11-26,0,0.1,75,FUT,NFO-FUT,NFO"]
    tok = 10_000
    for exp, tag in ((E1, "26N11"), (E2, "26N17")):
        for k in range(24_000, 26_001, 100):
            for kind in ("CE", "PE"):
                tok += 1
                nfo.append(f"{tok},{tok},NIFTY{tag}{k}{kind},NIFTY,0,{exp},{k},0.05,75,{kind},NFO-OPT,NFO")
    return {"NSE": f"{HEADER}\n256265,1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n", "NFO": "\n".join(nfo) + "\n"}


class FakeKite:
    """Quotes and basket margins only. Any other attribute (place_order, …) raises."""

    access_token = TOKEN

    def __init__(self):
        self.calls: list[tuple] = []
        self.snapshot = None
        self.pricer = None

    def __getattr__(self, name: str):  # noqa: ANN204
        raise AssertionError(f"the calibration tool must not use Kite's {name}")

    def _price(self, sym: str) -> float:
        info = instruments.get(instruments.find(sym.split(":")[0], sym.split(":")[1]))
        if info["kind"] == "FUT":
            return SPOT + 20
        if info["kind"] not in ("CE", "PE"):
            return SPOT
        k = info["strike_paise"] / 100
        t = (date.fromisoformat(info["expiry"]) - date.fromisoformat(DAY)).days / 365 + 0.2 / 365
        sigma = 0.12 + 0.5 * ((k - SPOT) / SPOT) ** 2
        return max(0.05, B.price(info["kind"] == "CE", SPOT, k, t, 0.065, sigma))

    def quote(self, keys: list[str]) -> dict:
        self.calls.append(("quote", len(keys)))
        out = {}
        for key in keys:
            px = round(self._price(key) / 0.05) * 0.05
            out[key] = {"last_price": round(px, 2), "oi": 1000,
                        "depth": {"buy": [{"price": round(max(0.05, px - 0.5), 2), "quantity": 75}],
                                  "sell": [{"price": round(px + 0.5, 2), "quantity": 75}]}}
        return out

    def basket_order_margins(self, orders: list[dict], consider_positions: bool = True, mode=None) -> dict:  # noqa: ANN001
        assert consider_positions is False, "calibration prices portfolios alone"
        self.calls.append(("margins", len(orders)))
        by_sym = {r["tradingsymbol"]: i for i, r in self.snapshot.rows.items()}
        legs = [(by_sym[o["tradingsymbol"]], o["quantity"] * (1 if o["transaction_type"] == "BUY" else -1))
                for o in orders]
        row = CAL.with_params(self.base, TRUE["psr"], TRUE["vsr"], TRUE["somc"])
        final = self.pricer.t1(CAL.Portfolio("x", legs), row)[0] / 100
        return {"initial": {"total": final * 1.1}, "final": {"total": final}, "orders": [{"total": 0}] * len(orders)}


class CalibrationTests(SandboxedDBTest):
    db_modules = (store, refdata, history)

    def setUp(self):
        super().setUp()
        instruments.sync(date(2026, 11, 9), dumps(), underlyings={"NIFTY": "NSE:NIFTY 50"})
        self.sleeps: list[float] = []
        self.kite = FakeKite()

    def snapshot(self) -> CAL.Snapshot:
        snap = CAL.build_snapshot(self.kite, "NIFTY", DAY, strikes=6, sleep=self.sleeps.append,
                                  now_ns=lambda: 1_794_180_600_000_000_000)
        margin, pricing = CAL._config_params()
        self.kite.snapshot, self.kite.base = snap, CAL.class_row(margin, snap)
        self.kite.pricer = CAL.Pricer(snap, margin, pricing)
        return snap

    def test_the_snapshot(self):
        snap = self.snapshot()
        kinds = [r["kind"] for r in snap.rows.values()]
        self.assertEqual(kinds.count("INDEX"), 1)
        self.assertEqual(kinds.count("FUT"), 1)
        self.assertEqual(snap.expiries, [E1, E2])
        for e in (E1, E2):
            strikes = sorted({r["strike_paise"] for r in snap.options(e)})
            self.assertEqual(len(strikes), 13)                     # ATM ± 6
            self.assertIn(2_500_000, strikes)
        self.assertEqual(snap.quotes[snap.index_id]["ltp"], 2_500_000)
        self.assertTrue(all(q["bid"] < q["ask"] for i, q in snap.quotes.items() if snap.rows[i]["kind"] in ("CE", "PE")))
        self.assertTrue(all(r["last_trading_ns"] for r in snap.rows.values() if r["kind"] in ("CE", "PE", "FUT")))
        self.assertEqual([c[0] for c in self.kite.calls], ["quote", "quote"])
        self.assertEqual(self.sleeps, [CAL.QUOTE_INTERVAL_S])     # ≤ 1 quote request per second (K-8)

    def test_portfolios_cover_every_type_and_are_seeded(self):
        snap = self.snapshot()
        ps = CAL.sample_portfolios(snap, 40, seed=3)
        self.assertEqual({p.kind for p in ps}, set(CAL.TYPES))
        self.assertEqual([(p.kind, p.legs) for p in ps], [(p.kind, p.legs) for p in CAL.sample_portfolios(snap, 40, 3)])
        fly = next(p for p in ps if p.kind == "iron_fly")
        self.assertEqual(sorted(u > 0 for _, u in fly.legs), [False, False, True, True])
        for p in ps:
            for o in CAL.kite_orders(snap, p):
                self.assertEqual((o["product"], o["order_type"], o["variety"]), ("NRML", "MARKET", "regular"))
                self.assertGreater(o["quantity"], 0)

    def test_calibrate_end_to_end(self):
        snap = self.snapshot()
        tmp = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))
        params = tmp / "margin_params.json"
        from stockify.config import loader
        shutil.copy(Path(loader.__file__).with_name("margin_params.json"), params)
        report, written = CAL.calibrate(self.kite, "NIFTY", 24, seed=1, day=DAY, sleep=self.sleeps.append,
                                        snapshot=snap, params_path=params)
        self.assertEqual(written, params)
        rows = store.margin_calibrations("NIFTY")
        self.assertEqual(len(rows), sum(1 for c in self.kite.calls if c[0] == "margins"))
        self.assertGreaterEqual(len(rows), 20)
        self.assertTrue(all(json.loads(r["kite_json"])["final"]["total"] >= 0 for r in rows))
        self.assertEqual(self.sleeps.count(CAL.MARGIN_INTERVAL_S), len(rows))   # ≤ 10 margin requests per second
        # the fit finds the "true" settings far better than the placeholders do
        text = report.read_text(encoding="utf-8")
        doc = json.loads(params.read_text(encoding="utf-8"))
        [pending] = doc["spanlite_pending"]
        self.assertTrue(pending["pending_review"])
        self.assertFalse(pending["verified"])
        self.assertEqual((pending["source"], pending["effective_from"]), (f"calibration {DAY}", DAY))
        self.assertLess(float(pending["mean_relative_error"]), 0.03)
        self.assertLess(abs(float(pending["psr_pct"]) - TRUE["psr"]), 0.01)
        self.assertIn("NOT used until you review", text)
        self.assertIn("short straddles", text)
        # the adopted rows are untouched: the fit is never used automatically
        self.assertEqual(doc["spanlite"], json.loads(Path(loader.__file__).with_name("margin_params.json")
                                                     .read_text(encoding="utf-8"))["spanlite"])
        # nothing about the login is stored anywhere
        blob = json.dumps(rows) + text + params.read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, blob)

    def test_pending_rows_are_never_read_by_the_models(self):
        snap = self.snapshot()
        margin, pricing = CAL._config_params()
        margin["spanlite_pending"] = [{"applies": {"underlying": "NIFTY"}, "effective_from": "2015-01-01",
                                       "psr_pct": "0.99", "pending_review": True}]
        base = CAL.class_row(margin, snap)
        self.assertNotEqual(base.get("psr_pct"), "0.99")
        pricer = CAL.Pricer(snap, margin, pricing)
        p = CAL.sample_portfolios(snap, 2, seed=0)[1]                 # a straddle
        self.assertEqual(pricer.t1(p, base)[0], CAL.Pricer(snap, CAL._config_params()[0], pricing).t1(p, base)[0])

    def test_no_kite_when_not_logged_in(self):
        from stockify import __main__ as cli
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["margin", "calibrate", "--underlying", "NIFTY", "--samples", "5"])
        self.assertEqual(code, 2)                                      # kiteconnect is absent in tests
        self.assertIn("Kite", err.getvalue())


if __name__ == "__main__":
    unittest.main()
