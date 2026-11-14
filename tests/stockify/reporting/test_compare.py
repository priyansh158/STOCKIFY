"""DMF 6.1: compare two to six finished runs side by side (logic, CLI, API)."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import unittest
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.research.fixtures import make_run

import stockify.store as store
from stockify import __main__ as cli
from stockify import compare as rc_old
from stockify import numerics, paths
from stockify.reporting import compare as rc
from stockify.research import plain


def _set_config(run_id: str, **extra) -> None:  # noqa: ANN003
    row = store.get_run(run_id)
    cfg = json.loads(row["config_json"])
    cfg.update(extra)
    store.update_run(run_id, config_json=numerics.dumps_canonical(cfg))


def _orb(range_minutes: int) -> dict:
    return {"strategy": {"kind": "python", "ref": "x:Orb",
                         "params": {"symbol": "NSE:A", "range_minutes": range_minutes}},
            "fill_model": {"kind": "bar"}, "margin": {"model": "flat"}}


class CompareBase(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        make_run("a", None, navs=[100_100_000, 100_300_000, 100_200_000], with_folder=True)
        make_run("b", None, navs=[99_900_000, 100_500_000, 101_000_000, 100_800_000, 100_900_000], with_folder=True)


class CompareLogicTests(CompareBase):
    def test_a_wrapped_strategy_reads_by_its_name_and_its_params_by_their_labels(self):
        """D-148: Margin Watch's inner_ref is shown as the strategy's display name (the raw ref stays in `values`),
        and the params it passes through are labelled from that strategy's own schema."""
        ref = "stockify.strategies.eod_short_straddle:EodShortStraddle"
        cfgs = [{"strategy": {"kind": "python", "ref": "stockify.strategies._margin_watch:MarginWatch",
                              "params": {"inner_ref": ref, "protection_bp": 5000}}}, {"strategy": {}}]
        self.assertEqual(rc._shown("strategy.params.inner_ref", [ref, None], cfgs), ["End-of-Day Short Straddle", None])
        labels = rc._param_labels(cfgs)
        self.assertEqual(labels["inner_ref"], "Strategy it runs")
        self.assertEqual(labels["protection_bp"], "Price band (basis points)")

    def test_two_to_six_runs_only(self):
        for ids in (["a"], [], ["a", "a"], list("abcdefg")):
            with self.assertRaisesRegex(ValueError, "compare 2 to 6 runs"):
                rc.compare_runs(ids)
        self.assertEqual(rc.compare_runs(["a", "b"]).run_ids, ["a", "b"])

    def test_unknown_run_is_a_key_error(self):
        with self.assertRaises(KeyError):
            rc.compare_runs(["a", "nope"])

    def test_equity_is_normalised_to_one_at_each_runs_start(self):
        c = rc.compare_runs(["a", "b"])
        self.assertAlmostEqual(c.equity[0]["values"][0], 1.001)
        self.assertAlmostEqual(c.equity[1]["values"][0], 0.999)
        self.assertAlmostEqual(c.equity[0]["values"][1], 1.003)

    def test_runs_of_different_lengths_keep_their_own_dates(self):
        c = rc.compare_runs(["a", "b"])
        self.assertEqual((len(c.equity[0]["dates"]), len(c.equity[1]["dates"])), (3, 5))
        self.assertEqual(c.equity[0]["dates"], c.equity[1]["dates"][:3])
        self.assertEqual(len(c.drawdown[1]["values"]), 5)

    def test_drawdown_series_hand_checked(self):
        make_run("d", None, navs=[110_000_000, 99_000_000, 110_000_000])
        make_run("e", None, navs=[100_000_000, 100_000_000])
        c = rc.compare_runs(["d", "e"])
        self.assertEqual(c.drawdown[0]["dates"], c.equity[0]["dates"])
        for got, want in zip(c.drawdown[0]["values"], [0.0, 0.1, 0.0]):
            self.assertAlmostEqual(got, want)

    def test_manifest_diff_lists_only_real_differences_as_differs(self):
        _set_config("a", **_orb(15))
        _set_config("b", **_orb(30))
        by = {r.field: r for r in rc.compare_runs(["a", "b"]).manifest_diff}
        self.assertTrue(by["strategy.params.range_minutes"].differs)
        self.assertEqual(by["strategy.params.range_minutes"].values, ["15", "30"])
        self.assertNotIn("strategy.params.symbol", by)
        self.assertFalse(by["fill_model"].differs)
        self.assertFalse(by["margin.model"].differs)
        self.assertTrue(by["config_sha256"].differs)

    def test_differing_labels_are_highlighted(self):
        store.update_run("a", labels_json=json.dumps(["fill:approximate", "same"]))
        store.update_run("b", labels_json=json.dumps(["same"]))
        c = rc.compare_runs(["a", "b"])
        self.assertEqual(c.differing_labels, ["fill:approximate"])
        self.assertEqual(c.labels["fill:approximate"], [True, False])
        self.assertEqual(c.labels["same"], [True, True])

    def test_missing_folder_falls_back_to_the_database_with_a_note(self):
        make_run("g", None, navs=[100_100_000])  # no folder
        store.update_run("g", bundle_sha256="ab" * 32, engine_build_id="build-1")
        c = rc.compare_runs(["a", "g"])
        self.assertIn("g: manifest not available (folder removed)", c.notes)
        self.assertEqual(len(c.notes), 1)
        by = {r.field: r for r in c.manifest_diff}
        self.assertEqual(by["bundle_sha256"].values[1], "ab" * 32)
        self.assertEqual(by["engine_build_id"].values[1], "build-1")

    def test_unusable_capital_gives_an_empty_series_and_a_note(self):
        for bad in (0, -5, "100", True, None):
            with self.subTest(capital=bad):
                _set_config("a", capital_paise=bad)
                store.update_run("a", metrics_json="{}")
                c = rc.compare_runs(["a", "b"])
                self.assertEqual(c.equity[0]["values"], [])
                self.assertEqual(c.drawdown[0]["values"], [])
                self.assertIn("a: no starting capital recorded", c.notes)
                self.assertEqual(len(c.equity[1]["values"]), 5)

    def test_bad_config_capital_falls_back_to_nav_start(self):
        _set_config("a", capital_paise="x")
        c = rc.compare_runs(["a", "b"])
        self.assertAlmostEqual(c.equity[0]["values"][0], 1.001)
        self.assertNotIn("a: no starting capital recorded", c.notes)

    def test_config_key_order_does_not_fake_a_difference(self):
        row = store.get_run("a")
        cfg = json.loads(row["config_json"])
        store.update_run("b", config_json=json.dumps(dict(reversed(list(cfg.items()))), indent=2))
        _set_config("b", name=cfg["name"])  # canonical dump, same content
        store.update_run("b", config_json=json.dumps(dict(reversed(list(cfg.items()))), indent=2))
        by = {r.field: r for r in rc.compare_runs(["a", "b"]).manifest_diff}
        self.assertFalse(by["config_sha256"].differs)

    def test_not_done_run_refused_naming_the_status(self):
        make_run("r", None, status="running")
        with self.assertRaisesRegex(ValueError, "running"):
            rc.compare_runs(["a", "r"])

    def test_money_rows_are_labelled_in_rupees_not_paise(self):
        # the page shows these rows in rupees, so the server's label must not say paise
        labels = {m.key: m.label for m in rc.compare_runs(["a", "b"]).metrics}
        self.assertEqual(labels["net_pnl"], "Net profit (₹)")
        self.assertEqual(labels["costs"], "Costs (₹)")
        self.assertFalse([k for k, v in labels.items() if "paise" in v])

    def test_every_metric_row_has_its_plain_meaning(self):
        c = rc.compare_runs(["a", "b"])
        keys = [m.key for m in c.metrics]
        self.assertEqual(keys, ["net_pnl", "return", "cagr", "sharpe", "sortino", "max_drawdown", "calmar", "trades",
                                "win_rate", "costs", "cost_share"])
        for m in c.metrics:
            self.assertEqual(m.plain, plain.meaning(m.key))
            self.assertIn(m.kind, ("money", "ratio", "pct", "count"))
            self.assertEqual(len(m.values), 2)
        self.assertEqual(c.metrics[0].values, [200_000, 900_000])  # int paise
        self.assertAlmostEqual(c.metrics[1].values[0], 0.002)


