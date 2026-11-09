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
E1, E2, E3 = "2026-11-11", "2026-11-17", "2026-11-23"      # trading days (10 and 24 Nov are NSE holidays)
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
          "instrument_type,segment,exchange")
SPOT = 25_000.0
TRUE = {"psr": 0.08, "vsr": 0.30, "somc": 300_000, "psr_fut": 0.11}   # futures move further than options (D-62)
TOKEN = "fake-access-token-do-not-store"


def dumps() -> dict[str, str]:
    nfo = [HEADER, "9001,1,NIFTY26NOVFUT,NIFTY,0,2026-11-26,0,0.1,75,FUT,NFO-FUT,NFO"]
    tok = 10_000
    for exp, tag in ((E1, "26N11"), (E2, "26N17"), (E3, "26N23")):
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
        row = CAL.with_params(self.base, TRUE["psr"], TRUE["vsr"], TRUE["somc"], TRUE["psr_fut"])
        [b] = self.pricer.t1(CAL.Portfolio("x", legs), row)[1] or [None]
        span, exposure = (max(b.scan, b.somc) / 100, (b.elm + b.addons) / 100) if b else (0.0, 0.0)
        # as Kite does: the premium of bought options is in `total` too (it is paid upfront), but not in span/exposure
        premium = sum(self.snapshot.mark(i) * u for i, u in legs if u > 0 and self.snapshot.rows[i]["kind"] != "FUT") / 100
        final = {"span": span, "exposure": exposure, "additional": 0, "option_premium": premium,
                 "total": span + exposure + premium}
        return {"initial": dict(final, span=span * 1.3, total=final["total"] + span * 0.3), "final": final,
                "orders": [{"total": 0}] * len(orders)}


