"""M6.4 / M6.8 (WF 6.1): GET /api/tests/{id}/research and /api/research/families."""
from __future__ import annotations

import dataclasses
import json
import random
import unittest
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import paths
from stockify.data import refdata
from stockify.research import report
from tests.stockify.research.fixtures import make_run

FAM = "strat/ds-1m-RELIANCE/sharpe"
OTHER = "strat/ds-1m-TCS/sharpe"


def noisy(seed: int, n: int = 30, drift: float = 0.0005) -> list[float]:
    rng = random.Random(seed)
    return [drift + rng.uniform(-0.01, 0.01) for _ in range(n)]


@support.requires_fastapi
class ResearchApiTests(SandboxedDBTest):
    db_modules = (store, refdata)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)
        for fam in (FAM, OTHER):
            store.add_family(fam, objective="sharpe", description=f"family {fam}")
        for i in range(3):
            make_run(f"r{i}", FAM, daily_returns=noisy(i), name=f"try{i}")
        make_run("lone", OTHER, daily_returns=noisy(9))

    def test_run_research_returns_dsr_and_bootstrap(self):
        r = self.client.get("/api/tests/r0/research")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(list(j), ["run_id", "family_id", "role", "objective", "n_trials", "rank", "is_best",
                                   "picked_run_id", "picked_name", "dsr", "bootstrap", "regimes",
                                   "holdout_unlocks", "plain"])
        self.assertEqual((j["run_id"], j["family_id"], j["n_trials"]), ("r0", FAM, 3))
        self.assertEqual(list(j["dsr"]), ["sr", "t", "skew", "kurt", "n_trials", "var_sharpe", "sr0", "dsr", "psr"])
        self.assertEqual(j["dsr"]["t"], 30)
        self.assertEqual(list(j["bootstrap"]), ["n_days", "mean_block", "resamples", "ci", "enough_days", "sharpe",
                                                "mean_daily_return", "max_drawdown"])
        self.assertEqual(list(j["bootstrap"]["sharpe"]), ["point", "lo", "hi"])
        reg = j["regimes"]
        self.assertEqual(list(reg), ["month", "weekday", "expiry", "vix", "gap", "vix_source", "notes"])
        self.assertEqual(list(reg["month"][0]), ["label", "days", "pnl_paise", "mean_pnl_paise", "up_days",
                                                 "up_share", "thin"])
        for dim in ("month", "weekday", "expiry", "vix", "gap"):
            self.assertEqual(sum(b["days"] for b in reg[dim]), 30, dim)
        self.assertEqual([b["label"] for b in reg["vix"]], ["no data"])
        self.assertIn("regimes_caveat", j["plain"])
        self.assertIn("dsr", j["plain"])

    def test_run_research_404(self):
        r = self.client.get("/api/tests/nope/research")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("Traceback", r.text)

    def test_run_research_for_a_failed_run_is_empty_not_an_error(self):
        store.create_run({"run_id": "bad", "status": "failed", "family_id": FAM, "created_ns": 1, "error": "X: y"})
        r = self.client.get("/api/tests/bad/research")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertIsNone(j["dsr"])
        self.assertIsNone(j["bootstrap"])
        self.assertTrue(j["plain"]["dsr"])

    def test_families_list_and_detail(self):
        r = self.client.get("/api/research/families")
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        self.assertEqual([f["family_id"] for f in rows], [FAM, OTHER])
        self.assertEqual(rows[0]["n_trials"], 3)
        self.assertEqual(len(self.client.get("/api/research/families", params={"limit": 1}).json()), 1)
        self.assertEqual(self.client.get("/api/research/families", params={"offset": 1}).json()[0]["family_id"],
                         OTHER)
        for bad in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
            self.assertEqual(self.client.get("/api/research/families", params=bad).status_code, 422)
        d = self.client.get(f"/api/research/families/{FAM}")
        self.assertEqual(d.status_code, 200)
        j = d.json()
        self.assertEqual(j["family_id"], FAM)
        self.assertEqual(len(j["trials"]), 3)
        self.assertEqual(j["trials"][0]["days"], 30)
        self.assertIsNotNone(j["best"])
        self.assertIn("pbo", j)                                 # a PboDTO or None, with its note
        self.assertTrue(j["pbo"] is not None or j["pbo_note"])
        self.assertEqual(self.client.get("/api/research/families/no/such/family").status_code, 404)
        self.assertEqual(self.client.get("/api/research/families/../../x").status_code, 404)

    def test_responses_have_no_nan(self):
        res = report.run_research("r0")
        nan = float("nan")
        bad = dataclasses.replace(res, dsr=dataclasses.replace(res.dsr, skew=nan, psr=float("inf")),
                                  bootstrap=dataclasses.replace(
                                      res.bootstrap, sharpe=dataclasses.replace(res.bootstrap.sharpe, point=nan)))
        with mock.patch.object(report, "run_research", return_value=bad):
            r = self.client.get("/api/tests/r0/research")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["dsr"]["skew"])
        self.assertIsNone(r.json()["dsr"]["psr"])
        self.assertIsNone(r.json()["bootstrap"]["sharpe"]["point"])
        for url in ("/api/tests/r0/research", "/api/research/families", f"/api/research/families/{FAM}"):
            text = self.client.get(url).text
            for token in ("NaN", "Infinity"):
                self.assertNotIn(token, text)
            json.loads(text, parse_constant=lambda c: self.fail(c))

    def test_non_loopback_host_refused(self):
        for url in ("/api/tests/r0/research", "/api/research/families", f"/api/research/families/{FAM}"):
            r = self.client.get(url, headers={"Host": "evil.example"})
            self.assertEqual(r.status_code, 400)
            self.assertNotIn("dsr", r.text)


