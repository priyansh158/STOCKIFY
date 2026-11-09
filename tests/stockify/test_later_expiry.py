"""D-61 (CB 4.5): every derivative in a bundle carries its last trading moment, even when its expiry falls after
the run's last day: the FO close of the expiry date under the dated session rules (X-7: 15:30 IST before
2026-08-03, 15:40 from then), on actual_expiry when present (D-41). Neither engine falls back to `now` any more;
a bundle without the field is refused when it loads, with the same message on both."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import engine, golden, paths, runs, timeutil
from stockify.data import calendar
from stockify.fno import black76 as B
from stockify.oracle import run_bundle as oracle_run
from tests.stockify.test_positions_underlying import world

LATER = "2026-03-13"                    # 10 days after the run's last day (2026-03-03)


def _later_world() -> dict:
    g = world()
    for row in g["bundle"]["instruments"]:
        if row["kind"] in ("FUT", "CE"):
            row["expiry"] = LATER
    return g


class SessionCloseTests(unittest.TestCase):
    def test_the_fo_close_follows_the_dated_rules(self):
        self.assertEqual(calendar.session_close_ns("FO_OPT", date(2026, 3, 13)), timeutil.ist_ns(date(2026, 3, 13), "15:30"))
        self.assertEqual(calendar.session_close_ns("FO_OPT", date(2026, 8, 13)), timeutil.ist_ns(date(2026, 8, 13), "15:40"))
        self.assertEqual(calendar.session_close_ns("FO_FUT", date(2026, 8, 13)), timeutil.ist_ns(date(2026, 8, 13), "15:40"))


class BundleTests(unittest.TestCase):
    def test_a_later_expiry_gets_its_close(self):
        b = golden.build_bundle(_later_world())
        ce = next(i for i in b["instruments"] if i["kind"] == "CE")
        self.assertEqual(ce["last_trading_ns"], timeutil.ist_ns(date(2026, 3, 13), "15:30"))

    def test_actual_expiry_wins(self):
        g = _later_world()
        g["bundle"]["instruments"][3]["actual_expiry"] = "2026-03-10"
        ce = next(i for i in golden.build_bundle(g)["instruments"] if i["kind"] == "CE")
        self.assertEqual(ce["last_trading_ns"], timeutil.ist_ns(date(2026, 3, 10), "15:30"))

    def test_a_bundle_without_the_field_is_refused_on_both_engines(self):
        g = _later_world()
        impls = [("oracle", oracle_run)] + ([("engine", engine.run_bundle)] if engine.available() else [])
        for name, run in impls:
            with self.subTest(impl=name), tempfile.TemporaryDirectory() as tmp:
                b = golden.build_bundle(g)
                next(i for i in b["instruments"] if i["kind"] == "CE")["last_trading_ns"] = None
                with self.assertRaisesRegex(Exception, r"bundle: instruments\[\d+\]: a CE needs last_trading_ns \(D-61\)"):
                    run(b, [golden.build_pack(g, Path(tmp))], Path(tmp) / "run", report=False)


class LaterExpiryGreeksTests(SandboxedDBTest):
    db_modules = (store,)

    def test_an_option_ten_days_out_has_a_sensible_iv_and_time_value(self):
        from fastapi.testclient import TestClient
        from server.main import app
        g = _later_world()
        scratch = paths.lab_dir() / "scratch-later"
        scratch.mkdir()
        folder = paths.run_path("later")
        oracle_run(golden.build_bundle(g), [golden.build_pack(g, scratch)], folder, report=False)
        runs.import_run(folder)
        ce = next(r for r in TestClient(app).get("/api/tests/later/greeks").json()
                  if r["kind"] == "CE" and r["trade_date"] == "2026-03-03")
        self.assertIsNone(ce["iv_reason"])
        self.assertFalse(ce["expiry_close_estimated"])                  # the bundle has it now
        self.assertTrue(0.05 < ce["iv"] < 1.0, ce["iv"])                  # a market-like volatility, not a t→0 artefact
        self.assertLess(ce["theta_rupees_per_day"], 0)                   # a long call loses time value
        intrinsic = max(0, 2_512_000 - 2_500_000)
        self.assertGreater(ce["mark_paise"], intrinsic)                  # nonzero time value in the price
        t = B.year_fraction(golden.build_bundle(g)["days"][-1]["day_end_ns"], timeutil.ist_ns(date(2026, 3, 13), "15:30"))
        self.assertGreater(t, 9 / 366)                                     # about ten days, not MIN_T


if __name__ == "__main__":
    unittest.main()
