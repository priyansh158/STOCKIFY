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


if __name__ == "__main__":
    unittest.main()