@support.requires_fastapi
class SensitivityApiTests(SandboxedDBTest):
    db_modules = (store, refdata)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)
        make_run("s0", None, with_folder=True, name="s0")
        self.folder = paths.run_path("s0")

    def _write(self, text):
        (self.folder / "sensitivity.json").write_text(text, encoding="utf-8")

    def _doc(self, **over):
        def row(key, axis, st, net, err=None):
            return {"key": key, "axis": axis, "label": key, "run_id": None if net is None else "x", "status": st,
                    "net_pnl_paise": net, "sharpe": float("nan") if net is not None else None,
                    "delta_net_paise": None if net is None else net - 4_120_000, "error": err}
        d = {"schema": "stockify.sensitivity/1", "run_id": "s0", "job_id": "sens-s0", "verdict": "fragile",
             "fragile": True, "reasons": ["r"], "baseline_net_paise": 4_120_000, "fragile_cost_scale": "1.5",
             "fragile_extra_ticks": 1,
             "rows": [row("baseline", "baseline", "baseline", 4_120_000), row("costs_x1.5", "costs", "done", -310_000),
                      row("slip_plus1", "slippage", "done", 3_000_000)]}
        d.update(over)
        return json.dumps(d)          # the NaN sharpe is written as the bare token NaN, as a hand-edit might

    def test_api_returns_404_until_run(self):
        r = self.client.get("/api/tests/s0/sensitivity")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "not run yet; run `python -m stockify sensitivity --run s0`")
        self.assertEqual(self.client.get("/api/tests/nope/sensitivity").status_code, 404)
        for bad in ("{not json", "[]", json.dumps({"rows": 3}), self._doc(verdict="maybe"), self._doc(run_id="other"), self._doc(verdict="robust", fragile=False)):
            self._write(bad)
            r = self.client.get("/api/tests/s0/sensitivity")
            self.assertEqual(r.status_code, 404, bad)
            self.assertIn("not run yet", r.json()["detail"])

    def test_api_returns_the_stored_result_without_nan(self):
        self._write(self._doc())
        r = self.client.get("/api/tests/s0/sensitivity")
        self.assertEqual(r.status_code, 200)
        json.loads(r.text, parse_constant=lambda c: self.fail(c))
        j = r.json()
        self.assertEqual(list(j), ["run_id", "job_id", "rows", "baseline_net_paise", "fragile", "verdict", "reasons",
                                   "plain"])
        self.assertEqual((j["run_id"], j["verdict"], j["fragile"], j["baseline_net_paise"]),
                         ("s0", "fragile", True, 4_120_000))
        self.assertEqual(list(j["rows"][0]), ["key", "axis", "label", "run_id", "status", "net_pnl_paise", "sharpe",
                                              "delta_net_paise", "error"])
        self.assertIsNone(j["rows"][0]["sharpe"])
        self.assertEqual(j["plain"], "Breaks if all costs rise by half: net P&L turns from +₹41,200 to -₹3,100.")

    def test_api_refuses_an_unsafe_run_id(self):
        for rid in ("..", "%2e%2e", "a%5cb", "C%3A", "x" * 200, "..%2f..%2fetc"):
            r = self.client.get(f"/api/tests/{rid}/sensitivity")
            self.assertEqual(r.status_code, 404, rid)
            self.assertNotIn("Traceback", r.text)



