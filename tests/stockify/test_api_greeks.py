"""M4.14 (WP 4.4): /api/tests/{id}/greeks, the Greeks of open positions at each day end, derived in the API from
positions_eod.csv (the option's mark and, D-55, its underlying's mark) with stockify.fno.black76. An option whose IV
cannot be found gets the reason, never a made-up number. Also pins the engines' settlement log lines that
/settlements reads, so a wording change in either engine fails loudly."""
from __future__ import annotations

import copy
import json
import math
import unittest

import tests.support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import golden, paths, runs
from stockify.fno import black76 as B
from stockify.oracle import run_bundle
from tests.stockify.test_positions_underlying import world


def _cheap_call_world() -> dict:
    """The same world, but the call is a deep in-the-money strike quoted below its intrinsic value."""
    g = world()
    g["bundle"]["instruments"][3]["strike_paise"] = 2_400_000
    return g


class GreeksApiTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app)

    def _run(self, run_id: str, g: dict):  # noqa: ANN202
        scratch = paths.lab_dir() / f"scratch-{run_id}"
        scratch.mkdir()
        folder = paths.run_path(run_id)
        run_bundle(golden.build_bundle(g), [golden.build_pack(g, scratch)], folder, report=False)
        runs.import_run(folder)
        return folder

    def test_option_and_future_greeks_at_day_end(self):
        folder = self._run("d55", world())
        rows = self.client.get("/api/tests/d55/greeks").json()
        last = [r for r in rows if r["trade_date"] == "2026-03-03"]
        self.assertEqual({r["kind"] for r in last}, {"FUT", "CE"})                  # the equity is left out
        fut = next(r for r in last if r["kind"] == "FUT")
        self.assertEqual((fut["delta_units"], fut["iv"], fut["iv_reason"], fut["underlying_paise"]), (75.0, None, None, 2512000))
        ce = next(r for r in last if r["kind"] == "CE")
        bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
        row = next(i for i in bundle["instruments"] if i["id"] == 4)
        day_end = next(d["day_end_ns"] for d in bundle["days"] if d["date"] == "2026-03-03")
        t = B.year_fraction(day_end, row["last_trading_ns"])                       # set even after the run (D-61)
        r = B.rate_on(bundle["pricing"].get("risk_free", []), "2026-03-03")
        sigma, why = B.implied_vol(True, 405.0, 25120.0, 25000.0, t, r)
        self.assertIsNone(why)
        g = B.greeks(True, 25120.0, 25000.0, t, r, sigma)
        self.assertEqual((ce["iv_reason"], ce["expiry_close_estimated"]), (None, False))
        self.assertTrue(math.isclose(ce["iv"], sigma, rel_tol=1e-12))
        self.assertTrue(math.isclose(ce["delta_units"], g.delta * 75, rel_tol=1e-12))
        self.assertTrue(math.isclose(ce["gamma_units_per_rupee"], g.gamma * 75, rel_tol=1e-12))
        self.assertTrue(math.isclose(ce["vega_rupees_per_point"], g.vega * 75 / 100, rel_tol=1e-12))
        self.assertTrue(math.isclose(ce["theta_rupees_per_day"], g.theta * 75 / 365, rel_tol=1e-12))
        self.assertEqual((ce["mark_paise"], ce["strike_paise"], ce["expiry"], ce["qty"]), (40500, 2500000, "2026-03-30", 75))

    def test_an_iv_that_cannot_be_found_gives_the_reason(self):
        self._run("cheap", _cheap_call_world())
        ce = next(r for r in self.client.get("/api/tests/cheap/greeks").json()
                  if r["kind"] == "CE" and r["trade_date"] == "2026-03-03")
        self.assertEqual((ce["iv"], ce["delta_units"], ce["vega_rupees_per_point"]), (None, None, None))
        self.assertEqual(ce["iv_reason"], B.BELOW_INTRINSIC)

    def test_unknown_run_404(self):
        self.assertEqual(self.client.get("/api/tests/nope/greeks").status_code, 404)


class SettlementLinePinTests(unittest.TestCase):
    """The exact log lines /api/tests/{id}/settlements reads, from both engines, on G10 and G11."""

    def test_both_engines_write_the_lines_the_api_parses(self):
        from server.routers.tests import _DELIVERED, _SETTLED
        from stockify import engine
        impls = [("oracle", run_bundle)] + ([("engine", engine.run_bundle)] if engine.available() else [])
        import tempfile
        from pathlib import Path
        for gid, variant, settled, delivered in (("G10", "fsp", 3, 0), ("G10", "fallback", 3, 0), ("G11", "fsp", 3, 3)):
            g = next(x for _, x in golden.goldens([gid]))
            v = next(x for x in g["variants"] if x["name"] == variant)
            h = {k: copy.deepcopy(x) for k, x in g.items() if k != "variants"}
            for part in ("bundle", "run", "strategy"):
                if part in v:
                    h[part] = golden._merge(h.get(part, {}), v[part])
            if "drop_events" in v:
                h["events"] = [e for n, e in enumerate(g["events"], start=1) if n not in v["drop_events"]]
            for name, run in impls:
                with self.subTest(golden=gid, variant=variant, impl=name), tempfile.TemporaryDirectory() as tmp:
                    folder = Path(tmp)
                    run(golden.build_bundle(h), [golden.build_pack(h, folder)], folder / "run", report=False)
                    lines = (folder / "run" / "events_log.txt").read_text(encoding="utf-8").splitlines()
                    about = [x for x in lines if " settled " in x or " delivered " in x]
                    self.assertEqual(sum(bool(_SETTLED.match(x)) for x in about), settled, about)
                    self.assertEqual(sum(bool(_DELIVERED.match(x)) for x in about), delivered, about)
                    self.assertEqual(len(about), settled + delivered, about)        # nothing the API would miss


if __name__ == "__main__":
    unittest.main()
