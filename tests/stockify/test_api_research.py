"""M6.4 / M6.8 (WF 6.1): GET /api/tests/{id}/research and /api/research/families."""
from __future__ import annotations

import dataclasses
import json
import random
import sqlite3
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
                                   "regimes_error", "holdout_unlocks", "plain"])
        self.assertEqual((j["run_id"], j["family_id"], j["n_trials"]), ("r0", FAM, 3))
        self.assertEqual(list(j["dsr"]), ["sr", "t", "skew", "kurt", "n_trials", "var_sharpe", "sr0", "dsr", "psr"])
        self.assertEqual(j["dsr"]["t"], 30)
        self.assertEqual(list(j["bootstrap"]), ["n_days", "mean_block", "resamples", "ci", "enough_days", "sharpe",
                                                "mean_daily_return", "max_drawdown"])
        self.assertEqual(list(j["bootstrap"]["sharpe"]), ["point", "lo", "hi"])
        reg = j["regimes"]
        self.assertEqual(list(reg), ["month", "weekday", "expiry", "vix", "gap", "vix_source", "notes"])
        self.assertEqual(list(reg["month"][0]), ["label", "days", "pnl_paise", "mean_pnl_paise", "up_days",
                                                 "up_share", "thin", "plain"])
        for dim in ("month", "weekday", "expiry", "vix", "gap"):
            self.assertEqual(sum(b["days"] for b in reg[dim]), 30, dim)
        self.assertEqual([b["label"] for b in reg["vix"]], ["no data"])
        self.assertIn("regimes_caveat", j["plain"])
        self.assertIn("dsr", j["plain"])

    def test_run_research_carries_the_server_meanings_and_bucket_sentences(self):
        from stockify.research import plain
        j = self.client.get("/api/tests/r0/research").json()
        for key in ("trials", "holdout_unlocks", "regime_month", "regime_weekday", "regime_expiry", "regime_vix",
                    "regime_gap"):
            self.assertEqual(j["plain"][key], plain.meaning(key), key)
        self.assertEqual(j["plain"]["dsr_meaning"], plain.meaning("dsr"))
        rep = report.run_research("r0")
        for dim in ("month", "weekday", "expiry", "vix", "gap"):
            want = [plain.regime_sentence(b) for b in getattr(rep.regimes, dim)]
            self.assertEqual([b["plain"] for b in j["regimes"][dim]], want, dim)

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
        self.assertIsNone(j["regimes"])
        self.assertIsNone(j["regimes_error"])             # nothing to split is a true "no daily results", not an error
        self.assertFalse([k for k in j["plain"] if k.startswith("regime_")])

    def test_a_failed_regime_split_is_sent_as_could_not_be_worked_out(self):
        from stockify.research import regimes
        with mock.patch.object(regimes, "build", side_effect=sqlite3.InterfaceError("bad parameter")):
            j = self.client.get("/api/tests/r0/research").json()
        self.assertIsNone(j["regimes"])
        self.assertEqual(j["regimes_error"], report.REGIMES_UNAVAILABLE)
        self.assertNotIn("no daily results", j["regimes_error"])

    def test_families_list_and_detail(self):
        r = self.client.get("/api/research/families")
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        self.assertEqual([f["family_id"] for f in rows], [FAM, OTHER])
        self.assertEqual(rows[0]["n_trials"], 3)
        self.assertEqual(rows[0]["n_finished"], 3)
        self.assertIn("best of 3 tries", rows[0]["best_plain"])
        self.assertEqual(rows[1]["best_plain"], "the only try, so nothing to allow for")
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
        self.assertEqual(r.json()["detail"], "the cost check has not been run for this test yet")
        self.assertEqual(self.client.get("/api/tests/nope/sensitivity").status_code, 404)
        for bad in ("{not json", "[]", json.dumps({"rows": 3}), self._doc(verdict="maybe"), self._doc(run_id="other"), self._doc(verdict="robust", fragile=False)):
            self._write(bad)
            r = self.client.get("/api/tests/s0/sensitivity")
            self.assertEqual(r.status_code, 404, bad)
            self.assertIn("has not been run", r.json()["detail"])

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

    def test_no_raw_error_text_reaches_the_response_but_a_not_applicable_row_says_why(self):
        # what the stored file can hold: a path, an exception class name, a validator message with values, a resolver text
        raw = {"costs_x1.5": ("failed", "ValueError: bad window in C:" + chr(92) + "Users" + chr(92) + "x" + chr(92) + "run.py"),
               "slip_plus1": ("not_applicable", "fill_model.slippage.extra_ticks: must be at most 1000, got 1001"),
               "fill_eod_close": ("not_applicable", "price_basis: eod_close runs use NSE files, basis raw"),
               "fill_bar": ("not_applicable", "data.packs[0]: pack 9f2c not found in D:/lab/packs"),
               "fill_snapshot_ltp": ("failed", "the run failed")}
        d = json.loads(self._doc())
        d["rows"] = [d["rows"][0]] + [
            {"key": k, "axis": "x", "label": k, "run_id": None, "status": st, "net_pnl_paise": None, "sharpe": None,
             "delta_net_paise": None, "error": e} for k, (st, e) in raw.items()]
        d["verdict"], d["fragile"] = "inconclusive", False
        # an older file: the reasons sentence has the raw text inside it
        d["reasons"] = ["The 1.5 times all costs case could not be checked (ValueError: bad window in C:" + chr(92) + "x).",
                        "The 1 extra slippage tick case could not be checked (fill_model.slippage.extra_ticks: got 1001).",
                        "The other case could not be checked (it did not run).", "Not called robust: x."]
        self._write(json.dumps(d))
        r = self.client.get("/api/tests/s0/sensitivity")
        self.assertEqual(r.status_code, 200)
        from stockify.research import plain
        rows = {x["key"]: x["error"] for x in r.json()["rows"]}
        self.assertEqual(rows, {"baseline": None, "costs_x1.5": plain.GENERIC_FAILURE,
                                "slip_plus1": plain.NOT_RUN_EXTRA_TICKS, "fill_eod_close": plain.NOT_RUN_RAW_PRICES,
                                "fill_bar": plain.NOT_RUN_DATA, "fill_snapshot_ltp": "the run failed"})
        for leak in ("bad window", "ValueError", "run.py", "Users", "1001", "must be at most", "9f2c", "D:/lab", "extra_ticks",
                     "price_basis"):
            self.assertNotIn(leak, r.text, leak)
        self.assertEqual(r.json()["reasons"], [
            f"The 1.5 times all costs case could not be checked ({plain.GENERIC_FAILURE}).",
            f"The 1 extra slippage tick case could not be checked ({plain.NOT_RUN_EXTRA_TICKS}).",
            "The other case could not be checked (it did not run).", "Not called robust: x."])

    def test_api_refuses_an_unsafe_run_id(self):
        for rid in ("..", "%2e%2e", "a%5cb", "C%3A", "x" * 200, "..%2f..%2fetc"):
            r = self.client.get(f"/api/tests/{rid}/sensitivity")
            self.assertEqual(r.status_code, 404, rid)
            self.assertNotIn("Traceback", r.text)


