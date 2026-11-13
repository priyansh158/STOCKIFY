"""CH 6.12: the walk-forward chart, `stockify.reporting.chart_wf.walkforward_chart`, and its route
`GET /api/research/walkforward/{job_id}/chart`. Run folders come from the C10 helpers; the job row is written by hand
with the windows the way `walkforward._dump` stores them."""
from __future__ import annotations

import json
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.charts.test_chart_model import NS, T, bar_world, fill, make_run, order

import stockify.store as store
from stockify import paths, timeutil

D0, D1, D2, D3 = "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"
JOB = "wf-chart-1"


def mk(day: str, *, n: int = 3, price: int = 10010, iid: int = 1) -> str:
    """A test run of one day: n bars from 09:15, a BUY at 09:16:05 and a SELL at 09:17:05, as run `oos`."""
    rid = make_run(orders=[order(1, note=f"in {day}"), order(2, side="SELL")],
                   fills=[fill(1, 1, T(9, 16, 5, day=day), "BUY", 10, price),
                          fill(2, 2, T(9, 17, 5, day=day), "SELL", 10, price + 40)],
                   world=bar_world(n, first=T(9, 15, day=day)))
    store.create_run({"run_id": rid, "status": "done", "role": "oos"})
    return rid


def win(index: int, day: str, run_id: str | None, status: str = "done", train_runs=()) -> dict:
    return {"index": index, "train_from": "2026-02-01", "train_to": "2026-02-28", "test_from": day, "test_to": day,
            "train_run_ids": list(train_runs), "chosen_params": {"strategy.params.n": 5}, "chosen_run_id": None,
            "test_run_id": run_id, "test_status": status, "error": None}


def add_job(windows: list[dict], job_id: str = JOB, status: str = "done", kind: str = "walkforward",
            sharpe: float | None = 1.0, oos_days: int = 100, write_file: bool = True) -> None:
    """A job row with the stored windows and, for a done job, the `walkforward.json` `finish` writes: a window has a
    `test` block when it has a done test run (and no `no_test` flag), as `walkforward._compute` makes them."""
    from stockify.research import fileio, walkforward
    store.create_research_job({"job_id": job_id, "kind": kind, "name": "wf", "family_id": f"walkforward/{job_id}",
                               "status": status, "created_ns": 1000, "finished_ns": 1005,
                               "state_json": json.dumps({"windows": windows})})
    if status != "done" or not write_file:
        return
    docw = []
    for w in windows:
        row = {k: v for k, v in w.items() if k not in ("train_run_ids", "no_test")}
        row["train"] = None
        has = w["test_run_id"] and w["test_status"] == "done" and not w.get("no_test")
        row["test"] = ({"run_id": w["test_run_id"], "from": w["test_from"], "to": w["test_to"], "days": 1,
                        "net_pnl_paise": 400, "sharpe": sharpe} if has else None)
        docw.append(row)
    folder = paths.research_path(job_id)
    folder.mkdir(parents=True, exist_ok=True)
    summary = {"windows": len(windows), "is_windows": 1, "oos_days": oos_days, "is_sharpe": 1.0, "oos_sharpe": 1.0,
               "ratio": 1.0, "chosen_modal_share": 1.0, "n_trials": 2, "var_sharpe": 0.1, "dsr": None}
    doc = {"schema": walkforward.SCHEMA, "job_id": job_id, "name": "wf", "objective": "sharpe", "train_days": 28,
           "test_days": 1, "step_days": 1, "span": None, "summary": summary, "windows": docw, "dates": [], "index": []}
    fileio.atomic_write_text(folder / "walkforward.json", json.dumps(doc))


def chart(job_id: str = JOB, *a, **kw) -> dict:
    from stockify.reporting.chart_wf import walkforward_chart
    return walkforward_chart(job_id, *a, **kw)


ZERO_NEW = [{"id": 2, "tradingsymbol": "OTHER", "kind": "EQ", "session_group": "CM", "cas_dates": []}]


class WfChartModelTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        self.a, self.b = mk(D0), mk(D1, price=20000)
        add_job([win(0, D0, self.a), win(1, D1, self.b)])

    def test_stitched_bars_are_ordered_and_windows_do_not_overlap(self):
        out = chart()
        starts = [b["start_ns"] for b in out["bars"]]
        self.assertEqual(starts, [T(9, 15 + k, day=d) for d in (D0, D1) for k in range(3)])
        self.assertEqual(starts, sorted(set(starts)))
        w = out["windows"]
        self.assertEqual([x["index"] for x in w], [0, 1])
        self.assertLessEqual(w[0]["to_ns"], w[1]["from_ns"])
        self.assertTrue(all(x["from_ns"] < x["to_ns"] for x in w))
        self.assertEqual(out["data_state"], "ok")
        self.assertFalse(out["truncated"])

    def test_markers_come_only_from_oos_runs(self):
        trial = make_run(orders=[order(1, note="trial")], fills=[fill(1, 1, T(9, 16, 5, day="2026-02-10"), "BUY", 1, 77700)])
        store.create_run({"run_id": trial, "status": "done", "role": "trial"})
        add_job([win(0, D0, self.a, train_runs=[trial]), win(1, D1, self.b)], job_id="wf-with-trial")
        out = chart("wf-with-trial")
        self.assertEqual([m["price_paise"] for m in out["markers"]], [10010, 10050, 20000, 20040])
        self.assertNotIn("trial", {m["note"] for m in out["markers"]})
        self.assertEqual([m["window"] for m in out["markers"]], [0, 0, 1, 1])
        self.assertEqual([m["trade_pnl_paise"] for m in out["markers"]], [None, 400, None, 400])   # each run starts flat

    def test_failed_window_is_listed_not_hidden(self):
        gone = paths.run_path("never-written")
        self.assertFalse(gone.exists())
        add_job([win(0, D0, self.a), win(1, D1, None, "failed"), win(2, D2, "never-written"), win(3, D3, None, "skipped")],
                job_id="wf-bad")
        w = chart("wf-bad")["windows"]
        self.assertEqual([(x["index"], x["run_id"], x["status"]) for x in w],
                         [(0, self.a, "done"), (1, None, "failed"), (2, None, "missing"), (3, None, "skipped")])
        out = chart("wf-bad")
        self.assertEqual(len(out["bars"]), 3)
        self.assertIn("3 of 4 windows have no chart", out["notice"])

    def test_window_bounds_match_the_state_json(self):
        from datetime import date, timedelta
        w = chart()["windows"]
        for x, day in zip(w, (D0, D1)):
            y, m, d = map(int, day.split("-"))
            lo = timeutil.ist_ns(date(y, m, d), "00:00")
            hi = timeutil.ist_ns(date(y, m, d) + timedelta(days=1), "00:00")
            self.assertEqual((x["from_ns"], x["to_ns"]), (lo, hi), day)

    def test_unknown_instrument_and_bad_arguments(self):
        from stockify.reporting.chart_wf import WfChartError
        with self.assertRaises(WfChartError) as cm:
            chart(instrument_id=99)
        self.assertEqual(cm.exception.kind, "instrument")
        with self.assertRaises(ValueError):
            chart(from_ns="x")
        with self.assertRaises(ValueError):
            chart(from_ns=True)

    def test_a_given_range_keeps_the_stitched_slice(self):
        whole = chart()
        lo = T(9, 16, day=D1)
        part = chart(from_ns=lo)
        self.assertEqual([b["start_ns"] for b in part["bars"]],
                         [b["start_ns"] for b in whole["bars"] if b["start_ns"] >= lo])
        self.assertEqual([m["window"] for m in part["markers"]], [1, 1])

    def test_pages_join_across_a_window_seam(self):
        from stockify.reporting import chart as model
        old = model.MAX_BARS
        model.MAX_BARS = 4                       # 3 bars + the first of the next window, then the rest
        self.addCleanup(setattr, model, "MAX_BARS", old)
        got, frm, pages = [], None, 0
        while True:
            out = chart(from_ns=frm)
            got += [b["start_ns"] for b in out["bars"]]
            pages += 1
            if not out["truncated"]:
                break
            frm = out["next_from_ns"]
            self.assertLess(pages, 5)
        self.assertEqual(got, [T(9, 15 + k, day=d) for d in (D0, D1) for k in range(3)])
        self.assertEqual(pages, 2)

    def test_a_page_cut_at_the_seam_exactly(self):
        from stockify.reporting import chart as model
        old = model.MAX_BARS
        model.MAX_BARS = 3
        self.addCleanup(setattr, model, "MAX_BARS", old)
        first = chart()
        self.assertEqual((len(first["bars"]), first["truncated"], first["next_from_ns"]), (3, True, T(9, 15, day=D1)))
        second = chart(from_ns=first["next_from_ns"])
        self.assertEqual((len(second["bars"]), second["truncated"]), (3, False))

    def test_a_pruned_pack_is_a_window_with_no_bars(self):
        for p in paths.packs_dir().glob("*.evt"):
            p.unlink()
        out = chart()
        self.assertEqual((out["bars"], out["data_state"]), ([], "pruned"))
        self.assertEqual([x["run_id"] for x in out["windows"]], [self.a, self.b])
        self.assertEqual(len(out["markers"]), 4)                      # the trades are still listed

    def test_plots_of_a_name_join_across_windows(self):
        def rows(day, v):
            return [{"t_ns": T(9, 15, 5, day=day), "instrument_id": 1, "name": "sma", "kind": "price", "value": v}]
        a = make_run(orders=[order(1)], fills=[fill(1, 1, T(9, 16, 5, day=D0), "BUY", 1, 10000)], plots=rows(D0, 1),
                     world=bar_world(3, first=T(9, 15, day=D0)))
        b = make_run(orders=[order(1)], fills=[fill(1, 1, T(9, 16, 5, day=D1), "BUY", 1, 10000)], plots=rows(D1, 2),
                     world=bar_world(3, first=T(9, 15, day=D1)))
        for r in (a, b):
            store.create_run({"run_id": r, "status": "done", "role": "oos"})
        add_job([win(0, D0, a), win(1, D1, b)], job_id="wf-plots")
        plots = chart("wf-plots")["plots"]
        self.assertEqual([(p["name"], [v for _, v in p["points"]]) for p in plots], [("sma", [1, 2])])


    def test_a_window_whose_run_never_traded_the_instrument_is_no_trades(self):
        c = make_run(orders=[order(1, iid=2)], fills=[fill(1, 1, T(9, 16, 5, day=D2), "BUY", 1, 5000, iid=2)],
                     world=bar_world(3, first=T(9, 15, day=D2), iid=2), instruments=ZERO_NEW)
        store.create_run({"run_id": c, "status": "done", "role": "oos"})
        add_job([win(0, D0, self.a), win(1, D1, self.b), win(2, D2, c)], job_id="wf-nt")
        out = chart("wf-nt")
        self.assertEqual([(x["index"], x["status"], x["run_id"]) for x in out["windows"]],
                         [(0, "done", self.a), (1, "done", self.b), (2, "no_trades", c)])
        self.assertEqual(len(out["bars"]), 6)
        self.assertIn("1 window did not trade this instrument", out["notice"])
        self.assertEqual(out["instrument_id"], 1)

    def test_a_window_the_summary_excludes_is_listed_with_its_reason_and_not_drawn(self):
        add_job([win(0, D0, self.a), win(1, D1, None, "skipped"), {**win(2, D2, self.b), "no_test": True},
                 {**win(3, D3, None, "failed"), "error": "ValueError: boom at C:\secret"}], job_id="wf-ex")
        out = chart("wf-ex")
        w = {x["index"]: x for x in out["windows"]}
        self.assertEqual(w[0]["result"], "tested")
        self.assertIsNone(w[0]["excluded"])
        for i in (1, 2, 3):
            self.assertTrue(w[i]["result"].startswith("test run did not finish"), w[i]["result"])
            self.assertEqual(w[i]["excluded"], w[i]["result"], i)
            self.assertIsNone(w[i]["run_id"], i)
        self.assertEqual(len(out["bars"]), 3)
        self.assertEqual({m["window"] for m in out["markers"]}, {0})
        self.assertNotIn("secret", json.dumps(out["windows"]))
        self.assertNotIn("boom", json.dumps(out["windows"]))

    def test_the_excluded_reason_is_the_words_of_the_walk_forward_page(self):
        from stockify.research import wf_report
        add_job([win(0, D0, self.a), {**win(1, D1, self.b), "chosen_params": None, "test_run_id": None,
                                      "test_status": "skipped"}], job_id="wf-words")
        out = chart("wf-words")
        _, doc = wf_report.load("wf-words")
        _, page = wf_report._shown(doc)
        self.assertEqual([x["result"] for x in out["windows"]], [wf_report._result_word(w) for w in page])
        self.assertEqual(out["windows"][1]["excluded"], "no training run gave a usable result")

    def test_a_tested_window_with_no_sharpe_is_still_drawn_and_says_why(self):
        add_job([win(0, D0, self.a), win(1, D1, self.b)], job_id="wf-nosharpe", sharpe=None)
        out = chart("wf-nosharpe")
        self.assertEqual(len(out["bars"]), 6)
        self.assertEqual([x["no_sharpe"] for x in out["windows"]], ["too few days to work one out"] * 2)
        self.assertTrue(all(x["excluded"] is None for x in out["windows"]))
        self.assertTrue(out["windows"][0]["result"].startswith("tested, no Sharpe"))

    def test_too_few_out_of_sample_days_is_said_in_every_window_and_nothing_is_hidden(self):
        add_job([win(0, D0, self.a), win(1, D1, self.b)], job_id="wf-few", oos_days=1)
        out = chart("wf-few")
        self.assertEqual(len(out["bars"]), 6)
        self.assertEqual([x["no_sharpe"] for x in out["windows"]], ["too few days to work one out"] * 2)


