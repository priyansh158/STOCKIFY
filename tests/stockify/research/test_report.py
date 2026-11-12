"""M6.4 / M6.8 (IL 6.3): the run and family research reports, the derived-file cache, the research command."""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import json
import random
import unittest
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import __main__ as cli
from stockify import paths
from stockify.data import refdata
from stockify.research import bootstrap, plain, registry, report, stats
from tests.stockify.research.fixtures import make_run

FAM = "strat/ds-1m-RELIANCE/sharpe"


def noisy(seed: int, n: int = 30, drift: float = 0.0005) -> list[float]:
    rng = random.Random(seed)
    return [drift + rng.uniform(-0.01, 0.01) for _ in range(n)]


def family_of(n: int, family: str = FAM, objective: str = "sharpe", **kw) -> list[str]:
    registry.ensure_family(family, objective)
    ids = []
    for i in range(n):
        rid = f"r{i:02d}"
        make_run(rid, family, daily_returns=noisy(i), name=f"t{i:02d}", **kw)
        ids.append(rid)
    return ids


def finite_json(obj) -> str:
    return json.dumps(dataclasses.asdict(obj), allow_nan=False)


class ReportTest(SandboxedDBTest):
    db_modules = (store, refdata)

    def sql(self, text: str, args: tuple = ()) -> None:
        with store._lock:
            conn = store._connect()
            conn.execute(text, args)
            conn.commit()


