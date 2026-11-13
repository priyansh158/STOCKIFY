"""M6.7 (FW 6.3): the sensitivity suite: one-axis variants, applicability, the fragile flag, the result file, the CLI."""
from __future__ import annotations

import contextlib
import csv
import io
import itertools
import json
import math
import unittest
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import __main__ as cli
from stockify import bundle, numerics, paths, research, runconfig
from stockify.config import loader
from stockify.research import fileio, holdout, pool, sensitivity
from stockify.research.pool import PoolOutcome
from tests.stockify.research.fixtures import make_run
from tests.stockify.test_bundle import BundleTest, run as run_config
from tests.stockify.test_costs_scale import golden_run
from tests.stockify.test_runs_launch import BUY

REPO = Path(__file__).resolve().parents[3]
ORB = json.loads((REPO / "examples" / "runs" / "orb_reliance_minute.json").read_text(encoding="utf-8"))
CAP = 100_000_000


def orb(**over) -> dict:
    """The demo ORB config, normalised (what a run's stored config looks like)."""
    return runconfig.validate({**ORB, **over})


def by_key(vs) -> dict:  # noqa: ANN001
    return {v.key: v for v in vs}


def changed_paths(a, b, prefix=""):  # noqa: ANN001, ANN201
    """The leaf paths where two configs differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += changed_paths(a.get(k), b.get(k), f"{prefix}/{k}")
        return out
    return [] if a == b else [prefix]


class VariantTests(unittest.TestCase):
    def test_variants_are_one_axis_at_a_time_and_skip_the_baseline_duplicates(self):
        cfg = orb()
        vs = sensitivity.variants(cfg)
        self.assertEqual(vs[0].axis, "baseline")
        keys = [v.key for v in vs]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual({"costs_x0", "costs_x1.5", "costs_x2", "slip_plus1", "slip_plus2", "latency_250",
                          "latency_1000", "fill_snapshot_ltp", "fill_snapshot_quote", "fill_eod_close"},
                         set(keys) - {vs[0].key})
        self.assertNotIn("costs_x1", keys)                    # x1, +0, latency 0 and the baseline kind are the baseline
        self.assertFalse({"slip_plus0", "latency_0", "fill_bar"} & set(keys))
        base = vs[0].config
        axis_paths = {"costs": "/costs/scale", "slippage": "/fill_model/slippage/extra_ticks",
                      "latency": "/latency_ms", "fill_model": "/fill_model/kind"}
        for v in vs[1:]:
            diff = [p for p in changed_paths(base, v.config) if p not in ("/name", "/unlock_holdout")]
            self.assertEqual(diff, [axis_paths[v.axis]], v.key)
        self.assertEqual(by_key(vs)["costs_x1.5"].config["costs"]["scale"], "1.5")
        self.assertEqual(by_key(vs)["latency_250"].config["latency_ms"], 250)

    def test_extra_ticks_variants_apply_to_a_bps_base(self):
        cfg = orb()
        self.assertEqual(cfg["fill_model"]["slippage"]["kind"], "bps")
        v = by_key(sensitivity.variants(cfg))
        self.assertEqual(v["slip_plus1"].config["fill_model"]["slippage"]["extra_ticks"], 1)
        self.assertEqual(v["slip_plus2"].config["fill_model"]["slippage"]["extra_ticks"], 2)
        self.assertEqual(v["slip_plus1"].config["fill_model"]["slippage"]["x"], "0.0005")     # the bps stay

    def test_extra_ticks_add_to_a_base_that_already_has_some(self):
        cfg = orb(fill_model={**ORB["fill_model"], "slippage": {"kind": "fixed_ticks", "k": 1, "extra_ticks": 3}})
        v = by_key(sensitivity.variants(cfg))
        self.assertEqual(v["slip_plus1"].config["fill_model"]["slippage"]["extra_ticks"], 4)
        self.assertEqual(v["slip_plus2"].config["fill_model"]["slippage"]["extra_ticks"], 5)
        self.assertNotIn("slip_plus0", v)

    def test_the_source_unlock_is_dropped_from_every_variant(self):
        cfg = orb(unlock_holdout="looked at it on purpose")
        for v in sensitivity.variants(cfg):
            self.assertIsNone(v.config.get("unlock_holdout"), v.key)

    def test_variant_names_are_safe_segments_at_most_64(self):
        for name in ("a", "x" * 64, "weird.name-" + "y" * 50, "n." * 29 + "n"):
            cfg = orb(name=name[:64])
            for v in sensitivity.variants(cfg):
                n = v.config["name"]
                with self.subTest(name=name[:12], key=v.key):
                    self.assertLessEqual(len(n), 64)
                    self.assertTrue(paths.safe_segment(n, 64), n)
                    if v.axis != "baseline":
                        self.assertTrue(n.endswith("-" + v.key), n)
        short = by_key(sensitivity.variants(orb(name="abc")))["costs_x2"].config["name"]
        self.assertEqual(short, "abc-costs_x2")

    def test_variants_are_deterministic(self):
        self.assertEqual(sensitivity.variants(orb()), sensitivity.variants(orb()))


class ApplicabilityTests(unittest.TestCase):
    def test_fill_model_applicability_comes_from_the_validator(self):
        cfg = orb()                                           # price_basis adjusted: eod_close needs raw
        with mock.patch.object(bundle, "resolve_bundle", return_value={}):
            res = sensitivity.applicable_fill_models(cfg)
        self.assertEqual(set(res), set(runconfig.FILL_KINDS))
        with self.assertRaises(runconfig.RunConfigError) as cm:
            runconfig.validate({**cfg, "fill_model": {**cfg["fill_model"], "kind": "eod_close"}})
        self.assertIn("price_basis", str(cm.exception))
        self.assertEqual(res["eod_close"], str(cm.exception))
        for kind in ("bar", "snapshot_ltp", "snapshot_quote"):
            self.assertIsNone(res[kind], kind)

    def test_a_bundle_the_resolver_refuses_is_not_applicable_with_its_text(self):
        with mock.patch.object(bundle, "resolve_bundle", side_effect=bundle.BundleError("no quotes in these packs")):
            res = sensitivity.applicable_fill_models(orb())
        self.assertEqual(res["snapshot_quote"], "no quotes in these packs")


# ---- run_suite with a canned pool ----------------------------------------------------------------------------

_IDS = itertools.count(1)


class FakePool:
    """Stands in for pool.run_all: makes a finished run per job whose net P&L is `net_of(config)`."""

    def __init__(self, net_of, *, fail=()):
        self.net_of, self.fail, self.jobs, self.n = net_of, tuple(fail), [], 0

    def __call__(self, jobs, *, workers=None):
        self.jobs += list(jobs)
        out = []
        for j in jobs:
            self.n += 1
            if any(j.config["name"].endswith("-" + f) for f in self.fail):
                out.append(PoolOutcome(None, "failed", "the run broke on purpose"))
                continue
            rid = f"v{next(_IDS)}"
            make_run(rid, j.config.get("family_id"), navs=[CAP + self.net_of(j.config)], capital_paise=CAP,
                     role=j.role, job_id=j.job_id, name=j.config["name"])
            if j.config.get("unlock_holdout"):          # what runs.launch does for a run over the holdout
                store.log_holdout_unlock(rid, j.config.get("family_id"), j.config["unlock_holdout"], job_id=j.job_id)
            out.append(PoolOutcome(rid, "done", None))
        return out


class SuiteBase(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        self.cfg = orb()
        patcher = mock.patch.object(bundle, "resolve_bundle", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def source(self, net: int, *, cfg=None, run_id="src", status="done") -> str:
        cfg = cfg or self.cfg
        make_run(run_id, cfg["family_id"], navs=[CAP + net], capital_paise=CAP, status=status, with_folder=True,
                 name=cfg["name"])
        store.update_run(run_id, config_json=numerics.dumps_canonical(cfg))
        return run_id

    def suite(self, net_of, base=5_000, **kw):  # noqa: ANN001, ANN003
        rid = self.source(base)
        fake = FakePool(net_of, fail=kw.pop("fail", ()))
        res = sensitivity.run_suite(rid, pool_run=fake, now_ns=1_700_000_000_000_000_000, **kw)
        return res, fake


def net_default(cfg: dict) -> int:
    return 5_000


class FragileTests(SuiteBase):
    def test_fragile_when_costs_x1_5_turns_it_negative(self):
        def net(cfg):
            return -300 if cfg.get("costs", {}).get("scale") == "1.5" else 5_000
        res, _ = self.suite(net)
        self.assertTrue(res.fragile)
        self.assertEqual(res.verdict, "fragile")
        self.assertEqual(res.baseline_net_paise, 5_000)
        self.assertTrue(any("1.5" in r for r in res.reasons), res.reasons)

    def test_fragile_when_one_extra_tick_turns_it_negative(self):
        def net(cfg):
            return -1 if cfg["fill_model"]["slippage"].get("extra_ticks") == 1 else 5_000
        res, _ = self.suite(net)
        self.assertEqual(res.verdict, "fragile")
        self.assertTrue(res.fragile)
        self.assertTrue(any("tick" in r for r in res.reasons), res.reasons)

    def test_robust_when_it_survives_both(self):
        def net(cfg):                                          # x2 and +2 ticks go negative; they are not the test
            if cfg.get("costs", {}).get("scale") == "2" or cfg["fill_model"]["slippage"].get("extra_ticks") == 2:
                return -50
            return 100
        res, _ = self.suite(net)
        self.assertEqual((res.verdict, res.fragile), ("robust", False))

    def test_already_losing_is_not_fragile(self):
        rid = self.source(-2_000)
        res = sensitivity.run_suite(rid, pool_run=FakePool(lambda c: -9_000), now_ns=1)
        self.assertEqual((res.verdict, res.fragile), ("already_losing", False))

    def test_a_zero_baseline_is_already_losing(self):
        rid = self.source(0)
        res = sensitivity.run_suite(rid, pool_run=FakePool(lambda c: -9_000), now_ns=1)
        self.assertEqual(res.verdict, "already_losing")

    def test_failed_variant_is_a_row_with_its_error(self):
        res, _ = self.suite(net_default, fail=("costs_x2",))
        rows = {r.key: r for r in res.rows}
        self.assertEqual(rows["costs_x2"].status, "failed")
        self.assertEqual(rows["costs_x2"].error, "the run broke on purpose")
        self.assertIsNone(rows["costs_x2"].net_pnl_paise)
        self.assertEqual(rows["costs_x1.5"].status, "done")
        self.assertEqual(rows["costs_x1.5"].delta_net_paise, 0)
        self.assertEqual(res.verdict, "robust")

    def test_a_failed_threshold_run_is_said_not_silently_robust(self):
        res, _ = self.suite(net_default, fail=("costs_x1.5",))
        self.assertTrue(any("could not be checked" in r for r in res.reasons), res.reasons)

    def test_the_baseline_is_a_row_from_the_source_and_is_not_rerun(self):
        res, fake = self.suite(net_default)
        base = res.rows[0]
        self.assertEqual((base.key, base.status, base.run_id, base.net_pnl_paise, base.delta_net_paise),
                         ("baseline", "baseline", "src", 5_000, 0))
        self.assertEqual(len(fake.jobs), 9)                    # 10 variants less the not applicable eod_close

    def test_unapplicable_fill_model_is_listed_with_a_reason(self):
        res, fake = self.suite(net_default)
        row = {r.key: r for r in res.rows}["fill_eod_close"]
        self.assertEqual(row.status, "not_applicable")
        self.assertIn("price_basis", row.error)
        self.assertIsNone(row.run_id)
        self.assertFalse(any(j.config["fill_model"]["kind"] == "eod_close" for j in fake.jobs))

    def test_a_variant_the_validator_refuses_is_not_applicable_never_a_crash(self):
        cfg = orb(fill_model={**ORB["fill_model"], "slippage": {"kind": "bps", "x": "0.0005", "extra_ticks": 999}})
        rid = self.source(5_000, cfg=cfg)
        res = sensitivity.run_suite(rid, pool_run=FakePool(net_default), now_ns=1)
        rows = {r.key: r for r in res.rows}
        self.assertEqual(rows["slip_plus1"].status, "done")                     # 1000 is the limit
        self.assertEqual(rows["slip_plus2"].status, "not_applicable")           # 1001 is not
        self.assertIn("extra_ticks", rows["slip_plus2"].error)

    def test_sensitivity_runs_do_not_change_n(self):
        from stockify.research import registry
        self.source(5_000)
        before = len(registry.trials(self.cfg["family_id"]))
        sensitivity.run_suite("src", pool_run=FakePool(net_default), now_ns=1)
        self.assertEqual(len(registry.trials(self.cfg["family_id"])), before)
        roles = {r["role"] for r in store.list_runs(family_id=self.cfg["family_id"]) if r["run_id"].startswith("v")}
        self.assertEqual(roles, {"sensitivity"})


class JobAndHoldoutTests(SuiteBase):
    def test_the_job_row_is_made_before_any_variant_and_finished_after(self):
        seen = {}

        def pool_run(jobs, *, workers=None):
            job = store.get_research_job(jobs[0].job_id)
            seen["status"], seen["kind"], seen["workers"] = job["status"], job["kind"], workers
            return FakePool(net_default)(jobs, workers=workers)
        self.source(5_000)
        res = sensitivity.run_suite("src", pool_run=pool_run, now_ns=1_700_000_000_000_000_000, workers=3)
        self.assertEqual((seen["status"], seen["kind"], seen["workers"]), ("running", "sensitivity", 3))
        job = store.get_research_job(res.job_id)
        self.assertEqual((job["status"], job["family_id"]), ("done", self.cfg["family_id"]))
        self.assertRegex(res.job_id, r"^sens-orb_reliance_minute-\d{8}T\d{6}-[0-9a-f]{6}$")
        self.assertTrue(paths.safe_segment(res.job_id, 64))
        self.assertIsNotNone(job["finished_ns"])

    def test_source_unlock_is_not_inherited_and_the_job_reason_logs_one_row(self):
        self.source(5_000, cfg=orb(unlock_holdout="the old reason"))
        fake = FakePool(net_default)
        res = sensitivity.run_suite("src", pool_run=fake, unlock_reason="checking the holdout once", now_ns=1)
        self.assertTrue(fake.jobs)
        for j in fake.jobs:
            self.assertEqual(j.config["unlock_holdout"], "checking the holdout once")
            self.assertEqual((j.role, j.job_id), ("sensitivity", res.job_id))
        rows = [u for u in store.holdout_unlock_rows() if u["job_id"] == res.job_id]
        self.assertEqual(len(rows), 1)                         # one row per (job, reason), however many variants
        self.assertEqual(rows[0]["reason"], "checking the holdout once")
        self.assertNotIn("the old reason", json.dumps(store.holdout_unlock_rows()))

    def test_without_a_reason_no_variant_carries_one(self):
        self.source(5_000, cfg=orb(unlock_holdout="the old reason"))
        fake = FakePool(net_default)
        sensitivity.run_suite("src", pool_run=fake, now_ns=1)
        self.assertTrue(all(j.config["unlock_holdout"] is None for j in fake.jobs))

    def test_holdout_locked_source_refuses_without_unlock(self):
        cfg = orb(data={"packs": [{"spec": {"dataset": "bars", "interval": "minute", "instruments": ["NSE:RELIANCE"],
                                            "from": "2026-07-20", "to": "2026-08-10"}}]})
        self.source(5_000, cfg=cfg)
        fake = FakePool(net_default)
        patched = {**research.settings(), "holdout_from": "2026-08-01"}
        with mock.patch.object(research, "settings", return_value=patched):
            with self.assertRaises(holdout.HoldoutLocked):
                sensitivity.run_suite("src", pool_run=fake, now_ns=1)
            self.assertEqual(fake.jobs, [])
            self.assertEqual(store.list_research_jobs(kind="sensitivity"), [])
            sensitivity.run_suite("src", pool_run=fake, unlock_reason="a real reason", now_ns=1)
        self.assertTrue(fake.jobs)

    def test_unknown_or_running_run_refused(self):
        with self.assertRaisesRegex(sensitivity.SensitivityError, "no run"):
            sensitivity.run_suite("nope", pool_run=FakePool(net_default))
        self.source(1, status="running")
        with self.assertRaisesRegex(sensitivity.SensitivityError, "not finished"):
            sensitivity.run_suite("src", pool_run=FakePool(net_default))
        self.assertEqual(store.list_research_jobs(kind="sensitivity"), [])


class ResultFileTests(SuiteBase):
    def test_result_is_written_atomically_and_finite_json(self):
        written = []
        real = fileio.atomic_write_text

        def spy(path, text):
            written.append(Path(path))
            real(path, text)
        self.source(5_000)
        with mock.patch.object(fileio, "atomic_write_text", spy):
            res = sensitivity.run_suite("src", pool_run=FakePool(net_default), now_ns=1)
        target = paths.run_path("src") / "sensitivity.json"
        self.assertEqual(written, [target])
        text = target.read_text(encoding="utf-8")
        doc = json.loads(text, parse_constant=lambda c: self.fail(f"non-finite {c}"))
        self.assertEqual((doc["run_id"], doc["job_id"], doc["verdict"]), ("src", res.job_id, res.verdict))
        self.assertEqual(len(doc["rows"]), len(res.rows))
        self.assertEqual([p.name for p in paths.run_path("src").glob("*.tmp")], [])
        for secretish in ("token", "api_key", "unlock"):
            self.assertNotIn(secretish, text.lower())

    def test_a_non_finite_sharpe_is_null_not_nan(self):
        self.source(5_000)
        real = store.get_run

        def get(run_id):
            row = real(run_id)
            if row and run_id.startswith("v"):
                m = json.loads(row["metrics_json"])
                m["sharpe"] = float("nan")
                row = {**row, "metrics_json": json.dumps(m)}
            return row
        with mock.patch.object(store, "get_run", get):
            res = sensitivity.run_suite("src", pool_run=FakePool(net_default), now_ns=1)
        doc = json.loads((paths.run_path("src") / "sensitivity.json").read_text(encoding="utf-8"))
        self.assertTrue(all(r["sharpe"] is None for r in doc["rows"] if r["status"] == "done"))
        self.assertTrue(all(r.sharpe is None or math.isfinite(r.sharpe) for r in res.rows))

    def test_the_table_lists_every_row_and_the_verdict(self):
        res, _ = self.suite(lambda c: -300 if c.get("costs", {}).get("scale") == "1.5" else 5_000)
        text = sensitivity.format_table(res)
        for r in res.rows:
            self.assertIn(r.label, text)
        self.assertIn("fragile", text.lower())
        self.assertIn("not applicable", text.lower())


class CliTests(SuiteBase):
    def run_cli(self, *argv):  # noqa: ANN002
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_sensitivity_is_no_longer_a_stub(self):
        self.assertNotIn("sensitivity", cli._STUBS)

    def test_exit_codes(self):
        code, _, err = self.run_cli("sensitivity", "--run", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no run", err)
        self.source(5_000)
        with mock.patch.object(pool, "run_all", FakePool(net_default)):
            code, out, _ = self.run_cli("sensitivity", "--run", "src", "--workers", "2")
        self.assertEqual(code, 0)
        self.assertIn("costs", out)
        with mock.patch.object(pool, "run_all", FakePool(net_default, fail=("latency_250",))):
            code, out, _ = self.run_cli("sensitivity", "--run", "src")
        self.assertEqual(code, 1)

    def test_a_locked_holdout_exits_two(self):
        self.source(5_000)
        with mock.patch.object(holdout, "enforce", side_effect=holdout.HoldoutLocked("locked, say why")):
            code, _, err = self.run_cli("sensitivity", "--run", "src")
        self.assertEqual((code, "locked" in err), (2, True))


# ---- the carried Task 13 per-component cost check -------------------------------------------------------------

def half_up(x: Fraction) -> int:
    return int((x + Fraction(1, 2)).__floor__())


def charge_rows(files) -> list[dict]:  # noqa: ANN001
    return list(csv.DictReader(io.StringIO(files["charges.csv"].decode("utf-8"))))


class PerComponentCostTests(unittest.TestCase):
    """costs.scale multiplies the TOTAL cost: every component scales, and GST follows its scaled base (never
    squared). A squared GST hides inside a total tolerance on a big row but not on a small one, so each component is
    checked on its own, on a large intraday row and on a small CNC-like row (zero or tiny brokerage)."""

    def check_golden(self, gid: str) -> None:
        # The dated GST rate (the row in force, the last one), never a literal.
        rate = Fraction(Decimal(loader.load_json("charges.json")["components"]["gst"][-1]["rate"]))
        base = charge_rows(golden_run("1", gid=gid))
        self.assertTrue(base, gid)
        for k in ("2", "1.5", "0.5"):
            scaled = charge_rows(golden_run(k, gid=gid))
            self.assertEqual(len(scaled), len(base))
            for b, s in zip(base, scaled):
                with self.subTest(golden=gid, scale=k, row=b["row_no"]):
                    for comp in ("brokerage", "exchange", "sebi"):
                        self.assertLessEqual(abs(Fraction(int(s[comp])) - Fraction(k) * int(b[comp])), 1, comp)
                    gst_base = int(s["brokerage"]) + int(s["exchange"]) + int(s["sebi"])
                    self.assertLessEqual(abs(int(s["gst"]) - half_up(rate * gst_base)), 1, (s["gst"], gst_base))

    def test_every_component_scales_and_gst_follows_its_scaled_base_on_a_large_row(self):
        self.check_golden("G09")                               # brokerage 20 rupees, exchange 29.74: GST in the hundreds

    def test_a_small_cnc_like_row_does_not_hide_a_squared_gst(self):
        self.check_golden("G01")                               # CNC: zero brokerage, tiny turnover
        self.check_golden("G03")
        for r in charge_rows(golden_run("2", gid="G03")):
            self.assertEqual(int(r["brokerage"]), 0)           # CNC brokerage is zero at any scale

    def test_a_squared_gst_would_be_caught_on_a_small_row(self):
        """The rule on whole numbers: at k=2 a base of 100 paise gives a right GST of 36; a squared one is 72."""
        rate = Fraction("0.18")
        right, squared = half_up(rate * 200), half_up(rate * 200 * 2)
        self.assertLessEqual(abs(right - 36), 1)
        self.assertGreater(abs(squared - right), 1)


# ---- end to end -------------------------------------------------------------------------------------------------

class EndToEndTests(BundleTest):
    """Real children on the oracle. The fixture bars end in 2025, before the shipped holdout (2026-08-01)."""

    def setUp(self):
        super().setUp()
        support.reset_sqlite_singleton(store)
        self.addCleanup(support.reset_sqlite_singleton, store)

    def test_suite_on_a_real_tiny_run_with_the_oracle(self):
        from stockify import runs
        rid = runs.launch(run_config(name="tiny", strategy=BUY, family_id="buy/tiny/sharpe"), impl="oracle",
                          report=False)

        def oracle_pool(jobs, *, workers=None):
            return pool.run_all([replace(j, impl="oracle") for j in jobs], workers=workers)

        res = sensitivity.run_suite(rid, workers=3, pool_run=oracle_pool)
        rows = {r.key: r for r in res.rows}
        self.assertEqual(rows["baseline"].run_id, rid)
        for key in ("costs_x0", "costs_x1.5", "costs_x2", "slip_plus1", "slip_plus2", "latency_250", "latency_1000"):
            self.assertEqual(rows[key].status, "done", (key, rows[key].error))
        self.assertEqual(rows["fill_eod_close"].status, "not_applicable")
        base = rows["baseline"].net_pnl_paise
        self.assertGreaterEqual(rows["costs_x0"].net_pnl_paise, base)                  # no costs: never worse
        self.assertLessEqual(rows["costs_x2"].net_pnl_paise, rows["costs_x1.5"].net_pnl_paise)
        self.assertLessEqual(rows["costs_x1.5"].net_pnl_paise, base)
        for r in res.rows:
            if r.status == "done":
                run = store.get_run(r.run_id)
                self.assertEqual((run["role"], run["job_id"]), ("sensitivity", res.job_id))
        doc = json.loads((paths.run_path(rid) / "sensitivity.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["verdict"], res.verdict)
        done = sum(r.status == "done" for r in res.rows)
        self.assertEqual(len(store.list_runs(family_id="buy/tiny/sharpe")), 1 + done)


if __name__ == "__main__":
    unittest.main()