def redirect_quiet():  # noqa: ANN201
    import io
    from contextlib import ExitStack, redirect_stderr, redirect_stdout
    stack = ExitStack()
    stack.enter_context(redirect_stdout(io.StringIO()))
    stack.enter_context(redirect_stderr(io.StringIO()))
    return stack


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
        # the shipped file may already hold pending rows from real calibrations: pick this run's row, and check the
        # others (other underlyings or days) are kept as they were
        shipped = json.loads(Path(loader.__file__).with_name("margin_params.json").read_text(encoding="utf-8"))
        mine = [r for r in doc["spanlite_pending"]
                if r["applies"].get("underlying") == "NIFTY" and r["effective_from"] == DAY]
        others = [r for r in shipped.get("spanlite_pending", [])
                  if not (r["applies"].get("underlying") == "NIFTY" and r["effective_from"] == DAY)]
        self.assertEqual([r for r in doc["spanlite_pending"] if r not in mine], others)
        [pending] = mine
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

    def test_a_second_run_the_same_day_replaces_the_pending_row_and_the_config_still_loads(self):
        from stockify.config import loader
        tmp = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))
        shutil.copytree(loader.CONFIG_DIR, tmp, dirs_exist_ok=True)
        row = {"applies": {"underlying": "NIFTY"}, "effective_from": DAY, "psr_pct": "0.07", "vsr_pct": "0.3",
               "calibration_id": f"calibration-{DAY}", "source": f"calibration {DAY}", "verified": False,
               "pending_review": True}
        CAL.write_pending(row, tmp / "margin_params.json")
        CAL.write_pending(dict(row, psr_pct="0.08"), tmp / "margin_params.json")
        CAL.write_pending(dict(row, applies={"underlying": "BANKNIFTY"}), tmp / "margin_params.json")
        doc = loader.load_json("margin_params.json", tmp)                    # refuses duplicate dated rows
        self.assertEqual(sorted((r["applies"]["underlying"], r["psr_pct"]) for r in doc["spanlite_pending"]),
                         [("BANKNIFTY", "0.07"), ("NIFTY", "0.08")])

    def test_skip_expiring_leaves_out_todays_expiry_and_takes_the_next_two(self):
        """IWB 4.2: on an expiry day, --skip-expiring prices the next two expiries; the choice is recorded."""
        for name, day, moved in (("on the expiry date", E1, False), ("moved by refdata's actual_expiry", DAY, True)):
            with self.subTest(name):
                if moved:                         # NSE moved E1's last trading day to DAY (as a holiday does)
                    with refdata.transaction() as conn:
                        conn.execute("UPDATE instruments SET actual_expiry=? WHERE expiry=?", (DAY, E1))
                kw = dict(strikes=4, sleep=self.sleeps.append, now_ns=lambda: 1_794_180_600_000_000_000)
                plain = CAL.build_snapshot(self.kite, "NIFTY", day, **kw)
                self.assertEqual((plain.expiries, plain.skipped, plain.skip_expiring), ([E1, E2], [], False))
                snap = CAL.build_snapshot(self.kite, "NIFTY", day, skip_expiring=True, **kw)
                self.assertEqual((snap.expiries, snap.skipped, snap.skip_expiring), ([E2, E3], [E1], True))
                self.assertFalse([r for r in snap.rows.values() if r["expiry"] == E1])
                self.assertEqual(len(snap.futures), 1)                      # the future expires on the 26th
                self.assertTrue({p.kind for p in CAL.sample_portfolios(snap, 40, 3)} >= {"straddle", "iron_fly"})
                margin, pricing = CAL._config_params()
                self.kite.snapshot, self.kite.base = snap, CAL.class_row(margin, snap)
                self.kite.pricer = CAL.Pricer(snap, margin, pricing)
                tmp = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))
                params = tmp / "margin_params.json"
                from stockify.config import loader
                shutil.copy(Path(loader.__file__).with_name("margin_params.json"), params)
                before = len(store.margin_calibrations("NIFTY"))
                report, _ = CAL.calibrate(self.kite, "NIFTY", 8, seed=2, day=day, sleep=self.sleeps.append,
                                          snapshot=snap, params_path=params)
                text = report.read_text(encoding="utf-8")
                self.assertIn(f"Expiries priced: {E2}, {E3}; contracts expiring today left out (--skip-expiring): "
                              f"{E1}.", text)
                rows = store.margin_calibrations("NIFTY")[before:]
                self.assertTrue(rows)
                for r in rows:
                    got = json.loads(r["market_json"])["snapshot"]
                    self.assertEqual({k: got[k] for k in ("skip_expiring", "expiries", "skipped")},
                                     {"skip_expiring": True, "expiries": [E2, E3], "skipped": [E1]})

    def test_the_cli_passes_skip_expiring(self):
        from unittest import mock
        from stockify import __main__ as cli
        import kite_client
        seen = {}

        def fake(client, und, n, **kw):  # noqa: ANN001, ANN202
            seen.update(kw, und=und, n=n)
            return Path("r.md"), Path("p.json")
        with mock.patch.object(kite_client, "is_enabled", return_value=True), \
                mock.patch.object(kite_client, "get_client", return_value=self.kite), \
                mock.patch.object(CAL, "calibrate", side_effect=fake), redirect_quiet():
            self.assertEqual(cli.main(["margin", "calibrate", "--underlying", "NIFTY", "--samples", "5",
                                       "--skip-expiring"]), 0)
            self.assertTrue(seen["skip_expiring"])
            self.assertEqual(cli.main(["margin", "calibrate", "--underlying", "NIFTY", "--samples", "5"]), 0)
            self.assertFalse(seen["skip_expiring"])
        self.assertEqual(self.kite.calls, [])                               # nothing reached the fake Kite

    def test_the_target_leaves_out_the_premium_of_bought_options(self):
        """IWB 4.5: Kite's final.total includes option_premium (paid as cash in a run); the fit target does not."""
        answer = {"final": {"span": 19750, "exposure": 64893.4, "additional": 0, "option_premium": 33741.5,
                            "total": 118384.9}}                       # the live hedged future of 2026-11-09
        self.assertEqual(CAL.kite_target(answer), 8_464_340)
        self.assertEqual(CAL.kite_target({"final": {"span": 0, "exposure": 0, "option_premium": 2600,
                                                    "total": 2600}}), 0)
        self.assertEqual(CAL.kite_target({}), 0)

    def calibrated(self) -> tuple[CAL.Snapshot, Path, Path]:
        snap = self.snapshot()
        tmp = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))
        params = tmp / "margin_params.json"
        from stockify.config import loader
        shutil.copy(Path(loader.__file__).with_name("margin_params.json"), params)
        report, _ = CAL.calibrate(self.kite, "NIFTY", 32, seed=4, day=DAY, sleep=self.sleeps.append, snapshot=snap,
                                  params_path=params)
        return snap, params, report

    def mine(self, params: Path) -> dict:
        doc = json.loads(params.read_text(encoding="utf-8"))
        [row] = [r for r in doc["spanlite_pending"]
                 if r["applies"].get("underlying") == "NIFTY" and r["effective_from"] == DAY]
        return row

    def test_the_fit_finds_a_bigger_futures_move_and_the_rows_record_the_target(self):
        snap, params, report = self.calibrated()
        row = self.mine(params)
        self.assertLess(abs(float(row["psr_futures_pct"]) - TRUE["psr_fut"]), 0.005)
        self.assertLess(abs(float(row["psr_pct"]) - TRUE["psr"]), 0.01)
        for r in store.margin_calibrations("NIFTY"):
            got = json.loads(r["market_json"])["snapshot"]
            self.assertEqual((got["target"], got["target_paise"]),
                             (CAL.TARGET, CAL.kite_target(json.loads(r["kite_json"]))))
            self.assertEqual(len(got["quotes"]), len(snap.quotes))     # the whole snapshot, for exact refits
        text = report.read_text(encoding="utf-8")
        self.assertIn("without the premium of bought options", text)
        self.assertIn("for futures", text)

    def test_refit_offline_from_the_stored_rows(self):
        """--refit rebuilds the snapshot from refdata and the stored rows, fits again and replaces the pending row;
        it takes no client at all. Rows from before IWB 4.5 (only their legs' quotes) refit too."""
        snap, params, _ = self.calibrated()
        live = self.mine(params)
        calls = len(self.kite.calls)
        report, written = CAL.refit("NIFTY", DAY, params_path=params)
        self.assertEqual(len(self.kite.calls), calls)
        self.assertEqual(written, params)
        row = self.mine(params)                                         # still one row for the day: replaced
        self.assertIn("refit offline", row["source"])
        for k in ("psr_pct", "psr_futures_pct", "vsr_pct", "somc_per_lot_paise"):
            self.assertEqual(row[k], live[k], k)                        # exact snapshot: the same fit
        self.assertEqual(report.name, f"NIFTY-{DAY}-refit.md")
        text = report.read_text(encoding="utf-8")
        self.assertIn("Refitted offline", text)
        self.assertIn("the full snapshot", text)
        rebuilt, _, how = CAL.snapshot_from_rows("NIFTY", DAY, CAL.stored_run("NIFTY", DAY))
        self.assertEqual((how, rebuilt.quotes, rebuilt.expiries), ("exact", snap.quotes, snap.expiries))
        # strip the full quotes, as rows written before IWB 4.5 are
        conn = store._connect()
        for r in store.margin_calibrations("NIFTY"):
            m = json.loads(r["market_json"])
            m["snapshot"] = {k: v for k, v in m["snapshot"].items() if k not in ("quotes", "target", "target_paise")}
            conn.execute("UPDATE margin_calibration SET market_json=? WHERE sample_id=?",
                         (json.dumps(m, sort_keys=True), r["sample_id"]))
        conn.commit()
        rebuilt, _, how = CAL.snapshot_from_rows("NIFTY", DAY, CAL.stored_run("NIFTY", DAY))
        self.assertEqual(how, "legs_only")
        self.assertLess(len(rebuilt.quotes), len(snap.quotes))
        report, _ = CAL.refit("NIFTY", DAY, params_path=params)
        self.assertIn("only the legs of each sample", report.read_text(encoding="utf-8"))
        self.assertLess(abs(float(self.mine(params)["psr_pct"]) - TRUE["psr"]), 0.01)
        with self.assertRaisesRegex(CAL.CalibrationError, "no stored calibration samples"):
            CAL.refit("NIFTY", "2026-11-11", params_path=params)

    def test_the_cli_refit_never_touches_kite(self):
        from unittest import mock
        from stockify import __main__ as cli
        import kite_client
        seen = {}

        def fake(und, day, **kw):  # noqa: ANN001, ANN202
            seen.update(und=und, day=day)
            return Path("r.md"), Path("p.json")
        with mock.patch.object(kite_client, "get_client", side_effect=AssertionError("no Kite")), \
                mock.patch.object(kite_client, "is_enabled", side_effect=AssertionError("no Kite")), \
                mock.patch.object(CAL, "refit", side_effect=fake), redirect_quiet():
            self.assertEqual(cli.main(["margin", "calibrate", "--underlying", "BANKNIFTY", "--refit", "--date", DAY]),
                             0)
            self.assertEqual(seen, {"und": "BANKNIFTY", "day": DAY})
            self.assertEqual(cli.main(["margin", "calibrate", "--underlying", "NIFTY", "--refit"]), 2)

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