class RunResearchTests(ReportTest):
    def test_run_research_for_the_best_run(self):
        family_of(12)
        best = registry.best_trial(FAM)
        res = report.run_research(best.run_id, use_cache=False)
        self.assertEqual(res.n_trials, 12)
        self.assertEqual(res.rank, 1)
        self.assertTrue(res.is_best)
        srs = []
        for t in registry.trials(FAM):
            r = stats.returns_from_nav(t.capital_paise, list(t.navs))
            srs.append(stats.sharpe_per_period(stats.excess_returns(list(t.dates), r, stats.risk_free_rows())))
        defined = [s for s in srs if s is not None]
        mean = sum(defined) / len(defined)
        v = sum((s - mean) ** 2 for s in defined) / (len(defined) - 1)
        self.assertAlmostEqual(res.dsr.sr0, stats.expected_max_sharpe(12, v), places=12)
        self.assertEqual(res.picked_run_id, best.run_id)
        self.assertIn("best of 12 tries", res.plain["dsr"])
        finite_json(res)

    def test_rank_and_best_follow_the_objective(self):
        fam = "f/pnl/net_pnl"
        registry.ensure_family(fam, "net_pnl")
        make_run("steady", fam, daily_returns=[0.001, 0.0011] * 15, name="steady")
        make_run("wild", fam, daily_returns=[0.05, -0.03] * 15, name="wild")
        wild = report.run_research("wild", use_cache=False)
        steady = report.run_research("steady", use_cache=False)
        self.assertEqual(wild.objective, "net_pnl")
        self.assertTrue(wild.is_best)
        self.assertEqual(steady.rank, 2)
        self.assertEqual(steady.picked_run_id, "wild")
        self.assertIn(plain.OBJECTIVE_LABELS["net_pnl"], wild.plain["dsr"])
        self.assertIn(plain.OBJECTIVE_LABELS["net_pnl"], steady.plain["dsr"])
        self.assertIn("wild", steady.plain["dsr"])

    def test_a_runner_up_says_which_run_the_job_picked(self):
        ids = family_of(5)
        best = registry.best_trial(FAM)
        other = next(i for i in ids if i != best.run_id)
        res = report.run_research(other, use_cache=False)
        self.assertGreater(res.rank, 1)
        self.assertFalse(res.is_best)
        self.assertEqual(res.picked_run_id, best.run_id)
        self.assertIn(best.name, res.plain["dsr"])
        self.assertIn("runner-up", res.plain["dsr"])

    def test_one_trial_family_reports_the_only_try(self):
        family_of(1)
        res = report.run_research("r00", use_cache=False)
        self.assertEqual((res.n_trials, res.rank, res.is_best), (1, 1, True))
        self.assertIn("only try", res.plain["dsr"])
        self.assertEqual(res.dsr.sr0, 0.0)

    def test_variants_do_not_change_n(self):
        family_of(3)
        for i in range(5):
            make_run(f"s{i}", FAM, daily_returns=noisy(100 + i), role="sensitivity", name=f"sens{i}")
        res = report.run_research("s0", use_cache=False)
        self.assertEqual(res.n_trials, 3)
        self.assertIsNone(res.rank)
        self.assertFalse(res.is_best)
        self.assertIn("not a try", res.plain["dsr"])
        self.assertEqual(report.run_research("r00", use_cache=False).n_trials, 3)

    def test_flat_run_has_no_sharpe_and_a_sentence(self):
        family_of(2)
        make_run("flat", FAM, daily_returns=[0.0] * 30, name="flat")
        res = report.run_research("flat", use_cache=False)
        self.assertEqual(res.n_trials, 3)                       # a flat trial still counts in N
        self.assertIsNone(res.dsr)
        self.assertIn("no Sharpe", res.plain["dsr"])
        finite_json(res)

    def test_v_stands_on_its_own_when_the_best_trial_is_flat(self):
        fam = "f/flatbest/net_pnl"
        registry.ensure_family(fam, "net_pnl")
        make_run("flat", fam, daily_returns=[0.0] * 30, name="flat")          # pnl 0 beats the losers
        for i in range(3):
            make_run(f"L{i}", fam, daily_returns=noisy(i, drift=-0.004), name=f"loser{i}")
        rep = report.family_report(fam)
        self.assertEqual(rep.best_run_id, "flat")
        self.assertIsNone(rep.best)
        self.assertEqual(rep.n_trials, 4)
        srs = []
        for t in registry.trials(fam):
            if t.run_id != "flat":
                r = stats.returns_from_nav(t.capital_paise, list(t.navs))
                srs.append(stats.sharpe_per_period(stats.excess_returns(list(t.dates), r, stats.risk_free_rows())))
        mean = sum(srs) / 3
        v = sum((x - mean) ** 2 for x in srs) / 2
        self.assertGreater(v, 0)
        self.assertAlmostEqual(rep.var_sharpe, v, places=15)
        self.assertIn("no Sharpe", rep.plain["dsr"])
        self.assertEqual(report.list_families()[0].best_dsr, None)

    def test_a_run_without_capital_never_breaks_its_family(self):
        family_of(3)
        self.sql("UPDATE runs SET config_json = ? WHERE run_id = 'r00'", (json.dumps({"seed": 1}),))
        self.sql("UPDATE runs SET config_json = ? WHERE run_id = 'r01'", (json.dumps({"capital_paise": 0}),))
        res = report.run_research("r00", use_cache=False)
        self.assertIsNone(res.bootstrap)
        self.assertIsNone(res.dsr)
        self.assertIn("no usable equity", res.plain["dsr"])
        rep = report.family_report(FAM)
        self.assertEqual(rep.n_trials, 3)                       # still counted in N, Sharpe None
        self.assertEqual({t.run_id: t.sharpe_per_period is None for t in rep.trials},
                         {"r00": True, "r01": True, "r02": False})
        finite_json(rep)
        report.list_families()
        report.run_research("r02", use_cache=False)

    def test_the_risk_free_rows_are_read_once_per_call(self):
        family_of(4)
        real = stats.risk_free_rows
        for call in (lambda: report.run_research("r00", use_cache=False), lambda: report.family_report(FAM),
                     report.list_families):
            with mock.patch.object(stats, "risk_free_rows", side_effect=real) as m:
                call()
            self.assertEqual(m.call_count, 1)

    def test_report_is_finite_json(self):
        registry.ensure_family(FAM)
        make_run("flat", FAM, daily_returns=[0.0] * 30)
        make_run("one", FAM, daily_returns=[0.01])
        make_run("bad", FAM, navs=[], status="failed")
        for rid in ("flat", "one", "bad"):
            finite_json(report.run_research(rid, use_cache=False))
        finite_json(report.family_report(FAM))
        for s in report.list_families():
            finite_json(s)

    def test_unknown_run_is_a_value_error(self):
        with self.assertRaises(ValueError):
            report.run_research("nope")


