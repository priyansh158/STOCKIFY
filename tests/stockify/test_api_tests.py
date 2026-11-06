"""M3.14: /api/strategies and /api/tests, and the run queue behind them (A2 §6, I-9).

Most tests install a queue with a fake launcher, so nothing is spawned; one runs the real subprocess.
"""
from __future__ import annotations

import copy
import json
import threading
import time
import unittest
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import engine, paths, runs

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = json.loads((REPO / "examples" / "runs" / "sma_reliance_daily.json").read_text(encoding="utf-8"))


class FakeProc:
    """A stand-in for subprocess.Popen: finishes when told to, or when killed."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.done = threading.Event()
        self.killed = False

    def wait(self) -> int:
        self.done.wait(30)
        return -9 if self.killed else 0

    def kill(self) -> None:
        self.killed = True
        self.done.set()

    def poll(self) -> int | None:
        return (-9 if self.killed else 0) if self.done.is_set() else None

    def finish(self, status: str = "done") -> None:
        store.update_run(self.run_id, status=status, only_if_status="running")
        self.done.set()


class FakeLauncher:
    def __init__(self):
        self.procs: dict[str, FakeProc] = {}
        self.order: list[str] = []

    def __call__(self, run_id: str) -> FakeProc:
        store.update_run(run_id, status="running", only_if_status="queued")  # what the runner does first
        p = FakeProc(run_id)
        self.procs[run_id] = p
        self.order.append(run_id)
        return p


def _wait(cond, timeout: float = 10.0) -> None:  # noqa: ANN001
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.01)


@support.requires_fastapi
class ApiTestBase(SandboxedDBTest):
    db_modules = (store,)
    cap = 2

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        from server.services import test_queue
        self.launcher = FakeLauncher()
        self.queue = test_queue.TestQueue(launcher=self.launcher, cap=self.cap)
        test_queue.set_queue(self.queue)
        self.addCleanup(test_queue.set_queue, None)
        self.addCleanup(self._release)
        self.client = TestClient(app)

    def _release(self):
        for p in self.launcher.procs.values():
            p.done.set()
        self.queue.join(5)

    def post(self, cfg: dict):  # noqa: ANN201
        return self.client.post("/api/tests", json=cfg)


class CatalogueAndRoutesTests(ApiTestBase):
    def test_strategies(self):
        r = self.client.get("/api/strategies")
        self.assertEqual(r.status_code, 200)
        refs = {s["ref"] for s in r.json()}
        self.assertIn("stockify.strategies.sma_cross:SmaCross", refs)
        self.assertIn("stockify.strategies.eod_short_straddle:EodShortStraddle", refs)
        if engine.available():
            self.assertIn("sma_cross_cpp", refs)
        sma = next(s for s in r.json() if s["ref"] == "stockify.strategies.sma_cross:SmaCross")
        self.assertEqual(sma["params"][0], {"name": "symbol", "label": "Stock or future", "type": "symbol",
                                            "default": None, "required": True,
                                            "help": "what to trade, e.g. NSE:RELIANCE"})

    def test_fixed_paths_are_not_ids(self):
        r = self.client.get("/api/tests/compare")
        self.assertEqual(r.status_code, 501)
        self.assertIn("M6", r.json()["detail"])
        self.assertEqual(self.client.get("/api/tests/nope").status_code, 404)
        self.assertEqual(self.client.get("/api/tests/nope/fills").status_code, 404)
        self.assertEqual(self.client.post("/api/tests/nope/cancel").status_code, 404)

    def test_list_filters(self):
        self.assertEqual(self.client.get("/api/tests", params={"status": "weird"}).status_code, 422)
        store.create_run({"run_id": "a", "status": "done", "family_id": "f1", "created_ns": 1})
        store.create_run({"run_id": "b", "status": "failed", "family_id": "f2", "created_ns": 2, "error": "X: y"})
        self.assertEqual([r["run_id"] for r in self.client.get("/api/tests").json()], ["b", "a"])
        self.assertEqual([r["run_id"] for r in self.client.get("/api/tests", params={"status": "done"}).json()], ["a"])
        self.assertEqual([r["run_id"] for r in self.client.get("/api/tests", params={"family": "f2"}).json()], ["b"])
        self.assertEqual(self.client.get("/api/tests", params={"limit": 1, "offset": 1}).json()[0]["run_id"], "a")


class CreateTests(ApiTestBase):
    def test_bad_configs_are_422_and_nothing_is_queued(self):
        bad_schema = self.post({"name": "x"})
        self.assertEqual(bad_schema.status_code, 422)
        self.assertIn("schema", bad_schema.json()["detail"][0])

        unknown = copy.deepcopy(EXAMPLE)
        unknown["strategy"]["ref"] = "stockify.strategies.nope:Nope"
        r = self.post(unknown)
        self.assertEqual(r.status_code, 422)
        self.assertIn("not in the catalogue", r.json()["detail"][0])

        params = copy.deepcopy(EXAMPLE)
        params["strategy"]["params"] = {"symbol": "NSE:RELIANCE", "fast": "twenty", "colour": "red"}
        r = self.post(params)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"], ["strategy.params.colour: unknown parameter",
                                              "strategy.params.fast: is not a whole number",
                                              "strategy.params.slow: required"])

        script = copy.deepcopy(EXAMPLE)
        script["strategy"] = {"kind": "script", "actions": []}
        self.assertEqual(self.post(script).status_code, 422)
        self.assertEqual(store.list_runs(), [])
        self.assertEqual(self.launcher.order, [])

    def test_a_good_config_is_queued_and_started(self):
        r = self.post(EXAMPLE)
        self.assertEqual(r.status_code, 201)
        run_id = r.json()["run_id"]
        self.assertTrue((paths.runs_dir() / run_id / "request.json").exists())
        _wait(lambda: run_id in self.launcher.procs)
        detail = self.client.get(f"/api/tests/{run_id}").json()
        self.assertEqual(detail["run"]["status"], "running")
        self.assertEqual((detail["run"]["start_date"], detail["run"]["end_date"]), ("2022-01-03", "2026-09-30"))
        self.assertEqual(detail["run"]["capital_paise"], 100_000_000)
        self.assertEqual(detail["run"]["price_basis"], "adjusted")
        self.assertTrue(detail["run"]["created_ist"].endswith("+05:30"))
        self.launcher.procs[run_id].finish()
        _wait(lambda: self.queue.running() == [])
        self.assertEqual(self.client.get(f"/api/tests/{run_id}").json()["run"]["status"], "done")


class QueueTests(ApiTestBase):
    cap = 2

    def _queue_three(self) -> list[str]:
        ids = [self.post(EXAMPLE).json()["run_id"] for _ in range(3)]
        _wait(lambda: len(self.launcher.order) == 2)
        return ids

    def test_concurrency_cap(self):
        ids = self._queue_three()
        self.assertEqual(self.launcher.order, ids[:2])
        self.assertEqual(self.queue.waiting(), ids[2:])
        self.assertEqual(store.get_run(ids[2])["status"], "queued")
        self.launcher.procs[ids[0]].finish()
        _wait(lambda: ids[2] in self.launcher.procs)
        self.assertEqual(sorted(self.queue.running()), sorted(ids[1:]))
        from server.services.test_queue import default_cap
        import os
        self.assertEqual(default_cap(), max(1, (os.cpu_count() or 2) - 1))

    def test_cancel_a_waiting_run(self):
        ids = self._queue_three()
        r = self.client.post(f"/api/tests/{ids[2]}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "cancelled")
        self.launcher.procs[ids[0]].finish()
        self.launcher.procs[ids[1]].finish()
        _wait(lambda: self.queue.running() == [])
        self.assertNotIn(ids[2], self.launcher.procs)  # never started
        self.assertEqual(self.client.post(f"/api/tests/{ids[2]}/cancel").status_code, 409)

    def test_cancel_a_running_run_kills_it(self):
        ids = self._queue_three()
        r = self.client.post(f"/api/tests/{ids[0]}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.launcher.procs[ids[0]].killed)
        row = store.get_run(ids[0])
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["finished_ns"])
        _wait(lambda: ids[2] in self.launcher.procs)  # the freed slot goes to the waiting run
        # the killed runner's own late "done" must not overwrite cancelled
        self.launcher.procs[ids[0]].finish("done")
        self.assertEqual(store.get_run(ids[0])["status"], "cancelled")

    def test_cancel_loses_the_race_to_a_finish(self):
        ids = self._queue_three()
        store.update_run(ids[0], status="done", only_if_status="running")  # finished, watcher not yet back
        r = self.client.post(f"/api/tests/{ids[0]}/cancel")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(store.get_run(ids[0])["status"], "done")

    def test_cancel_a_finished_run_is_409(self):
        store.create_run({"run_id": "d", "status": "done"})
        self.assertEqual(self.client.post("/api/tests/d/cancel").status_code, 409)

    def test_restart_recovery(self):
        from server.services.test_queue import INTERRUPTED, TestQueue
        store.create_run({"run_id": "old-running", "status": "running", "created_ns": 1})
        store.create_run({"run_id": "q1", "status": "queued", "created_ns": 2})
        store.create_run({"run_id": "q2", "status": "queued", "created_ns": 3})
        store.create_run({"run_id": "done", "status": "done", "created_ns": 4})
        fresh = TestQueue(launcher=self.launcher, cap=1)
        queued, failed = fresh.recover()
        self.assertEqual(failed, ["old-running"])
        self.assertEqual(queued, ["q1", "q2"])  # oldest first
        row = store.get_run("old-running")
        self.assertEqual((row["status"], row["error"]), ("failed", INTERRUPTED))
        self.assertEqual(self.launcher.order, ["q1"])
        self.assertEqual(fresh.waiting(), ["q2"])
        self.assertEqual(store.get_run("done")["status"], "done")
        self.launcher.procs["q1"].finish()
        _wait(lambda: "q2" in self.launcher.procs)
        self.launcher.procs["q2"].finish()
        fresh.join(5)

    def test_a_launcher_that_cannot_start_fails_that_run_only(self):
        from server.services.test_queue import TestQueue

        def broken(run_id: str):  # noqa: ANN202
            raise OSError("no python here")
        q = TestQueue(launcher=broken, cap=1)
        store.create_run({"run_id": "x", "status": "queued"})
        q.submit("x")
        row = store.get_run("x")
        self.assertEqual(row["status"], "failed")
        self.assertIn("could not start the runner", row["error"])


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class ResultTests(ApiTestBase):
    def _imported_run(self) -> str:
        from tests.stockify.test_engine_strategies import REFS, _load, _materialise, daily_world
        g, params = daily_world(5212)
        scratch = paths.lab_dir() / "packs-test"
        scratch.mkdir(exist_ok=True)
        pack, b = _materialise(g, params, scratch)
        folder = paths.runs_dir() / "api-run"
        engine.run_bundle(b, [pack], folder, strategy=_load(REFS["sma_cross"]), report=False)
        (folder / "runner.log").write_text("resolving\nrunning\n", encoding="utf-8")
        return runs.import_run(folder)

    def test_results(self):
        run_id = self._imported_run()
        d = self.client.get(f"/api/tests/{run_id}").json()
        self.assertEqual(d["run"]["status"], "done")
        self.assertEqual(d["run"]["nav_start_paise"], d["metrics"]["nav_start_paise"])
        self.assertIsInstance(d["run"]["nav_end_paise"], int)
        self.assertEqual(d["manifest"]["engine"], "engine")
        self.assertEqual(d["run"]["labels"], d["manifest"]["labels"])  # golden bundles carry none

        fills = self.client.get(f"/api/tests/{run_id}/fills").json()
        self.assertTrue(fills)
        self.assertTrue(all(isinstance(f["price_paise"], int) and f["t_ist"].endswith("+05:30") for f in fills))
        self.assertEqual({f["tradingsymbol"] for f in fills}, {"TEST"})  # from the run's bundle.json
        self.assertEqual([f["fill_id"] for f in fills], sorted(f["fill_id"] for f in fills))
        self.assertEqual(len(self.client.get(f"/api/tests/{run_id}/fills", params={"limit": 1}).json()), 1)

        orders = self.client.get(f"/api/tests/{run_id}/orders").json()
        self.assertEqual({o["order_id"] for o in orders} >= {f["order_id"] for f in fills}, True)

        equity = self.client.get(f"/api/tests/{run_id}/equity").json()
        self.assertEqual(equity[-1]["nav_paise"], d["run"]["nav_end_paise"])

        charges = self.client.get(f"/api/tests/{run_id}/charges").json()
        for c in charges:
            self.assertTrue(set(c["components"]) <= {"brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp",
                                                     "other"})
        self.assertEqual(sum(c["total_paise"] for c in charges), d["run"]["costs_total_paise"])

        logs = self.client.get(f"/api/tests/{run_id}/logs", params={"tail": 2}).json()
        self.assertEqual(logs["runner_log"], ["resolving", "running"])
        self.assertLessEqual(len(logs["events_log"]), 2)
        page = self.client.get(f"/api/tests/{run_id}/logs", params={"offset": 0, "limit": 3}).json()
        self.assertEqual(page["offset"], 0)
        self.assertEqual(page["total_lines"], logs["total_lines"])


@support.requires_fastapi
class RealSubprocessTests(SandboxedDBTest):
    """The queue's own launcher really starts `python -m stockify run` (I-9). The sandbox has no lab data, so
    the run fails at bundle resolution: cheap, and it proves the subprocess owns the status and writes
    runner.log."""
    db_modules = (store,)

    def test_the_runner_subprocess(self):
        from server.services.test_queue import TestQueue, subprocess_launcher
        q = TestQueue(launcher=subprocess_launcher, cap=1)
        run_id = runs.queue(EXAMPLE)
        q.submit(run_id)
        q.join(120)
        row = store.get_run(run_id)
        self.assertEqual(row["status"], "failed", row)
        self.assertIn("unknown symbol 'NSE:RELIANCE'", row["error"])  # the runner really ran, in the sandbox
        self.assertIn(run_id, (paths.runs_dir() / run_id / "runner.log").read_text(encoding="utf-8"))


@support.requires_fastapi
@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class CppFromTheFormTests(SandboxedDBTest):
    """WP 3.5 (4): the C++ strategies the form offers launch end to end through POST /api/tests and the real
    runner subprocess, on the M2.1 offline refdata and bars (test_bundle.BundleTest)."""
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        from datetime import date
        from fastapi.testclient import TestClient
        from server.main import app
        from server.services import test_queue
        from stockify import timeutil
        from stockify.data import history
        from tests.stockify.test_bundle import BundleTest
        fixture = BundleTest("setUp")  # its setUp writes the offline refdata and daily bars into this sandbox
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        prev = 10000  # up, then down: one SMA round trip (as test_runs_launch.CppConfigTests)
        with history._Tx() as conn:
            for d, c in {22: 10000, 23: 10050, 24: 10150, 26: 10200, 29: 10100, 30: 10000, 31: 9950}.items():
                conn.execute("UPDATE bars SET open_paise = ?, high_paise = ?, low_paise = ?, close_paise = ? "
                             "WHERE instrument_id = ? AND ts_ns = ?",
                             (prev, max(prev, c) + 10, min(prev, c) - 10, c, fixture.rel,
                              timeutil.ist_ns(date(2025, 12, d), "00:00")))
                prev = c
        # One run at a time: two runs that both have to build the same pack race in packs.build (the second's
        # rename onto a pack the first already has open fails on Windows); reported to the supervisor, not ours.
        self.queue = test_queue.TestQueue(launcher=test_queue.subprocess_launcher, cap=1)
        test_queue.set_queue(self.queue)
        self.addCleanup(test_queue.set_queue, None)
        self.client = TestClient(app)

    def test_sma_cross_cpp_and_orb_cpp_launch_from_the_form(self):
        from tests.stockify.test_bundle import run
        offered = {s["ref"] for s in self.client.get("/api/strategies").json() if s["kind"] == "cpp"}
        self.assertEqual(offered, {"sma_cross_cpp", "orb_cpp"})
        bad = run(name="orb_bad", strategy={"kind": "cpp", "ref": "orb_cpp", "params": {"symbol": "NSE:RELIANCE"}})
        r = self.client.post("/api/tests", json=bad)
        self.assertEqual((r.status_code, r.json()["detail"]), (422, ["strategy.params.qty: required"]))

        sma = self.client.post("/api/tests", json=run(name="sma_cpp", strategy={
            "kind": "cpp", "ref": "sma_cross_cpp", "params": {"symbol": "NSE:RELIANCE", "fast": 2, "slow": "3"}}))
        orb = self.client.post("/api/tests", json=run(name="orb_cpp", strategy={
            "kind": "cpp", "ref": "orb_cpp", "params": {"symbol": "NSE:RELIANCE", "qty": 10}}))
        self.assertEqual((sma.status_code, orb.status_code), (201, 201))
        self.queue.join(180)

        s = self.client.get(f"/api/tests/{sma.json()['run_id']}").json()
        self.assertEqual((s["run"]["status"], s["run"]["strategy_ref"], s["run"]["error"]), ("done", "sma_cross_cpp", None))
        fills = self.client.get(f"/api/tests/{sma.json()['run_id']}/fills").json()
        self.assertEqual([(f["side"], f["tradingsymbol"]) for f in fills], [("BUY", "RELIANCE"), ("SELL", "RELIANCE")])
        # 7 trading days, plus the final close-out (D-52): the last daily candle is knowable the next midnight
        self.assertEqual(len(self.client.get(f"/api/tests/{sma.json()['run_id']}/equity").json()), 8)

        o = self.client.get(f"/api/tests/{orb.json()['run_id']}").json()
        # daily bars only: the intraday strategy runs to the end and simply never trades
        self.assertEqual((o["run"]["status"], o["run"]["strategy_ref"], o["run"]["trades"]), ("done", "orb_cpp", 0))


if __name__ == "__main__":
    unittest.main()
