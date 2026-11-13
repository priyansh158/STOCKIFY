"""M3.11: run rows in lab.sqlite (A1 §4), queueing, run ids and importing a run folder (stockify.runs)."""
from __future__ import annotations

import io
import json
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import engine, paths, runconfig, runs
from stockify import __main__ as cli

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "runs" / "sma_reliance_daily.json"


def _cli(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class StoreRunTests(SandboxedDBTest):
    db_modules = (store,)

    def test_create_and_get_with_defaults(self):
        store.create_run({"run_id": "a", "name": "n"})
        row = store.get_run("a")
        self.assertEqual(set(row), set(store.RUN_FIELDS))
        self.assertEqual((row["status"], row["name"], row["folder"]), ("queued", "n", None))
        self.assertIsInstance(row["created_ns"], int)
        self.assertIsNone(store.get_run("missing"))
        with self.assertRaises(ValueError):
            store.create_run({"run_id": "b", "colour": "red"})

    def test_update_only_if_status_never_overwrites_cancelled(self):
        store.create_run({"run_id": "a", "status": "running"})
        self.assertTrue(store.update_run("a", only_if_status="running", started_ns=5))
        self.assertTrue(store.update_run("a", status="cancelled"))
        self.assertFalse(store.update_run("a", only_if_status="running", status="done", finished_ns=9))
        self.assertFalse(store.update_run("a", only_if_status=("running", "queued"), status="failed"))
        row = store.get_run("a")
        self.assertEqual((row["status"], row["started_ns"], row["finished_ns"]), ("cancelled", 5, None))
        self.assertFalse(store.update_run("missing", status="done"))
        with self.assertRaises(ValueError):
            store.update_run("a", colour="red")

    def test_list_runs_newest_first_with_filters(self):
        for i, (status, fam) in enumerate([("done", "f1"), ("failed", "f1"), ("done", "f2"), ("queued", None)]):
            store.create_run({"run_id": f"r{i}", "status": status, "family_id": fam, "created_ns": 100 + i})
        self.assertEqual([r["run_id"] for r in store.list_runs()], ["r3", "r2", "r1", "r0"])
        self.assertEqual([r["run_id"] for r in store.list_runs(status="done")], ["r2", "r0"])
        self.assertEqual([r["run_id"] for r in store.list_runs(family_id="f1")], ["r1", "r0"])
        self.assertEqual([r["run_id"] for r in store.list_runs(status="done", family_id="f2")], ["r2"])
        self.assertEqual([r["run_id"] for r in store.list_runs(offset=1, limit=2)], ["r2", "r1"])

    def test_replace_run_rows_is_idempotent_and_ordered(self):
        orders = [{"order_id": 2, "side": "SELL"}, {"order_id": 1, "side": "BUY", "tag": None}]
        fills = [{"fill_id": 1, "order_id": 1, "t_ns": 10, "instrument_id": 7, "side": "BUY", "qty": 3,
                  "price_paise": 500}]
        equity = [{"trade_date": "2026-03-03", "nav_paise": 9, "cash_paise": 8, "margin_paise": 0},
                  {"trade_date": "2026-03-02", "nav_paise": 10, "cash_paise": 10, "margin_paise": 1}]
        charges = [{"row_no": 1, "kind": "order", "order_id": 1, "trade_date": "2026-03-02",
                    "components_json": '{"stt": 1}', "total_paise": 1}]
        for _ in range(2):
            store.replace_run_rows("a", orders=orders, fills=fills, equity=equity, charges=charges)
        store.replace_run_rows("b", orders=orders[:1], fills=[], equity=[], charges=[])
        self.assertEqual(store.run_rows("a", "orders"), [orders[1], orders[0]])
        self.assertEqual(store.run_rows("a", "fills"), fills)
        self.assertEqual([r["trade_date"] for r in store.run_rows("a", "equity")], ["2026-03-02", "2026-03-03"])
        self.assertEqual(store.run_rows("a", "charges"), charges)
        self.assertEqual(store.run_rows("a", "orders", offset=1, limit=5), [orders[0]])
        self.assertEqual(len(store.run_rows("b", "orders")), 1)
        store.replace_run_rows("a", orders=[], fills=[], equity=[], charges=[])
        self.assertEqual(store.run_rows("a", "fills"), [])
        with self.assertRaises(ValueError):
            store.run_rows("a", "ticks")

    def test_a_failed_replace_leaves_the_old_rows(self):
        store.replace_run_rows("a", orders=[{"order_id": 1}], fills=[], equity=[], charges=[])
        with self.assertRaises(KeyError):
            store.replace_run_rows("a", orders=[{"order_id": 1}, {"no_id": 2}], fills=[], equity=[], charges=[])
        self.assertEqual(store.run_rows("a", "orders"), [{"order_id": 1}])


class QueueAndIdTests(SandboxedDBTest):
    db_modules = (store,)

    def test_queue_writes_the_request_and_a_queued_row(self):
        run_id = runs.queue(EXAMPLE)
        self.assertRegex(run_id, r"^sma_reliance_daily-\d{8}T\d{6}-[0-9a-f]{6}$")
        row = store.get_run(run_id)
        self.assertEqual((row["status"], row["name"], row["family_id"], row["impl"]),
                         ("queued", "sma_reliance_daily", "sma_cross/RELIANCE/daily", "engine"))
        folder = paths.runs_dir() / run_id
        self.assertEqual(row["folder"], str(folder))
        request = (folder / "request.json").read_text(encoding="utf-8")
        self.assertEqual(request, row["config_json"] + "\n")
        self.assertEqual(json.loads(request), runconfig.load(EXAMPLE))

    def test_a_bad_config_is_refused_and_nothing_is_written(self):
        with self.assertRaises(runconfig.RunConfigError):
            runs.queue({"name": "x"})
        self.assertEqual(store.list_runs(), [])
        self.assertEqual(list(paths.runs_dir().iterdir()), [])

    def test_ids_never_reuse_a_folder_or_a_row(self):
        self.assertEqual(runs.unique_run_id("n-abc"), "n-abc")
        (paths.runs_dir() / "n-abc").mkdir()
        self.assertEqual(runs.unique_run_id("n-abc"), "n-abc-2")
        store.create_run({"run_id": "n-abc-2"})
        self.assertEqual(runs.unique_run_id("n-abc"), "n-abc-3")
        t = 1_793_677_500_000_000_000  # 2026-11-03 03:45 UTC
        self.assertRegex(runs.queued_run_id("n", t), r"^n-20261103T034500-[0-9a-f]{6}$")
        a = runs.bundle_run_id("n", {"x": 1})
        self.assertRegex(a, r"^n-[0-9a-f]{12}$")
        self.assertEqual(runs.bundle_run_id("n", {"x": 1}), a)  # nothing taken yet: the same id
        (paths.runs_dir() / a).mkdir()
        self.assertEqual(runs.bundle_run_id("n", {"x": 1}), a + "-2")

    def test_launch_refuses_a_run_id_that_is_not_queued(self):
        with self.assertRaises(runs.RunNotQueued):
            runs.launch(run_id="nope")
        store.create_run({"run_id": "d", "status": "done"})
        with self.assertRaises(runs.RunNotQueued):
            runs.launch(run_id="d")
        self.assertEqual(store.get_run("d")["status"], "done")
        code, _, err = _cli("run", "--run-id", "d")
        self.assertEqual(code, 2)
        self.assertIn("not queued", err)

    def test_cli_usage_and_bad_config_exit_2(self):
        self.assertEqual(_cli("run")[0], 2)
        bad = paths.runs_dir() / "bad.json"
        bad.write_text('{"name": "x"}', encoding="utf-8")
        code, out, err = _cli("run", "--config", str(bad))
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err)
        with self.assertRaises(SystemExit):
            _cli("run", "--config", str(bad), "--rerun", "x")


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class ImportRunTests(SandboxedDBTest):
    db_modules = (store,)

    def _engine_run(self, run_id: str) -> Path:
        from tests.stockify.test_engine_strategies import _load, _materialise, daily_world, REFS
        g, params = daily_world(5212)
        scratch = paths.lab_dir() / "packs-test"
        scratch.mkdir(exist_ok=True)
        pack, b = _materialise(g, params, scratch)
        folder = paths.runs_dir() / run_id
        engine.run_bundle(b, [pack], folder, strategy=_load(REFS["sma_cross"]), report=False)
        return folder

    def test_import_gives_typed_rows(self):
        folder = self._engine_run("t-run")
        self.assertEqual(runs.import_run(folder), "t-run")
        row = store.get_run("t-run")
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((row["status"], row["impl"], row["folder"]), ("done", "engine", str(folder)))
        self.assertEqual(row["bundle_sha256"], manifest["bundle_sha256"])
        self.assertEqual(row["engine_build_id"], engine.build_info()["engine_build_id"])
        self.assertEqual(json.loads(row["labels_json"]), manifest["labels"])
        self.assertEqual(json.loads(row["metrics_json"]),
                         json.loads((folder / "metrics.json").read_text(encoding="utf-8")))
        self.assertEqual(row["name"], "D5212")
        self.assertGreater(row["finished_ns"], 0)

        orders = store.run_rows("t-run", "orders")
        csv_orders = runs.read_csv(folder / "orders.csv")
        self.assertGreater(len(orders), 2)
        self.assertEqual(orders, csv_orders)
        o = orders[0]
        self.assertIsInstance(o["qty"], int)
        self.assertIsInstance(o["t_submit_ns"], int)
        self.assertIsInstance(o["side"], str)
        self.assertTrue(all(x["protection_bp"] is None or isinstance(x["protection_bp"], int) for x in orders))
        self.assertTrue(any(x["reason"] is None for x in orders))  # an empty reason cell
        fills = store.run_rows("t-run", "fills")
        self.assertEqual([f["fill_id"] for f in fills], [f["fill_id"] for f in runs.read_csv(folder / "fills.csv")])
        self.assertIsInstance(fills[0]["price_paise"], int)
        eq_csv = runs.read_csv(folder / "equity.csv")
        eq = store.run_rows("t-run", "equity")
        self.assertEqual([(e["trade_date"], e["nav_paise"], e["margin_paise"]) for e in eq],
                         [(e["trade_date"], e["nav_paise"], e["margin_blocked_paise"]) for e in eq_csv])
        ch = store.run_rows("t-run", "charges")
        ch_csv = runs.read_csv(folder / "charges.csv")
        self.assertEqual(len(ch), len(ch_csv))
        self.assertGreater(len(ch), 2)
        comps = json.loads(ch[0]["components_json"])
        self.assertEqual(comps, {c: ch_csv[0][c] for c in runs.CHARGE_COMPONENTS})
        self.assertEqual(ch[0]["total_paise"], ch_csv[0]["total"])

        runs.import_run(folder)  # idempotent
        self.assertEqual(len(store.run_rows("t-run", "orders")), len(orders))
        self.assertEqual(len(store.list_runs()), 1)

    def test_import_keeps_a_cancelled_status(self):
        folder = self._engine_run("t-cancel")
        store.create_run({"run_id": "t-cancel", "status": "cancelled"})
        runs.import_run(folder)
        row = store.get_run("t-cancel")
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["metrics_json"])


if __name__ == "__main__":
    unittest.main()