class CacheTests(ReportTest):
    def setUp(self):
        super().setUp()
        registry.ensure_family(FAM)
        for rid, seed in (("a", 1), ("b", 2), ("c", 3)):
            make_run(rid, FAM, daily_returns=noisy(seed), with_folder=True, seed=7)

    def counted(self):
        return mock.patch.object(report.bootstrap, "bootstrap_ci", side_effect=bootstrap.bootstrap_ci)

    def cache_path(self, rid="a"):
        return paths.run_path(rid) / "research.json"

    def test_cache_file_is_written_and_reused(self):
        with self.counted() as m:
            first = report.run_research("a")
            self.assertTrue(self.cache_path().is_file())
            second = report.run_research("a")
        self.assertEqual(m.call_count, 1)
        self.assertEqual(first, second)
        data = json.loads(self.cache_path().read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"key", "bootstrap", "regimes"})   # the bootstrap and the regimes, keyed apart

    def test_family_changes_do_not_touch_the_cache(self):
        with self.counted() as m:
            self.assertEqual(report.run_research("a").n_trials, 3)
            make_run("d", FAM, daily_returns=noisy(4), with_folder=True)
            self.assertEqual(report.run_research("a").n_trials, 4)
        self.assertEqual(m.call_count, 1)

    def test_cache_is_invalidated_when_returns_or_seed_change(self):
        with self.counted() as m:
            report.run_research("a")
            report.run_research("a")
            self.assertEqual(m.call_count, 1)
            cfg = json.loads(store.get_run("a")["config_json"])
            cfg["seed"] = 8
            self.sql("UPDATE runs SET config_json = ? WHERE run_id = 'a'", (json.dumps(cfg),))
            report.run_research("a")
            self.assertEqual(m.call_count, 2)
            self.sql("UPDATE run_equity SET nav_paise = nav_paise + 1000 WHERE run_id = 'a' AND trade_date = "
                     "(SELECT MAX(trade_date) FROM run_equity WHERE run_id = 'a')")
            report.run_research("a")
            self.assertEqual(m.call_count, 3)

    def test_cache_is_invalidated_when_the_risk_free_rows_change(self):
        other = copy.deepcopy(stats.risk_free_rows())
        other[0]["rate_ppb"] += 1_000_000
        with self.counted() as m:
            report.run_research("a")
            with mock.patch.object(stats, "risk_free_rows", return_value=other):
                report.run_research("a")
            self.assertEqual(m.call_count, 2)

    def test_a_corrupt_cache_is_a_miss(self):
        for junk in ("{not json", "[]", '{"key": 1}', '{"key": "x", "bootstrap": {"n_days": NaN}}'):
            with self.subTest(junk=junk):
                self.cache_path().unlink(missing_ok=True)
                with self.counted() as m:
                    report.run_research("a")
                    self.cache_path().write_text(junk, encoding="utf-8")
                    res = report.run_research("a")
                self.assertEqual(m.call_count, 2)
                self.assertIsNotNone(res.bootstrap)
                json.loads(self.cache_path().read_text(encoding="utf-8"))   # healed
        self.cache_path().write_bytes(b"\x00\xff\xfe")
        self.assertIsNotNone(report.run_research("a").bootstrap)

    def test_a_crafted_cache_with_the_right_key_but_bad_types_is_a_miss(self):
        with self.counted() as m:
            report.run_research("a")
            data = json.loads(self.cache_path().read_text(encoding="utf-8"))
            data["bootstrap"]["sharpe"] = "<script>"
            self.cache_path().write_text(json.dumps(data), encoding="utf-8")
            res = report.run_research("a")
        self.assertEqual(m.call_count, 2)
        self.assertIsInstance(res.bootstrap.sharpe.point, float)

    def test_missing_folder_computes_without_caching(self):
        make_run("nofolder", FAM, daily_returns=noisy(9))
        res = report.run_research("nofolder")
        self.assertIsNotNone(res.bootstrap)
        self.assertFalse((paths.runs_dir() / "nofolder").exists())

    def test_use_cache_false_neither_reads_nor_writes(self):
        with self.counted() as m:
            report.run_research("a", use_cache=False)
            report.run_research("a", use_cache=False)
        self.assertEqual(m.call_count, 2)
        self.assertFalse(self.cache_path().exists())