class CompareCliTests(CompareBase):
    def run_cli(self, *argv):  # noqa: ANN002, ANN201
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_prints_the_table(self):
        code, out, _ = self.run_cli("compare", "a", "b")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"(?m)^\s*run\s+a\s+b\s*$")
        self.assertRegex(out, r"net_pnl.*\*")
        self.assertIn("* marks a row where the runs differ", out)

    def test_cli_refuses_one_run_and_unknown_run(self):
        self.assertEqual(self.run_cli("compare", "a")[0], 2)
        code, _, err = self.run_cli("compare", "a", "nope")
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)

    def test_compare_artifacts_command_still_works(self):
        for rid in ("a", "b"):
            self.assertTrue((paths.run_path(rid) / "equity.csv").exists())
            (paths.run_path(rid) / "events_log.txt").write_text("", encoding="utf-8")
            for name in rc_old.CSVS:
                p = paths.run_path(rid) / name
                if not p.exists():
                    p.write_text("a,b\n", encoding="utf-8")
        code, _, err = self.run_cli("compare-artifacts", "a", "a")
        self.assertEqual(code, 0)
        self.assertIn("identical", err)


@support.requires_fastapi
class CompareApiTests(CompareBase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)

    def test_api_compare_repeated_ids_ok_422_404(self):
        r = self.client.get("/api/tests/compare", params=[("ids", "a"), ("ids", "b")])
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["run_ids"], ["a", "b"])
        self.assertEqual(set(body), {"run_ids", "names", "name_labels", "metrics", "equity", "drawdown", "labels", "differing_labels",
                                     "manifest_diff", "notes"})
        self.assertEqual(self.client.get("/api/tests/compare", params=[("ids", "a")]).status_code, 422)
        self.assertEqual(self.client.get("/api/tests/compare").status_code, 422)
        seven = [("ids", c) for c in "abcdefg"]
        self.assertEqual(self.client.get("/api/tests/compare", params=seven).status_code, 422)
        r = self.client.get("/api/tests/compare", params=[("ids", "a"), ("ids", "nope")])
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("Traceback", r.text)
        make_run("r", None, status="failed")
        r = self.client.get("/api/tests/compare", params=[("ids", "a"), ("ids", "r")])
        self.assertEqual(r.status_code, 422)
        self.assertIn("failed", r.json()["detail"])

    def test_a_manifest_that_cannot_be_read_falls_back_to_the_database(self):
        manifest = paths.run_path("a") / "manifest.json"
        manifest.unlink(missing_ok=True)
        manifest.mkdir()                                      # exists, but is not a readable file
        r = self.client.get("/api/tests/compare", params=[("ids", "a"), ("ids", "b")])
        self.assertEqual(r.status_code, 200)
        self.assertIn(rc.FALLBACK_NOTE, " ".join(r.json()["notes"]))
        shutil.rmtree(paths.run_path("b"))                    # a removed folder is the same fallback
        r = self.client.get("/api/tests/compare", params=[("ids", "a"), ("ids", "b")])
        self.assertEqual(r.status_code, 200)

    def test_an_unreadable_store_is_a_plain_409_not_a_bare_500(self):
        import sqlite3
        with mock.patch.object(store, "run_nav_series", side_effect=sqlite3.OperationalError("C:/secret/lab.sqlite")):
            r = self.client.get("/api/tests/compare", params=[("ids", "a"), ("ids", "b")])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json(), {"detail": "the runs could not be read right now"})

    def test_api_compare_accepts_a_comma_list(self):
        r = self.client.get("/api/tests/compare?ids=a,b")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["run_ids"], ["a", "b"])

    def test_api_compare_declared_before_the_run_id_route(self):
        r = self.client.get("/api/tests/compare?ids=a,b")
        self.assertNotIn("no test run", r.text)
        long = self.client.get("/api/tests/compare", params=[("ids", "x" * 500), ("ids", "a")])
        self.assertEqual(long.status_code, 422)
        self.assertLess(len(long.text), 300)

    def test_api_hostile_ids_never_500(self):
        for bad in ("..%2F..", "a/b", "..%5Cx", "%00"):
            r = self.client.get(f"/api/tests/compare?ids={bad}&ids=a")
            self.assertIn(r.status_code, (404, 422), bad)
            self.assertNotIn("Traceback", r.text)

    def test_api_json_has_no_non_finite_numbers(self):
        r = self.client.get("/api/tests/compare?ids=a,b")
        json.loads(r.text, parse_constant=lambda c: self.fail(c))


if __name__ == "__main__":
    unittest.main()