@support.requires_fastapi
class WfChartApiTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)
        self.a, self.b = mk(D0), mk(D1, price=20000)
        add_job([win(0, D0, self.a), win(1, D1, self.b)])

    def get(self, job=JOB, **q):
        return self.client.get(f"/api/research/walkforward/{job}/chart", params=q)

    def test_the_chart_is_a_run_chart_plus_windows(self):
        r = self.get()
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual((d["data_state"], d["instrument_id"], len(d["bars"])), ("ok", 1, 6))
        self.assertEqual(d["bars"][3]["t"], T(9, 15, day=D1) // NS)
        self.assertEqual([m["id"] for m in d["markers"]], ["w0-f1", "w0-f2", "w1-f1", "w1-f2"])
        self.assertEqual(d["markers"][0]["price"], "100.10")
        w = d["windows"]
        self.assertEqual([(x["index"], x["run_id"], x["status"]) for x in w], [(0, self.a, "done"), (1, self.b, "done")])
        self.assertEqual(list(w[0]), ["index", "from_t", "to_t", "from_d", "to_d", "run_id", "status", "result",
                                      "no_sharpe", "excluded"])
        self.assertEqual((w[0]["result"], w[0]["no_sharpe"], w[0]["excluded"]), ("tested", "", None))
        self.assertEqual((w[0]["from_d"], w[0]["to_d"]), (D0, D0))
        self.assertEqual(w[0]["to_t"] - w[0]["from_t"], 86400)
        self.assertNotIn("window", d["markers"][0])

    def test_walkforward_chart_404(self):
        for bad in ("wf-nothing", "..%2F..%2Fx", "a%5Cb", "%2E%2E", "x%00y", "a" * 65):
            r = self.client.get(f"/api/research/walkforward/{bad}/chart")
            self.assertEqual(r.status_code, 404, bad)
            self.assertNotIn(bad, r.text)
        self.assertEqual(self.get("wf-nothing").json()["detail"], "no such walk-forward job")
        store.create_research_job({"job_id": "sw-9", "kind": "sweep", "name": "s", "status": "done", "created_ns": 1,
                                   "state_json": json.dumps({"windows": []})})
        self.assertEqual(self.get("sw-9").status_code, 404)
        r = self.get(instrument=99)
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "no such instrument in this walk-forward"))

    def test_only_a_done_job_has_a_chart(self):
        for status in ("running", "failed", "cancelled", "queued"):
            add_job([win(0, D0, self.a)], job_id=f"wf-{status}", status=status)
            store.update_research_job(f"wf-{status}", error="boom at C:\\secret\\path")
            r = self.get(f"wf-{status}")
            self.assertEqual(r.status_code, 409, status)
            self.assertNotIn("secret", r.text)
            self.assertIn("not finished", r.json()["detail"])
        store.create_research_job({"job_id": "wf-nostate", "kind": "walkforward", "name": "w", "status": "done",
                                   "created_ns": 1})
        r = self.get("wf-nostate")
        self.assertEqual(r.status_code, 409)
        self.assertIn("no readable windows", r.json()["detail"])
        store.update_research_job("wf-nostate", state_json="{not json")
        self.assertEqual(self.get("wf-nostate").status_code, 409)
        store.update_research_job("wf-nostate", state_json=json.dumps({"windows": [{"index": "x"}]}))
        self.assertEqual(self.get("wf-nostate").status_code, 409)

    def test_bad_queries_are_422(self):
        for q in ({"instrument": -1}, {"from_ns": "x"}, {"to_ns": 2**63}, {"from_ns": 5, "to_ns": 5}):
            self.assertEqual(self.get(**q).status_code, 422, q)

    def test_no_fixed_path_under_research_is_shadowed(self):
        for path in ("/api/research/families", "/api/research/jobs"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.get(f"/api/research/walkforward/{JOB}").status_code, 200)   # Task 22's route, unchanged
        listed = list(self.client.get("/openapi.json").json()["paths"])
        self.assertIn("/api/research/walkforward/{job_id}/chart", listed)
        self.assertIn("/api/research/walkforward/{job_id}", listed)
        self.assertEqual(self.client.get("/api/research/walkforward/chart").status_code, 404)

    def test_non_loopback_host_refused(self):
        r = self.client.get(f"/api/research/walkforward/{JOB}/chart", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