@support.requires_fastapi
class WalkforwardApiTests(SandboxedDBTest):
    """WF 6.6: GET /api/research/walkforward/{job_id}."""
    db_modules = (store, refdata)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        from stockify.research import stats, walkforward as wf
        from tests.stockify.research.test_walkforward_finish import JOB, build_job, two_windows
        self.job = JOB
        p = mock.patch.object(stats, "risk_free_rows", return_value=[])
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(app, base_url=support.API_BASE_URL)
        self.build_job, self.two_windows, self.wf = build_job, two_windows, wf

    def finished(self):
        self.build_job(self.two_windows())
        self.wf.finish(self.job)

    def test_api_walkforward_detail_and_404(self):
        self.finished()
        r = self.client.get(f"/api/research/walkforward/{self.job}")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(list(j), ["job_id", "name", "family_id", "objective", "train", "test", "step", "summary",
                                   "windows", "stitched_dates", "stitched_index", "plain"])
        self.assertEqual((j["job_id"], j["train"], j["test"], j["step"], j["objective"]),
                         (self.job, 120, 30, 30, "sharpe"))
        self.assertEqual(j["family_id"], f"walkforward/{self.job}")
        self.assertEqual(list(j["summary"]), ["windows", "is_windows", "oos_days", "is_sharpe", "oos_sharpe", "ratio",
                                              "chosen_modal_share", "n_trials", "var_sharpe", "dsr"])
        self.assertEqual((j["summary"]["windows"], j["summary"]["oos_days"], j["summary"]["n_trials"]), (2, 12, 4))
        self.assertEqual(list(j["summary"]["dsr"]), ["sr", "t", "skew", "kurt", "n_trials", "var_sharpe", "sr0",
                                                     "dsr", "psr"])
        self.assertEqual(len(j["windows"]), 2)
        self.assertEqual(list(j["windows"][0]), ["index", "train_from", "train_to", "test_from", "test_to",
                                                 "chosen_params", "chosen_run_id", "test_run_id", "test_status",
                                                 "error", "train", "test"])
        self.assertIsInstance(j["windows"][0]["test"]["net_pnl_paise"], int)
        self.assertEqual((len(j["stitched_dates"]), len(j["stitched_index"])), (12, 12))
        self.assertEqual(sorted(j["plain"]), ["dsr", "holdout", "pbo", "ratio", "stability"])
        self.assertIn("of the in-sample Sharpe out of sample", j["plain"]["ratio"])
        self.assertIn("the windows cover different days", j["plain"]["pbo"])
        # unknown, unsafe and other-kind jobs are 404
        for bad in ("wf-nothing", "..%2F..%2Fx", "a%5Cb"):
            self.assertEqual(self.client.get(f"/api/research/walkforward/{bad}").status_code, 404, bad)
        self.assertEqual(self.client.get("/api/research/walkforward/").status_code, 404)
        store.create_research_job({"job_id": "sw-9", "kind": "sweep", "name": "s", "status": "done", "created_ns": 1})
        self.assertEqual(self.client.get("/api/research/walkforward/sw-9").status_code, 404)

    def test_a_job_that_is_not_finished_is_409_and_says_nothing_of_its_error(self):
        self.build_job(self.two_windows())                       # still running
        r = self.client.get(f"/api/research/walkforward/{self.job}")
        self.assertEqual(r.status_code, 409)
        self.assertIn("not finished", r.json()["detail"])
        store.update_research_job(self.job, status="failed", error="boom at C:\\secret\\path")
        r = self.client.get(f"/api/research/walkforward/{self.job}")
        self.assertEqual(r.status_code, 409)
        self.assertNotIn("secret", r.text)

    def test_a_done_job_whose_file_is_gone_is_409_not_500(self):
        self.finished()
        (paths.research_path(self.job) / "walkforward.json").unlink()
        r = self.client.get(f"/api/research/walkforward/{self.job}")
        self.assertEqual(r.status_code, 409)
        self.assertNotIn("Traceback", r.text)

    def test_a_window_error_with_a_path_is_not_echoed(self):
        self.build_job(self.two_windows(w1=dict(test=None, chosen=0)))
        state = json.loads(store.get_research_job(self.job)["state_json"])
        state["windows"][1]["error"] = "crashed in C:\\Users\\x\\file.py"
        store.update_research_job(self.job, state_json=json.dumps(state))
        self.wf.finish(self.job)
        j = self.client.get(f"/api/research/walkforward/{self.job}").json()
        self.assertIsNone(j["windows"][1]["error"])
        self.assertEqual(j["windows"][1]["test_status"], "failed")
        self.assertNotIn("Users", json.dumps(j))

    def test_walkforward_route_is_not_shadowed_by_the_family_route(self):
        self.finished()
        fam = f"walkforward/{self.job}"
        self.assertEqual(self.client.get(f"/api/research/families/{fam}").status_code, 200)     # the family path
        self.assertEqual(self.client.get(f"/api/research/walkforward/{self.job}").json()["job_id"], self.job)

    def test_non_loopback_host_refused(self):
        self.finished()
        r = self.client.get(f"/api/research/walkforward/{self.job}", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
