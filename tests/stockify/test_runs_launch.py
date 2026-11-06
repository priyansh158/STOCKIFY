"""M3.11 end to end: `python -m stockify run` resolves a bundle offline, runs it on the engine, and records it
in lab.sqlite (A1 §4); failures, queued runs and cancellation (stockify.runs). Uses the M2.1 bundle fixture."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports

import stockify.store as store
from stockify import engine, paths, runs
from stockify import __main__ as cli
from tests.stockify.test_bundle import BundleTest, run

REPO = Path(__file__).resolve().parents[2]
BUY = {"kind": "python", "ref": "stockify.strategies.buy_and_hold:BuyAndHold", "params": {"symbol": "NSE:RELIANCE"}}
BOOM = {"kind": "python", "ref": "tests.stockify.run_fixtures:FailOnSecondBar", "params": {"symbol": "NSE:RELIANCE"}}


def _cli(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class RunsTestBase(BundleTest):
    """The M2.1 offline refdata and bars, plus a sandboxed lab.sqlite."""

    def setUp(self):
        super().setUp()
        support.reset_sqlite_singleton(store)
        self.addCleanup(support.reset_sqlite_singleton, store)

    def config(self, strategy: dict, name: str = "t") -> Path:
        path = paths.lab_dir() / f"{name}.run.json"
        path.write_text(json.dumps(run(name=name, strategy=strategy)), encoding="utf-8")
        return path


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class LaunchTests(RunsTestBase):

    def test_a_config_runs_to_done_with_its_rows(self):
        code, out, err = _cli("run", "--config", str(self.config(BUY)), "--no-report")
        self.assertEqual(code, 0, err)
        folder = Path(out.strip())
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(folder.parent, paths.runs_dir())
        self.assertRegex(folder.name, r"^t-[0-9a-f]{12}$")
        self.assertIn("NAV", err)
        row = store.get_run(folder.name)
        self.assertEqual((row["status"], row["impl"], row["folder"], row["error"]), ("done", "engine", str(folder), None))
        self.assertEqual(row["bundle_sha256"], json.loads((folder / "manifest.json").read_text())["bundle_sha256"])
        self.assertEqual(row["engine_build_id"], engine.build_info()["engine_build_id"])
        self.assertLessEqual(row["created_ns"], row["started_ns"])
        self.assertLessEqual(row["started_ns"], row["finished_ns"])
        orders = store.run_rows(folder.name, "orders")
        # one buy, part-filled (the fixture's bars trade 1,000 units; 10 % participation), the rest cancelled EOD
        self.assertEqual([(o["side"], o["status"], o["filled_qty"], o["reason"]) for o in orders],
                         [("BUY", "CANCELLED", 100, "EOD")])
        self.assertEqual(len(store.run_rows(folder.name, "fills")), 1)
        self.assertEqual(len(store.run_rows(folder.name, "equity")), 7)
        self.assertFalse((folder / "report.md").exists())
        # the same config again: a new folder, the first one untouched
        before = (folder / "determinism.json").read_bytes()
        code, out2, _ = _cli("run", "--config", str(self.config(BUY)), "--no-report")
        self.assertEqual((code, Path(out2.strip()).name), (0, folder.name + "-2"))
        self.assertEqual((folder / "determinism.json").read_bytes(), before)
        self.assertEqual((Path(out2.strip()) / "determinism.json").read_bytes(), before)

    def test_the_oracle_impl(self):
        rid = runs.launch(self.config(BUY), impl="oracle", report=False)
        row = store.get_run(rid)
        self.assertEqual((row["status"], row["impl"], row["engine_build_id"]), ("done", "oracle", None))
        self.assertEqual(len(store.run_rows(rid, "orders")), 1)

    def test_a_strategy_that_raises_leaves_a_failed_row(self):
        code, out, err = _cli("run", "--config", str(self.config(BOOM, "boom")), "--no-report")
        self.assertEqual((code, out), (1, ""))
        row = store.list_runs()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "ZeroDivisionError: planned failure on the second bar")
        self.assertIn(row["error"], err)
        self.assertIsNotNone(row["finished_ns"])
        log = (Path(row["folder"]) / "events_log.txt").read_text(encoding="utf-8")
        self.assertIn("ERROR Traceback (most recent call last):", log)
        self.assertIn("ERROR ZeroDivisionError: planned failure on the second bar", log)
        self.assertFalse((Path(row["folder"]) / "orders.csv").exists())
        self.assertEqual(store.run_rows(row["run_id"], "orders"), [])

    def test_a_bundle_that_cannot_resolve_is_a_failed_run(self):
        bad = dict(BUY, ref="no.such.module:X")
        with self.assertRaises(runs.RunFailed) as ctx:
            runs.launch(self.config(bad), report=False)
        row = store.get_run(ctx.exception.run_id)
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["error"].startswith("BundleError: "), row["error"])

    def test_a_queued_run_runs_by_id_once(self):
        rid = runs.queue(self.config(BUY))
        self.assertEqual(store.get_run(rid)["status"], "queued")
        code, out, err = _cli("run", "--run-id", rid, "--no-report")
        self.assertEqual(code, 0, err)
        self.assertEqual(Path(out.strip()), paths.runs_dir() / rid)
        self.assertTrue((paths.runs_dir() / rid / "request.json").exists())
        self.assertEqual(store.get_run(rid)["status"], "done")
        code, _, err = _cli("run", "--run-id", rid)
        self.assertEqual(code, 2)
        self.assertIn("done, not queued", err)

    def test_a_cancelled_row_stays_cancelled(self):
        rid = runs.queue(self.config(BUY))
        real = engine.run_bundle

        def cancel_meanwhile(*a, **kw):  # noqa: ANN002, ANN003, ANN202
            out = real(*a, **kw)
            store.update_run(rid, status="cancelled")  # the API cancels while the run finishes
            return out
        with mock.patch.object(engine, "run_bundle", cancel_meanwhile):
            runs.launch(run_id=rid, report=False)
        row = store.get_run(rid)
        self.assertEqual((row["status"], row["finished_ns"], row["metrics_json"]), ("cancelled", None, None))
        self.assertEqual(store.run_rows(rid, "orders"), [])

    def test_as_a_subprocess(self):
        """I-9: the API launches `python -m stockify run` as a child process."""
        proc = subprocess.run([sys.executable, "-m", "stockify", "run", "--config", str(self.config(BUY)), "--no-report"],
                              cwd=str(REPO), capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        folder = Path(proc.stdout.strip())
        support.reset_sqlite_singleton(store)
        self.assertEqual(store.get_run(folder.name)["status"], "done")
        self.assertTrue((folder / "determinism.json").exists())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class CppConfigTests(RunsTestBase):
    """A `strategy.kind: cpp` config runs end to end and equals its Python twin on the same data."""

    PARAMS = {"symbol": "NSE:RELIANCE", "fast": 2, "slow": 3}

    def setUp(self):
        super().setUp()
        from stockify.data import history
        # up, then down (one round trip); each day opens at the previous close, moves stay inside the 2 % band
        closes = {22: 10000, 23: 10050, 24: 10150, 26: 10200, 29: 10100, 30: 10000, 31: 9950}
        from datetime import date
        from stockify import timeutil
        prev = 10000
        with history._Tx() as conn:
            for d, c in closes.items():
                conn.execute("UPDATE bars SET open_paise = ?, high_paise = ?, low_paise = ?, close_paise = ? "
                             "WHERE instrument_id = ? AND ts_ns = ?",
                             (prev, max(prev, c) + 10, min(prev, c) - 10, c, self.rel,
                              timeutil.ist_ns(date(2025, 12, d), "00:00")))
                prev = c

    def test_sma_cross_cpp_equals_the_python_strategy(self):
        from tests.stockify.test_engine_parity import assert_same_artifacts
        py = runs.launch(self.config({"kind": "python", "ref": "stockify.strategies.sma_cross:SmaCross",
                                      "params": self.PARAMS}, "py"), report=False)
        code, out, err = _cli("run", "--config", str(self.config({"kind": "cpp", "ref": "sma_cross_cpp",
                                                                   "params": self.PARAMS}, "cpp")), "--no-report")
        self.assertEqual(code, 0, err)
        cpp = Path(out.strip())
        assert_same_artifacts(self, paths.runs_dir() / py, cpp)
        self.assertEqual([f["side"] for f in store.run_rows(py, "fills")], ["BUY", "SELL"])
        bundle = json.loads((cpp / "bundle.json").read_text(encoding="utf-8"))
        import hashlib
        build = engine.build_info()["engine_build_id"]
        self.assertEqual(bundle["strategy"]["source_sha256"],
                         hashlib.sha256(f"{build}:sma_cross_cpp".encode()).hexdigest())
        self.assertEqual(store.get_run(cpp.name)["status"], "done")
        self.assertTrue(runs.rerun(cpp.name)[1])

    def test_an_unknown_cpp_name_is_a_failed_run(self):
        with self.assertRaises(runs.RunFailed) as ctx:
            runs.launch(self.config({"kind": "cpp", "ref": "nope_cpp", "params": {}}), report=False)
        self.assertRegex(store.get_run(ctx.exception.run_id)["error"],
                         r"^BundleError: strategy nope_cpp: no C\+\+ strategy is registered .*known: .*orb_cpp, sma_cross_cpp")
