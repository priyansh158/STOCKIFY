"""M3.7: the C++ cost engine against the oracle's and against Kite's contract notes (A4 §5, D-46, G08).

G08 on the engine: for every captured Kite case, the engine's charge parts equal the oracle's, and its total
(the exact sum rounded once) equals Kite's. Then random orders, DP charges, square-off fees and option
exercise across dates on the shipped rows must agree with the oracle component for component.
Skipped when the engine is not built.
"""
from __future__ import annotations

import json
import random
import unittest
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, engine
from stockify.config import loader
from stockify.oracle import costs
from stockify.tools import capture_charges

PARTS = ("turnover", "brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp", "other")


def _rows_on(d: str) -> dict:
    return bundle_mod._dated_section(loader.load_json("charges.json"), date.fromisoformat(d), date.fromisoformat(d))


def _python(c: costs.Costs, what: str, kind: str, product: str, side: str, value: int, d: str):  # noqa: ANN202
    if what == "order":
        p = c.order(kind, product, side, value, d)
    elif what == "dp":
        p = c.dp(d)
    elif what == "sqoff_fee":
        p = c.sqoff_fee(d)
    else:
        p = c.exercise(kind, value, d)
    if p is None:
        return None
    out = {k: p[k] for k in PARTS}
    out["total"] = costs.total(p)
    return out


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class EngineCostTests(unittest.TestCase):
    def _cpp(self, rows: dict, cfg: dict, what: str, kind: str, product: str, side: str, value: int, d: str):  # noqa: ANN202
        return engine.load().testing.costs_eval(json.dumps(rows), json.dumps(cfg), what, kind, product, side, value, d)

    def test_g08_on_the_engine(self):
        caps = sorted(capture_charges.FIXTURES.glob("*.json"))
        self.assertTrue(caps, "no Kite capture in tests/fixtures/kite_charges")
        data = json.loads(caps[-1].read_text(encoding="utf-8"))
        d = data["captured_on"]
        rows = _rows_on(d)
        c = costs.Costs(rows, {})
        for case in data["cases"]:
            req, charges = case["request"], case["response"]["charges"]
            args = ("order", req["kind"], req["product"], req["transaction_type"], req["quantity"] * req["price_paise"], d)
            with self.subTest(case=req["case"]):
                mine = self._cpp(rows, {}, *args)
                self.assertEqual(mine, _python(c, *args))
                kite = int((Decimal(str(charges["total"])) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                self.assertEqual(mine["total"], kite)

    def test_random_charges_match_the_oracle(self):
        rng = random.Random(3701)
        cases = [("EQ", "CNC"), ("EQ", "MIS"), ("FUT", "NRML"), ("FUT", "MIS"), ("CE", "NRML"), ("PE", "MIS"),
                 ("ETF", "CNC")]
        for d in ("2026-03-31", "2026-04-01", "2025-06-02", "2026-10-30", "2026-11-05"):
            rows = _rows_on(d)
            for cfg in ({}, {"dp_charges": False, "auto_square_off_fee": False}):
                c = costs.Costs(rows, cfg)
                for _ in range(600):
                    kind, product = rng.choice(cases)
                    side = rng.choice(["BUY", "SELL"])
                    value = rng.choice([1, 99, 100, 900_00, 195_000, rng.randint(1, 10**8), rng.randint(1, 10**13)])
                    what = rng.choice(["order", "order", "order", "dp", "sqoff_fee", "exercise"])
                    if what == "exercise":
                        kind = rng.choice(["CE", "PE"])
                    args = (what, kind, product, side, value, d)
                    with self.subTest(args=args, cfg=cfg):
                        try:
                            expected = ("ok", _python(c, *args))
                        except costs.CostError:
                            expected = ("error", None)
                        try:
                            got = ("ok", self._cpp(rows, cfg, *args))
                        except RuntimeError:
                            got = ("error", None)
                        self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