def sweep_doc(job_id="sweep-t-1", objective="sharpe", axes=None, rows=None, **over) -> dict:
    """A stored sweep result (stockify.research.sweep RESULT_SCHEMA) over a 2 x 3 grid by default."""
    axes = axes if axes is not None else {"strategy.params.fast": [5, 10], "strategy.params.slow": [20, 30, 40]}
    if rows is None:
        rows, vals, n = [], [0.5, -0.2, 1.0, 0.1, 0.9, None], 0
        for f in axes["strategy.params.fast"]:
            for sl in axes["strategy.params.slow"]:
                v = vals[n]
                rows.append({"index": n, "params": {"strategy.params.fast": f, "strategy.params.slow": sl},
                             "run_id": f"run{n}", "status": "done" if v is not None else "failed",
                             "objective_value": v, "sharpe": v, "net_pnl_paise": None if v is None else int(v * 1000),
                             "trades": 3, "max_drawdown": 0.1, "error": None if v is not None else "boom"})
                n += 1
    ok = [r for r in rows if r["objective_value"] is not None]
    best = max(ok, key=lambda r: (r["objective_value"], -r["index"]))["run_id"] if ok else None
    d = {"schema": "stockify.sweep.result/1", "job_id": job_id, "name": "t", "family_id": f"sweep/{job_id}",
         "objective": objective, "axes": axes, "best_run_id": best, "rows": rows}
    d.update(over)
    return d


def _row(i: int, params: dict, value) -> dict:  # noqa: ANN001
    return {"index": i, "params": params, "run_id": f"r{i}", "status": "done", "objective_value": value,
            "sharpe": None, "net_pnl_paise": None, "trades": None, "max_drawdown": None, "error": None}