class FamilyReportTests(ReportTest):
    def test_family_report_lists_sessions_and_unlocks_without_counting_them(self):
        family_of(3)
        make_run("sens", FAM, daily_returns=noisy(50), role="sensitivity", name="sens")
        make_run("failed", FAM, navs=[], status="failed", name="failed")
        store.create_session("sess1", "paper", {"family_id": FAM}, 1)
        self.sql("UPDATE sessions SET family_id = ? WHERE session_id = 'sess1'", (FAM,))
        store.log_holdout_unlock("r00", FAM, "look", job_id="job1")
        store.log_holdout_unlock("r01", FAM, "look again", job_id="job1")
        store.name_family(FAM, "my idea")
        rep = report.family_report(FAM)
        self.assertEqual((rep.n_trials, len(rep.trials)), (3, 3))
        self.assertEqual({o.run_id for o in rep.others}, {"sens", "failed"})
        self.assertEqual([s["session_id"] for s in rep.sessions], ["sess1"])
        self.assertEqual(len(rep.holdout_unlocks), 2)
        self.assertEqual((rep.description, rep.named), ("my idea", True))
        self.assertEqual(rep.best_run_id, registry.best_trial(FAM).run_id)
        self.assertEqual(rep.best.n_trials, 3)
        self.assertIsInstance(rep.trials[0].net_pnl_paise, int)
        self.assertEqual(rep.trials[0].days, 30)
        summary = {s.family_id: s for s in report.list_families()}[FAM]
        self.assertEqual((summary.n_trials, summary.n_other_runs, summary.n_sessions, summary.unlocks),
                         (3, 2, 1, 2))
        self.assertEqual(summary.best_run_id, rep.best_run_id)
        self.assertEqual(summary.best_dsr, rep.best.dsr)
        finite_json(rep)

    def test_unknown_family_is_a_value_error(self):
        with self.assertRaises(ValueError):
            report.family_report("nope/x/sharpe")


class CliTests(ReportTest):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_research_show_prints_the_plain_sentence(self):
        family_of(4)
        best = registry.best_trial(FAM)
        code, out, _ = self.run_cli("research", "show", best.run_id)
        self.assertEqual(code, 0)
        self.assertIn("best of 4 tries", out)
        self.assertIn("after allowing for luck", out)
        code, out, _ = self.run_cli("research", "show", FAM)
        self.assertEqual(code, 0)
        self.assertIn("4 tries", out)
        self.assertEqual(self.run_cli("research", "show", "nothing")[0], 2)

    def test_research_show_survives_a_cp1252_console(self):
        family_of(2)
        raw = io.BytesIO()
        fake = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with contextlib.redirect_stdout(fake):
            code = cli.main(["research", "show", FAM])
        fake.flush()
        self.assertEqual(code, 0)
        self.assertTrue(raw.getvalue())

    def test_research_families_name_sets_the_flag(self):
        family_of(2)
        code, out, _ = self.run_cli("research", "families", "--name", FAM, "my idea")
        self.assertEqual(code, 0)
        fam = store.get_family(FAM)
        self.assertEqual((fam["description"], fam["named"]), ("my idea", 1))
        self.assertIn("my idea", out)
        code, _, err = self.run_cli("research", "families", "--name", "no/such/sharpe", "x")
        self.assertEqual(code, 1)
        self.assertIn("no such family", err)

    def test_backfill_and_dry_run(self):
        make_run("orphan", None, daily_returns=noisy(1))
        self.assertEqual(self.run_cli("research", "families", "--dry-run")[0], 2)   # needs --backfill
        code, out, _ = self.run_cli("research", "families", "--backfill", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would give 1 run", out)
        self.assertIsNone(store.get_run("orphan")["family_id"])


if __name__ == "__main__":
    unittest.main()
