"""FS 6: the test record: bars for the ranked stocks and the benchmark, the base config, the walk-forward over
balance x basket size, ending the day before the holdout, and the job id kept for the page. Kite and the pool are
fakes here; the real run happens from the command line."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import runconfig
from stockify import store as lab
from stockify.data import history as bars_history
from stockify.data import refdata
from stockify.fundamentals import store, test_record
from stockify.research import holdout, walkforward

RANKING = {"0.3": [{"date": "2022-01-03", "symbols": ["AAA", "BBB"]}],
           "0.5": [{"date": "2022-01-03", "symbols": ["BBB", "GONE", "AAA"]}],
           "0.7": [{"date": "2022-01-03", "symbols": ["CCC"]}]}


@dataclass
class _Done:
    job_id: str
    family_id: str


class TestRecordTests(SandboxedDBTest):
    db_modules = (store, refdata, lab)

    def setUp(self):
        super().setUp()
        from unittest import mock

        from stockify import research
        patch = mock.patch.object(research, "settings", return_value={**research.settings(),
                                                                      "holdout_from": "2026-08-01"})
        patch.start()
        self.addCleanup(patch.stop)
        conn = refdata._connect()
        for iid, (sym, kind) in enumerate((("AAA", "EQ"), ("BBB", "EQ"), ("CCC", "EQ"), ("NIFTY 500", "INDEX")), 1):
            conn.execute("INSERT INTO instruments (instrument_id, exchange, kind, tradingsymbol, first_seen, last_seen, "
                         "source) VALUES (?, 'NSE', ?, ?, '2021-01-01', '2026-07-31', 'kite_dump')", (iid, kind, sym))
        self.fetched: list[tuple] = []
        self.walked: list[tuple] = []

    def top_up(self, iid, day, *, years, client, **kw):  # noqa: ANN001, ANN201
        self.fetched.append((iid, day, years, kw.get("today")))
        return bars_history.TopUp(requests=1)

    def walk(self, spec, **kw):  # noqa: ANN001, ANN201
        """As run_windows: the job row first (its name, family and span), then the runs (none here)."""
        self.walked.append((spec, kw))
        job = f"wf-fundamentals-basket-{len(self.walked)}"
        lab.create_research_job({"job_id": job, "kind": "walkforward", "name": spec.name,
                                 "family_id": f"walkforward/{job}", "status": "running", "created_ns": len(self.walked),
                                 "spec_json": json.dumps({"span": [kw["start"].isoformat(), kw["end"].isoformat()]})})
        return _Done(job, f"walkforward/{job}")

    def _run(self, **kw):  # noqa: ANN003, ANN202
        return test_record.run(rank=lambda start, end: RANKING, top_up=self.top_up, walk=self.walk, client=object(),
                               **kw)

    def test_test_record_ends_before_the_holdout(self):
        self.assertEqual(holdout.holdout_from(), date(2026, 8, 1))
        out = self._run()
        end = date(2026, 7, 31)
        self.assertEqual(out["end"], end.isoformat())
        self.assertEqual(out["start"], "2022-01-01")
        (spec, kw), = self.walked
        self.assertEqual((kw["start"], kw["end"]), (date(2022, 1, 1), end))
        self.assertEqual((kw["train"], kw["test"], kw["step"]), (365, 90, 90))
        self.assertEqual(spec.axes, {"strategy.params.balance": ["0.3", "0.5", "0.7"],
                                     "strategy.params.basket_size": [10, 20, 30]})
        pack = spec.base["data"]["packs"][0]["spec"]
        self.assertEqual((pack["dataset"], pack["interval"], pack["from"], pack["to"]),
                         ("bars", "day", "2022-01-01", "2026-07-31"))
        self.assertEqual(pack["instruments"], [{"symbols": ["NSE:AAA", "NSE:BBB", "NSE:CCC"]}])
        self.assertEqual(spec.base["strategy"]["ref"], "stockify.strategies.fundamentals_basket:FundamentalsBasket")
        self.assertEqual(spec.base["strategy"]["params"]["ranking"], RANKING)
        self.assertEqual(spec.base["fill_model"]["kind"], "bar")
        self.assertEqual(spec.base["costs"]["broker"], "zerodha")
        self.assertEqual(spec.base["price_basis"], "adjusted")
        runconfig.validate(spec.base)
        # every window of the real plan ends before the holdout
        wins, grid, _, _ = walkforward.plan(spec, train=365, test=90, step=90, objective="sharpe",
                                            start=date(2022, 1, 1), end=end)
        self.assertEqual(grid, 9)
        self.assertLessEqual(max(w.test_to for w in wins), end)
        # bars for the ranked stocks and the benchmark, never past the end: the nightly candles' top-up (CW 1b),
        # back at least to START (candles.json history_years, or more for a start further back)
        self.assertEqual(sorted(f[0] for f in self.fetched), [1, 2, 3, 4])
        self.assertTrue(all(f[1] == end for f in self.fetched))
        self.assertTrue(all(bars_history.years_before(end, f[2]) <= date(2022, 1, 1) for f in self.fetched))
        self.assertEqual({f[2] for f in self.fetched}, {5})          # the fewest years that reach START
        from stockify import timeutil
        self.assertEqual({f[3] for f in self.fetched}, {timeutil.ist_today()})   # today, apart from the end
        self.assertEqual(out["missing"], ["GONE"])
        self.assertEqual(out["benchmark"], {"symbol": "NSE:NIFTY 500", "label": "price index, without dividends",
                                            "source": "kite"})
        kept = store.latest_test_record()
        self.assertEqual((kept["job_id"], kept["family_id"], kept["start"], kept["end"], kept["status"]),
                         ("wf-fundamentals-basket-1", "walkforward/wf-fundamentals-basket-1", "2022-01-01",
                          "2026-07-31", "running"))
        self.assertEqual((kept["benchmark"], kept["benchmark_symbol"]),
                         ("price index, without dividends", "NSE:NIFTY 500"))
        # another walk-forward job is not the test record; a second run of it is the newest
        lab.create_research_job({"job_id": "wf-other", "kind": "walkforward", "name": "sma", "status": "done",
                                 "created_ns": 99})
        self.assertEqual(store.latest_test_record()["job_id"], "wf-fundamentals-basket-1")
        self._run()
        self.assertEqual(store.latest_test_record()["job_id"], "wf-fundamentals-basket-2")

    def test_an_end_in_the_holdout_is_refused(self):
        for bad in (date(2026, 8, 1), date(2026, 9, 1)):
            with self.subTest(end=bad), self.assertRaisesRegex(ValueError, "holdout"):
                self._run(end=bad)
        self.assertEqual(self.walked, [])
        self.assertIsNone(store.latest_test_record())

    def test_no_ranked_stock_with_data_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no ranked stock"):
            test_record.run(rank=lambda start, end: {"0.5": [{"date": "2022-01-03", "symbols": ["GONE"]}]},
                            top_up=self.top_up, walk=self.walk, client=object())
        self.assertEqual(self.walked, [])

    def test_the_top_up_reaches_back_to_the_start_for_a_far_end(self):
        self.assertEqual(test_record.years_needed(date(2026, 7, 31)), 5)          # 2021-07-31 <= 2022-01-01
        self.assertEqual(test_record.years_needed(date(2029, 7, 31)), 8)
        self.assertEqual(test_record.years_needed(date(2023, 1, 1)), 1)           # 2022-01-01 reaches START
        self.assertEqual(test_record.years_needed(date(2023, 1, 2)), 2)           # 2022-01-02 does not
        self.assertLessEqual(bars_history.years_before(date(2029, 7, 31), 8), test_record.START)

    def test_a_stock_kite_cannot_give_is_missing(self):
        def top_up(iid, day, *, years, client, **kw):  # noqa: ANN001, ANN202
            if iid == 2:
                return bars_history.TopUp(outcome="no_token")
            if iid == 3:
                raise RuntimeError("Kite said no")
            return bars_history.TopUp()
        out = test_record.run(rank=lambda start, end: RANKING, top_up=top_up, walk=self.walk, client=object())
        self.assertEqual(out["symbols"], ["AAA"])
        self.assertEqual(out["missing"], ["BBB", "CCC", "GONE"])