def add_sweep_job(doc: dict | None, job_id="sweep-t-1", kind="sweep", status="done", created_ns=1000) -> None:
    store.create_research_job({
        "job_id": job_id, "kind": kind, "name": "t", "family_id": f"sweep/{job_id}", "status": status,
        "created_ns": created_ns, "finished_ns": created_ns + 5,
        "state_json": None if doc is None else json.dumps(doc)})     # json.dumps writes a bare NaN as it is given


@support.requires_fastapi
class SweepApiTests(SandboxedDBTest):
    db_modules = (store, refdata)
    X, Y = "strategy.params.fast", "strategy.params.slow"

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)

    def test_the_jobs_list_carries_a_plain_reason_never_the_raw_error(self):
        b = chr(92)
        raw = {"sweep-v": "ValueError: bad window", "sens-p": "boom at C:" + b + "secret" + b + "path",
               "wf-h": "HoldoutLocked: the span C:/x/y crosses the holdout", "wf-r": "WalkforwardRunError: 1 for 2",
               "wf-k": "KeyError: 'secret-key'"}
        for i, (jid, err) in enumerate(raw.items()):
            kind = "walkforward" if jid.startswith("wf") else "sweep"
            add_sweep_job(None, jid, kind=kind, status="failed", created_ns=i + 1)
            store.update_research_job(jid, error=err)
        add_sweep_job(None, "wf-new", kind="walkforward", status="failed", created_ns=9)
        store.update_research_job("wf-new", error="ValueError: bad window",
                                  state_json=json.dumps({"windows": [], "failure_reason": "the holdout is locked"}))
        add_sweep_job(None, "wf-ok", kind="walkforward", status="running", created_ns=10)
        r = self.client.get("/api/research/jobs")
        for needle in ("bad window", "ValueError", "secret", "KeyError", "crosses the holdout", "1 for 2"):
            self.assertNotIn(needle, r.text, needle)
        got = {j["job_id"]: j["error"] for j in r.json()}
        self.assertEqual(got, {
            "sweep-v": "Failed", "sens-p": "Failed", "wf-h": "the holdout is locked",
            "wf-r": "the run pool returned the wrong number of results", "wf-k": "Failed",
            "wf-new": "the holdout is locked", "wf-ok": None})

    def test_counts_give_the_totals_behind_the_capped_overview_lists(self):
        self.assertEqual(self.client.get("/api/research/counts").json(),
                         {"families": 0, "sweeps": 0, "walkforwards": 0})
        store.add_family("strat/x/sharpe", objective="sharpe", description=None)
        add_sweep_job(sweep_doc("sweep-a"), "sweep-a", created_ns=1)
        add_sweep_job(sweep_doc("sweep-b"), "sweep-b", created_ns=2)
        add_sweep_job(None, "wf-1", kind="walkforward", status="running", created_ns=3)
        add_sweep_job(None, "s-1", kind="sensitivity", created_ns=4)
        self.assertEqual(self.client.get("/api/research/counts").json(),
                         {"families": 1, "sweeps": 2, "walkforwards": 1})

    def test_jobs_list_filters_by_kind(self):
        add_sweep_job(sweep_doc("sweep-a"), "sweep-a", created_ns=1)
        add_sweep_job(sweep_doc("sweep-b"), "sweep-b", created_ns=2)
        add_sweep_job(None, "wf-1", kind="walkforward", status="running", created_ns=3)
        r = self.client.get("/api/research/jobs")
        self.assertEqual([j["job_id"] for j in r.json()], ["wf-1", "sweep-b", "sweep-a"])
        self.assertEqual(list(r.json()[0]), ["job_id", "kind", "name", "name_label", "family_id", "status", "created_ns",
                                             "finished_ns", "error", "plain"])
        self.assertEqual([j["plain"] for j in r.json()], ["still running", "finished", "finished"])
        r = self.client.get("/api/research/jobs", params={"kind": "sweep"})
        self.assertEqual([j["job_id"] for j in r.json()], ["sweep-b", "sweep-a"])
        self.assertEqual(self.client.get("/api/research/jobs", params={"kind": "sweep", "limit": 1,
                                                                       "offset": 1}).json()[0]["job_id"], "sweep-a")
        self.assertEqual(self.client.get("/api/research/jobs", params={"kind": "nothing"}).json(), [])
        for bad in ({"limit": 0}, {"limit": 501}, {"offset": -1}, {"kind": "x" * 40}, {"kind": "a b;"}):
            self.assertEqual(self.client.get("/api/research/jobs", params=bad).status_code, 422, bad)

    def test_api_sweep_cells_for_two_axes_take_the_best_over_the_rest(self):
        add_sweep_job(sweep_doc())
        r = self.client.get("/api/research/sweeps/sweep-t-1")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(list(j), ["job_id", "name", "name_label", "family_id", "objective", "objective_label", "axes", "rows",
                                   "best_run_id", "cells", "plain"])
        self.assertEqual((j["objective"], j["best_run_id"]), ("sharpe", "run2"))
        self.assertEqual([a["path"] for a in j["axes"]], [self.X, self.Y])
        self.assertEqual(j["axes"][1]["labels"], ["20", "30", "40"])
        self.assertEqual(len(j["rows"]), 6)
        c = j["cells"]
        self.assertEqual(list(c), ["x", "y", "xs", "ys", "x_values", "y_values", "cells", "run_ids", "best",
                                   "signed", "format", "hidden_axes"])
        self.assertEqual((c["x"], c["y"]), (self.X, self.Y))
        self.assertEqual((c["xs"], c["ys"], c["hidden_axes"]), (["5", "10"], ["20", "30", "40"], []))
        self.assertEqual(c["cells"], [[0.5, 0.1], [-0.2, 0.9], [1.0, None]])         # cells[y][x]
        self.assertEqual(c["best"], {"xi": 0, "yi": 2, "run_id": "run2", "value": 1.0})
        self.assertTrue(c["signed"])
        self.assertEqual(c["format"], "ratio")
        t = self.client.get("/api/research/sweeps/sweep-t-1", params={"x": self.Y, "y": self.X}).json()["cells"]
        self.assertEqual(t["cells"], [[0.5, -0.2, 1.0], [0.1, 0.9, None]])           # swapped axes: transposed
        self.assertEqual(t["best"]["xi"], 2)

    def test_three_axes_the_cell_is_the_best_over_the_hidden_axis(self):
        axes = {"a": [1, 2], "b": ["p", "q"], "c": [True, False]}
        rows, n = [], 0
        for a in axes["a"]:
            for b in axes["b"]:
                for c in axes["c"]:
                    rows.append(_row(n, {"a": a, "b": b, "c": c}, float(n if n != 3 else -5)))
                    n += 1
        add_sweep_job(sweep_doc(axes=axes, rows=rows))
        j = self.client.get("/api/research/sweeps/sweep-t-1").json()
        c = j["cells"]
        self.assertEqual((c["x"], c["y"], c["hidden_axes"]), ("a", "b", ["c"]))
        self.assertEqual(c["cells"], [[1.0, 5.0], [2.0, 7.0]])
        self.assertEqual(c["run_ids"], [["r1", "r5"], ["r2", "r7"]])
        self.assertIn("best", j["plain"])
        self.assertIn("c", j["plain"])
        self.assertIn("Each square is the best steadiness of returns (Sharpe) over every setting of C", j["plain"])
        self.assertNotIn("sharpe ", j["plain"])                 # the objective's code never reaches the sentence
        picked = self.client.get("/api/research/sweeps/sweep-t-1", params={"x": "c", "y": "a"}).json()["cells"]
        self.assertEqual(picked["xs"], ["true", "false"])
        self.assertEqual(picked["hidden_axes"], ["b"])

    def test_one_axis_sweep_has_one_row_of_cells(self):
        axes = {self.X: [5, 10, 15]}
        rows = [_row(i, {self.X: v}, o) for i, (v, o) in enumerate(zip(axes[self.X], (1.0, 3.0, 2.0)))]
        add_sweep_job(sweep_doc(axes=axes, rows=rows))
        c = self.client.get("/api/research/sweeps/sweep-t-1").json()["cells"]
        self.assertEqual((c["x"], c["y"]), (self.X, None))
        self.assertEqual(c["xs"], ["5", "10", "15"])
        self.assertEqual(len(c["ys"]), 1)
        self.assertEqual(c["cells"], [[1.0, 3.0, 2.0]])
        self.assertEqual((c["best"]["xi"], c["best"]["yi"]), (1, 0))
        r = self.client.get("/api/research/sweeps/sweep-t-1", params={"y": self.X})      # the same axis twice
        self.assertEqual(r.status_code, 422)

    def test_unknown_axis_422_names_it(self):
        add_sweep_job(sweep_doc())
        for q in ({"x": "nope"}, {"y": "nope"}, {"x": self.X, "y": "nope"}):
            r = self.client.get("/api/research/sweeps/sweep-t-1", params=q)
            self.assertEqual(r.status_code, 422, q)
            self.assertIn("nope", r.text)
            self.assertIn(self.X, r.text)                                   # and lists the axes it does have
        r = self.client.get("/api/research/sweeps/sweep-t-1", params={"x": self.X, "y": self.X})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.client.get("/api/research/sweeps/sweep-t-1", params={"x": "q" * 300}).status_code, 422)

    def test_sweep_404_for_other_kinds_and_unknown(self):
        add_sweep_job(None, "wf-1", kind="walkforward", status="running")
        add_sweep_job(None, "sweep-run", status="running")
        add_sweep_job(sweep_doc("other"), "sweep-bad")                       # the stored doc names another job
        add_sweep_job(sweep_doc("sweep-j"), "sweep-j")
        store.update_research_job("sweep-j", state_json="{not json")
        for jid in ("wf-1", "nope", "sweep-run", "sweep-bad", "sweep-j", "..", "%2e%2e", "a%5cb", "x" * 300):
            r = self.client.get(f"/api/research/sweeps/{jid}")
            self.assertEqual(r.status_code, 404, jid)
            self.assertNotIn("Traceback", r.text)

    def test_sweep_404_says_which_case_it_is(self):
        add_sweep_job(None, "wf-1", kind="walkforward", status="running")
        add_sweep_job(None, "sweep-run", status="running")
        add_sweep_job(None, "sweep-fail", status="failed")
        add_sweep_job(None, "sweep-lock", status="failed")
        store.update_research_job("sweep-lock", error="HoldoutLocked: the holdout starts 2026-08-01 C:" + chr(92) + "x")
        add_sweep_job(None, "sweep-boom", status="failed")
        store.update_research_job("sweep-boom", error="RuntimeError: the pool fell over")
        add_sweep_job(sweep_doc("sweep-j"), "sweep-j")
        store.update_research_job("sweep-j", state_json="{not json")
        for jid, text in (("nope", "no such sweep"), ("wf-1", "no such sweep"),
                          ("sweep-run", "sweep not finished yet (status: running)"),
                          ("sweep-fail", "sweep not finished yet (status: failed)"),
                          ("sweep-lock", "sweep not finished yet (status: failed): the holdout is locked"),
                          ("sweep-boom", "sweep not finished yet (status: failed): the run failed; see the log"),
                          ("sweep-j", "the sweep has no readable stored result")):
            r = self.client.get(f"/api/research/sweeps/{jid}")
            self.assertEqual((r.status_code, r.json()["detail"]), (404, text), jid)

    def test_a_failed_trials_error_is_a_plain_reason_never_exception_text(self):
        from stockify.research import plain
        axes = {self.X: [5, 10, 15, 20, 25]}
        rows = [_row(i, {self.X: v}, None) for i, v in enumerate(axes[self.X])]
        errs = ("the run did not report its folder", "ValueError: bad window", "KeyError: fast_period in x/y.py",
                "HoldoutLocked: the holdout starts 2026-08-01", None)
        for r, err in zip(rows, errs):
            r.update(status="failed", error=err)
        rows[4]["failure_reason"] = "the settings were not accepted"        # stored at write time: read as it is
        add_sweep_job(sweep_doc(axes=axes, rows=rows))
        j = self.client.get("/api/research/sweeps/sweep-t-1").json()
        self.assertEqual([r["error"] for r in j["rows"]],
                         ["the run did not report its folder", plain.GENERIC_FAILURE, plain.GENERIC_FAILURE,
                          "the holdout is locked", "the settings were not accepted"])
        self.assertNotIn("bad window", json.dumps(j))
        self.assertNotIn("fast_period", json.dumps(j))

    def test_axes_carry_a_plain_label_from_the_strategy_and_the_objective_a_name(self):
        axes = {"strategy.params.interval_s": [60, 300], self.X: [5, 10]}
        rows = [_row(i, {"strategy.params.interval_s": a, self.X: b}, float(i))
                for i, (a, b) in enumerate((a, b) for a in axes["strategy.params.interval_s"] for b in axes[self.X])]
        add_sweep_job(sweep_doc(axes=axes, rows=rows))
        store.update_research_job("sweep-t-1", spec_json=json.dumps(
            {"base": {"strategy": {"kind": "python", "ref": "stockify.strategies.buy_and_hold:BuyAndHold"}}}))
        j = self.client.get("/api/research/sweeps/sweep-t-1").json()
        self.assertEqual([(a["path"], a["label"]) for a in j["axes"]],
                         [("strategy.params.interval_s", "Bar length"), (self.X, "Fast")])   # no label known: its words, never the path
        self.assertEqual(j["objective_label"], "steadiness of returns (Sharpe)")
        self.assertIn("Bar length = ", j["plain"])                  # the sentence names the settings as the page does
        self.assertNotIn("interval_s", j["plain"])

    def test_the_sentence_and_axes_use_a_setting_s_choice_labels_and_percentages_not_raw_values(self):
        iv, inv = "strategy.params.interval_s", "strategy.params.invest"
        axes = {iv: [60, 300], inv: ["0.5", "0.95"]}
        rows = [_row(i, {iv: a, inv: b}, float(i)) for i, (a, b) in enumerate((a, b) for a in axes[iv] for b in axes[inv])]
        add_sweep_job(sweep_doc(axes=axes, rows=rows))
        store.update_research_job("sweep-t-1", spec_json=json.dumps(
            {"base": {"strategy": {"kind": "python", "ref": "stockify.strategies.buy_and_hold:BuyAndHold"}}}))
        j = self.client.get("/api/research/sweeps/sweep-t-1").json()
        self.assertIn("Bar length = 5 minutes and Share of cash to invest (%) = 95%", j["plain"])
        self.assertNotIn("= 300", j["plain"])
        self.assertEqual(j["cells"]["xs"], ["1 minute", "5 minutes"])
        self.assertEqual(j["cells"]["ys"], ["50%", "95%"])
        self.assertEqual([a["labels"] for a in j["axes"]], [["1 minute", "5 minutes"], ["50%", "95%"]])
        self.assertEqual([a["values"] for a in j["axes"]], [[60, 300], ["0.5", "0.95"]])    # the raw values stay as data

    def test_a_walk_forward_window_s_chosen_settings_use_choice_labels_and_percentages_too(self):
        from server.routers.research import _chosen_text
        from stockify.strategies import _catalogue
        schema = {p["name"]: p for p in _catalogue.schema_of("python", "stockify.strategies.buy_and_hold:BuyAndHold")}
        self.assertEqual(_chosen_text({"interval_s": 300, "invest": "0.95"}, schema),
                         "Bar length = 5 minutes, Share of cash to invest (%) = 95%")
        self.assertEqual(_chosen_text({"fast": 5, "ok": True}, {}), "Fast = 5, Ok = Yes")

    def test_a_sweep_without_a_stored_spec_labels_every_axis_in_words_not_by_its_path(self):
        add_sweep_job(sweep_doc())
        j = self.client.get("/api/research/sweeps/sweep-t-1").json()
        self.assertEqual([a["label"] for a in j["axes"]], [a["path"].rsplit(".", 1)[-1].capitalize() for a in j["axes"]])
        for a in j["axes"]:
            self.assertNotIn("strategy.params", a["label"])

    def test_net_pnl_plain_sentence_is_in_rupees(self):
        axes = {self.X: [5, 10]}
        add_sweep_job(sweep_doc(objective="net_pnl", axes=axes,
                                rows=[_row(0, {self.X: 5}, 412_000.0), _row(1, {self.X: 10}, -31_000.0)]))
        j = self.client.get("/api/research/sweeps/sweep-t-1").json()
        self.assertEqual(j["plain"], f"Each square is the profit after costs of one trial. The best of all 2 trials "
                                     f"has Fast = 5, with profit after costs ₹4,120.")
        self.assertNotIn("412000", j["plain"])

    def test_sweep_response_has_no_nan(self):
        doc = sweep_doc()
        doc["rows"][0]["sharpe"] = float("nan")
        doc["rows"][1]["objective_value"] = float("inf")
        doc["rows"][1]["max_drawdown"] = float("-inf")
        add_sweep_job(doc)
        for url in ("/api/research/sweeps/sweep-t-1", "/api/research/jobs"):
            text = self.client.get(url).text
            for token in ("NaN", "Infinity"):
                self.assertNotIn(token, text)
            json.loads(text, parse_constant=lambda c: self.fail(c))
        j = self.client.get("/api/research/sweeps/sweep-t-1").json()
        self.assertIsNone(j["rows"][0]["sharpe"])
        self.assertIsNone(j["rows"][1]["objective_value"])
        self.assertIsNone(j["cells"]["cells"][1][0])

    def test_signed_and_format_follow_the_objective(self):
        for obj, fmt in (("sharpe", "ratio"), ("sortino", "ratio"), ("calmar", "ratio"), ("cagr", "percent"),
                         ("net_pnl", "paise")):
            add_sweep_job(sweep_doc(f"sweep-{obj}", objective=obj), f"sweep-{obj}")
            c = self.client.get(f"/api/research/sweeps/sweep-{obj}").json()["cells"]
            self.assertEqual((c["format"], c["signed"]), (fmt, True), obj)

    def test_sweep_routes_refuse_a_non_loopback_host(self):
        add_sweep_job(sweep_doc())
        for url in ("/api/research/jobs", "/api/research/sweeps/sweep-t-1"):
            r = self.client.get(url, headers={"Host": "evil.example"})
            self.assertEqual(r.status_code, 400)
            self.assertNotIn("sweep-t-1", r.text)


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
        from stockify.research import wf_report
        q = mock.patch.object(wf_report, "min_oos_days", return_value=2)     # the fixtures have 12 days; 60 is below
        q.start()
        self.addCleanup(q.stop)
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
        self.assertEqual(list(j), ["job_id", "name", "name_label", "family_id", "objective", "train", "test", "step", "summary",
                                   "windows", "stitched_dates", "stitched_index", "plain"])
        self.assertEqual((j["job_id"], j["train"], j["test"], j["step"], j["objective"]),
                         (self.job, 120, 30, 30, "sharpe"))
        self.assertEqual(j["family_id"], f"walkforward/{self.job}")
        self.assertEqual(list(j["summary"]), ["windows", "is_windows", "oos_days", "oos_days_needed", "is_sharpe",
                                              "oos_sharpe", "ratio", "chosen_modal_share", "n_trials", "var_sharpe",
                                              "dsr"])
        self.assertEqual((j["summary"]["windows"], j["summary"]["oos_days"], j["summary"]["n_trials"]), (2, 12, 4))
        self.assertEqual(j["summary"]["oos_days_needed"], 2)
        self.assertEqual(list(j["summary"]["dsr"]), ["sr", "t", "skew", "kurt", "n_trials", "var_sharpe", "sr0",
                                                     "dsr", "psr"])
        self.assertEqual(len(j["windows"]), 2)
        self.assertEqual(list(j["windows"][0]), ["index", "train_from", "train_to", "test_from", "test_to",
                                                 "chosen_params", "chosen_text", "chosen_run_id", "test_run_id", "test_status", "result", "no_sharpe",
                                                 "error", "train", "test"])
        self.assertIsInstance(j["windows"][0]["test"]["net_pnl_paise"], int)
        self.assertEqual((len(j["stitched_dates"]), len(j["stitched_index"])), (12, 12))
        self.assertEqual(sorted(j["plain"]), sorted([
            "coverage", "curve", "dsr", "few_days", "flatter", "gaps", "holdout", "n_note", "objective", "pbo", "pbo_lead", "ratio", "ratio_caveat", "ratio_lead", "sensitivity", "setup", "stability", "stability_lead", "y_label"]))
        self.assertIn("of the in-sample Sharpe out of sample", j["plain"]["ratio"])
        self.assertIn("the windows cover different days", j["plain"]["pbo"])
        # unknown, unsafe and other-kind jobs are 404
        for bad in ("wf-nothing", "..%2F..%2Fx", "a%5Cb"):
            self.assertEqual(self.client.get(f"/api/research/walkforward/{bad}").status_code, 404, bad)
        self.assertEqual(self.client.get("/api/research/walkforward/").status_code, 404)
        store.create_research_job({"job_id": "sw-9", "kind": "sweep", "name": "s", "status": "done", "created_ns": 1})
        self.assertEqual(self.client.get("/api/research/walkforward/sw-9").status_code, 404)

    def test_the_report_notes_and_each_window_result_come_from_the_server(self):
        from stockify.research import wf_report
        self.finished()
        j = self.client.get(f"/api/research/walkforward/{self.job}").json()
        for key in ("objective", "setup", "flatter", "ratio_caveat", "coverage", "n_note", "curve", "ratio", "dsr"):
            self.assertTrue(j["plain"][key], key)
        self.assertIn("calendar days", j["plain"]["setup"])
        report = wf_report.render(self.job).read_text(encoding="utf-8")       # the same lines are in report.md
        for key in ("setup", "flatter", "ratio_caveat", "coverage", "n_note"):
            self.assertIn(j["plain"][key], report, key)
        self.assertEqual([w["result"] for w in j["windows"]], ["tested"] * len(j["windows"]))
        self.assertNotIn("--walkforward", j["plain"]["sensitivity"])           # the page names no command (D-145)
        self.assertIn("Run sensitivity check", j["plain"]["sensitivity"])
        self.assertIn("isn't stored", j["plain"]["sensitivity"])
        self.assertEqual(j["plain"]["y_label"], "growth of 1 (dashed line = starting capital)")
        self.assertIn("left empty", j["plain"]["gaps"])
        for key in ("ratio_lead", "stability_lead", "pbo_lead"):
            self.assertTrue(j["plain"][key], key)

    def test_a_window_whose_run_has_no_sharpe_carries_the_server_word_for_it(self):
        self.finished()
        f = paths.research_path(self.job) / "walkforward.json"
        doc = json.loads(f.read_text(encoding="utf-8"))
        doc["windows"][0]["test"]["sharpe"] = None
        doc["windows"][1]["train"]["sharpe"] = None
        f.write_text(json.dumps(doc), encoding="utf-8")
        ws = self.client.get(f"/api/research/walkforward/{self.job}").json()["windows"]
        self.assertEqual([w["no_sharpe"] for w in ws], ["too few days to work one out"] * 2)
        self.assertIn("too few days", ws[0]["result"])
        self.assertIn("too few days", ws[1]["result"])

    def test_a_window_with_both_sharpes_has_no_word_for_a_missing_one(self):
        self.finished()
        ws = self.client.get(f"/api/research/walkforward/{self.job}").json()["windows"]
        self.assertEqual([w["no_sharpe"] for w in ws], ["", ""])

    def test_a_job_that_is_not_finished_is_409_and_says_nothing_of_its_error(self):
        self.build_job(self.two_windows())                       # still running
        r = self.client.get(f"/api/research/walkforward/{self.job}")
        self.assertEqual(r.status_code, 409)
        self.assertIn("not finished", r.json()["detail"])
        store.update_research_job(self.job, status="failed", error="boom at C:\\secret\\path")
        r = self.client.get(f"/api/research/walkforward/{self.job}")
        self.assertEqual(r.status_code, 409)
        self.assertNotIn("secret", r.text)

    def test_a_job_that_is_not_finished_says_its_status_and_a_plain_reason_only(self):
        self.build_job(self.two_windows())
        j = self.client.get(f"/api/research/walkforward/{self.job}").json()
        self.assertEqual((j["status"], j["reason"]), ("running", None))
        state = json.loads(store.get_research_job(self.job)["state_json"])
        state["failure_reason"] = "the holdout is locked"
        store.update_research_job(self.job, status="failed", error="HoldoutLocked: x", state_json=json.dumps(state))
        j = self.client.get(f"/api/research/walkforward/{self.job}").json()
        self.assertEqual((j["status"], j["reason"]), ("failed", "the holdout is locked"))
        state.pop("failure_reason")
        store.update_research_job(self.job, status="failed", error="boom at C:\\secret\\path", state_json=json.dumps(state))
        j = self.client.get(f"/api/research/walkforward/{self.job}").json()
        self.assertEqual((j["status"], j["reason"]), ("failed", None))

    def test_an_exception_message_never_reaches_the_409(self):
        self.build_job(self.two_windows())
        store.update_research_job(self.job, status="failed", error="ValueError: bad window")
        r = self.client.get(f"/api/research/walkforward/{self.job}")
        self.assertEqual((r.json()["status"], r.json()["reason"]), ("failed", None))   # no stored plain reason
        self.assertNotIn("bad window", r.text)
        self.assertNotIn("ValueError", r.text)
        state = json.loads(store.get_research_job(self.job)["state_json"])
        state["failure_reason"] = self.wf.plain_failure(ValueError("bad window"))
        store.update_research_job(self.job, state_json=json.dumps(state))
        r = self.client.get(f"/api/research/walkforward/{self.job}")
        self.assertEqual(r.json()["reason"], "an internal error; see the log")
        self.assertNotIn("bad window", r.text)

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

    def test_walkforward_detail_has_no_sharpe_below_the_minimum_days(self):
        from stockify.research import wf_report
        self.finished()
        with mock.patch.object(wf_report, "min_oos_days", return_value=60):
            j = self.client.get(f"/api/research/walkforward/{self.job}").json()
        s = j["summary"]
        self.assertEqual((s["oos_days"], s["oos_days_needed"]), (12, 60))
        for key in ("is_sharpe", "oos_sharpe", "ratio", "dsr"):
            self.assertIsNone(s[key], key)
        for w in j["windows"]:
            self.assertIsNone(w["train"]["sharpe"])
            self.assertIsNone(w["test"]["sharpe"])
            self.assertIsInstance(w["test"]["net_pnl_paise"], int)
        self.assertEqual(len(j["stitched_index"]), 12)                                # the curve stays
        self.assertIn("not enough out-of-sample days to judge (12 of 60 needed)", j["plain"]["ratio"])

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
