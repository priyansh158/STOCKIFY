"""M6.6 (PM 6.1): the locked holdout: scope, fail-closed, the reason, the unlock log, every way a run starts."""
from __future__ import annotations

import contextlib
import io
import json
import random
import unittest
from datetime import date, timedelta
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import stockify.store as store
from stockify import __main__ as cli
from stockify import paths, runconfig, runs, sessionconfig
from stockify.data import packs
from stockify.research import holdout, registry, report
from tests.stockify import test_api_tests
from tests.stockify.research.test_registry import SESSION, cfg_with
from tests.stockify.test_bundle import BundleTest, run as run_config
from tests.stockify.test_runs_launch import BUY

HOLD = "2026-08-01"
SHA = "a" * 64


def holding(value=HOLD):
    """research.settings() with `holdout_from` set; the shipped file is never touched."""
    from stockify import research
    return mock.patch.object(research, "settings", return_value={**research.settings(), "holdout_from": value})


def cfg(frm="2026-07-01", to="2026-07-31", **extra) -> dict:
    c = cfg_with()
    spec = c["data"]["packs"][0]["spec"]
    spec["from"] = frm
    if to is None:
        spec.pop("to")
    else:
        spec["to"] = to
    return {**c, **extra}


class SpanTests(unittest.TestCase):
    def test_run_span(self):
        self.assertEqual(holdout.run_span(cfg("2026-07-01", "2026-07-31")), (date(2026, 7, 1), date(2026, 7, 31)))
        self.assertEqual(holdout.run_span(cfg("2026-07-01", None)), (date(2026, 7, 1), None))
        self.assertIsNone(holdout.run_span({"data": {"packs": [{"spec": {"dataset": "bars"}}]}}))

    def test_two_packs_take_earliest_from_and_latest_to(self):
        c = cfg("2026-07-10", "2026-07-20")
        c["data"]["packs"].append({"spec": {**c["data"]["packs"][0]["spec"], "from": "2026-06-01",
                                            "to": "2026-07-05"}})
        self.assertEqual(holdout.run_span(c), (date(2026, 6, 1), date(2026, 7, 20)))

    def test_sha_pack_comes_from_its_manifest(self):
        c = {"data": {"packs": [{"sha256": SHA}]}}
        with mock.patch.object(packs, "manifest", return_value={"spec": {"from": "2026-07-01", "to": "2026-08-03"}}):
            self.assertEqual(holdout.run_span(c), (date(2026, 7, 1), date(2026, 8, 3)))


class EnforceTests(unittest.TestCase):
    def test_off_by_default_never_locks(self):
        self.assertIsNone(holdout.holdout_from())
        self.assertIsNone(holdout.enforce(cfg("2030-01-01", None)))
        self.assertIsNone(holdout.enforce({}))                  # nothing is even read while it is off

    def test_run_ending_the_day_before_passes(self):
        with holding():
            self.assertIsNone(holdout.enforce(cfg(to="2026-07-31")))

    def test_run_ending_on_the_holdout_day_is_locked(self):
        with holding(), self.assertRaises(holdout.HoldoutLocked) as cm:
            holdout.enforce(cfg(to=HOLD))
        msg = str(cm.exception)
        self.assertIn(HOLD, msg)
        self.assertIn("unlock_holdout", msg)
        self.assertIn("--unlock-holdout", msg)
        self.assertIsInstance(cm.exception, runconfig.RunConfigError)

    def test_range_crossing_the_line_is_locked(self):
        with holding(), self.assertRaises(holdout.HoldoutLocked):
            holdout.enforce(cfg("2026-07-15", "2026-08-15"))

    def test_open_ended_pack_is_locked(self):
        with holding(), self.assertRaises(holdout.HoldoutLocked) as cm:
            holdout.enforce(cfg("2026-01-01", None))
        self.assertIn("no end date", str(cm.exception))

    def test_sha_pack_is_checked_through_its_manifest(self):
        c = {"data": {"packs": [{"sha256": SHA}]}}
        with holding(), mock.patch.object(packs, "manifest",
                                          return_value={"spec": {"from": "2026-07-01", "to": "2026-08-03"}}):
            with self.assertRaises(holdout.HoldoutLocked):
                holdout.enforce(c)
        with holding(), mock.patch.object(packs, "manifest",
                                          return_value={"spec": {"from": "2026-06-01", "to": "2026-07-03"}}):
            self.assertIsNone(holdout.enforce(c))

    def test_unknown_span_fails_closed(self):
        bad_manifests = [FileNotFoundError("no manifest"), ValueError("not json"),
                         {"spec": {}}, {"spec": {"from": "x", "to": "y"}}, {}, [], {"spec": {"from": 5, "to": 6}}]
        for m in bad_manifests:
            with self.subTest(m=repr(m)), holding():
                kw = {"side_effect": m} if isinstance(m, Exception) else {"return_value": m}
                with mock.patch.object(packs, "manifest", **kw), self.assertRaises(holdout.HoldoutLocked):
                    holdout.enforce({"data": {"packs": [{"sha256": SHA}]}})
        for c in ({}, {"data": {}}, {"data": {"packs": []}}, {"data": {"packs": [{"spec": {"to": "2026-01-01"}}]}},
                  {"data": {"packs": [{"spec": {"from": "junk", "to": "2026-01-01"}}]}},
                  {"data": {"packs": ["x"]}}):
            with self.subTest(c=c), holding(), self.assertRaises(holdout.HoldoutLocked):
                holdout.enforce(c)

    def test_unreadable_settings_fail_closed(self):
        from stockify import research
        from stockify.config import loader
        with mock.patch.object(research, "settings", side_effect=loader.ConfigError("research.json: broken")):
            with self.assertRaises(holdout.HoldoutLocked):
                holdout.enforce(cfg())

    def test_unlock_needs_a_real_reason(self):
        for bad in ("", "  ", "x", " xy ", "\t\n  ", None, 5, True, ["a long reason"]):
            with self.subTest(bad=bad), holding(), self.assertRaises(holdout.HoldoutLocked):
                holdout.enforce(cfg(to="2026-09-01", unlock_holdout=bad))
        with holding():
            self.assertEqual(holdout.enforce(cfg(to="2026-09-01", unlock_holdout="  final check  ")),
                             "final check")
            self.assertEqual(holdout.enforce(cfg(to="2026-09-01", unlock_holdout="abc")), "abc")

    def test_a_reason_on_a_run_that_does_not_need_it_is_not_an_unlock(self):
        with holding():
            self.assertIsNone(holdout.enforce(cfg(to="2026-07-01", unlock_holdout="a good reason")))

    def test_enforce_locks_exactly_when_the_run_reaches_the_holdout(self):
        rng = random.Random(2611)
        base = date(2026, 1, 1)
        for _ in range(400):
            hold = base + timedelta(days=rng.randrange(0, 300))
            frm = base + timedelta(days=rng.randrange(0, 300))
            open_ended = rng.random() < 0.2
            to = None if open_ended else frm + timedelta(days=rng.randrange(0, 120))
            c = cfg(frm.isoformat(), None if to is None else to.isoformat())
            locked = open_ended or to >= hold
            with holding(hold.isoformat()):
                try:
                    holdout.enforce(c)
                    got = False
                except holdout.HoldoutLocked:
                    got = True
                self.assertEqual(got, locked, (hold, frm, to))
                if locked:
                    self.assertEqual(holdout.enforce({**c, "unlock_holdout": "property check"}), "property check")


class ValidateTests(unittest.TestCase):
    def test_unlock_holdout_is_null_or_a_string(self):
        c = run_config()
        self.assertIsNone(runconfig.load(c)["unlock_holdout"])
        self.assertEqual(runconfig.load({**c, "unlock_holdout": "why"})["unlock_holdout"], "why")
        for bad in (5, True, ["x"], {"a": 1}):
            with self.subTest(bad=bad), self.assertRaises(runconfig.RunConfigError) as cm:
                runconfig.load({**c, "unlock_holdout": bad})
            self.assertIn("unlock_holdout", str(cm.exception))


class QueueTests(SandboxedDBTest):
    db_modules = (store,)

    def unlocks(self):
        return store.holdout_unlock_rows()

    def test_queue_refuses_before_any_row_or_folder(self):
        with holding(), self.assertRaises(holdout.HoldoutLocked):
            runs.queue(cfg(to="2026-09-01"))
        self.assertEqual(store.list_runs(), [])
        self.assertFalse(paths.runs_dir().exists() and any(paths.runs_dir().iterdir()))

    def test_unlock_runs_and_is_logged_once_with_run_and_family(self):
        with holding():
            rid = runs.queue(cfg(to="2026-09-01", unlock_holdout="final check"))
        rows = self.unlocks()
        self.assertEqual(len(rows), 1)
        row = store.get_run(rid)
        self.assertEqual((rows[0]["run_id"], rows[0]["family_id"], rows[0]["reason"], rows[0]["job_id"]),
                         (rid, row["family_id"], "final check", None))
        self.assertTrue(row["family_id"])

    def test_a_run_that_is_not_over_the_holdout_logs_nothing(self):
        with holding():
            runs.queue(cfg(unlock_holdout="not needed at all"))
        self.assertEqual(self.unlocks(), [])

    def test_twelve_runs_of_one_job_log_one_unlock(self):
        with holding():
            for _ in range(12):
                runs.queue(cfg(to="2026-09-01", unlock_holdout="sweep over the holdout"), job_id="job-1")
            runs.queue(cfg(to="2026-09-01", unlock_holdout="another reason"), job_id="job-1")
        self.assertEqual(len(store.list_runs()), 13)
        self.assertEqual(sorted(r["reason"] for r in self.unlocks()), ["another reason", "sweep over the holdout"])

    def test_standalone_runs_each_log(self):
        with holding():
            for _ in range(2):
                runs.queue(cfg(to="2026-09-01", unlock_holdout="same reason"))
        self.assertEqual(len(self.unlocks()), 2)

    def test_unlock_count_appears_on_the_family_report(self):
        with holding():
            rid = runs.queue(cfg(to="2026-09-01", unlock_holdout="final check"))
        fam = store.get_run(rid)["family_id"]
        rep = report.family_report(fam)
        self.assertEqual(len(rep.holdout_unlocks), 1)
        self.assertEqual(rep.holdout_unlocks[0]["reason"], "final check")
        self.assertEqual([f.unlocks for f in report.list_families() if f.family_id == fam], [1])

    def test_paper_sessions_are_exempt(self):
        with holding("2020-01-01"), mock.patch.object(holdout, "enforce", side_effect=AssertionError("called")):
            cfg_s = sessionconfig.load(SESSION)
        self.assertEqual(cfg_s["family_id"], registry.family_for_session(cfg_s))
        self.assertEqual(self.unlocks(), [])
        import inspect

        from server.routers import trade
        self.assertNotIn("holdout", inspect.getsource(trade))
        self.assertNotIn("holdout", inspect.getsource(sessionconfig))


class LaunchTests(BundleTest):
    def setUp(self):
        super().setUp()
        support.reset_sqlite_singleton(store)
        self.addCleanup(support.reset_sqlite_singleton, store)

    def locked_cfg(self, **kw):
        return run_config(strategy=BUY, **kw)          # bars/day 2025-12-22 .. 2025-12-31

    def test_launch_refuses_before_any_row_or_folder(self):
        with holding("2025-12-30"), self.assertRaises(holdout.HoldoutLocked):
            runs.launch(self.locked_cfg())
        self.assertEqual(store.list_runs(), [])

    def test_queue_then_launch_logs_once(self):
        with holding("2025-12-30"):
            rid = runs.queue(self.locked_cfg(unlock_holdout="final check"))
            self.assertEqual(len(store.holdout_unlock_rows()), 1)
            with mock.patch("stockify.bundle.resolve_bundle", side_effect=RuntimeError("stop here")):
                with self.assertRaises(runs.RunFailed):
                    runs.launch(run_id=rid)
        self.assertEqual(len(store.holdout_unlock_rows()), 1)

    def test_launch_of_a_queued_run_checks_the_holdout_again(self):
        rid = runs.queue(self.locked_cfg())              # queued while no holdout was set
        with holding("2025-12-30"), self.assertRaises(holdout.HoldoutLocked):
            runs.launch(run_id=rid)
        self.assertEqual(store.get_run(rid)["status"], "queued")      # untouched

    def test_launch_logs_a_standalone_unlock(self):
        with holding("2025-12-30"), mock.patch("stockify.bundle.resolve_bundle", side_effect=RuntimeError("x")):
            with self.assertRaises(runs.RunFailed):
                runs.launch(self.locked_cfg(unlock_holdout="final check"))
        rows = store.holdout_unlock_rows()
        self.assertEqual([r["reason"] for r in rows], ["final check"])
        self.assertEqual(rows[0]["run_id"], store.list_runs()[0]["run_id"])

    def test_the_resolved_bundle_is_checked_too(self):
        """The config says it ends before the holdout but the bundle holds a day on it: refused, row failed."""
        fake = {"days": [{"date": "2025-12-31"}], "packs": []}
        with holding("2025-12-30"), mock.patch("stockify.bundle.resolve_bundle", return_value=fake):
            with mock.patch.object(holdout, "enforce", return_value=None):
                with self.assertRaises(runs.RunFailed) as cm:
                    runs.launch(self.locked_cfg())
        self.assertIn("2025-12-30", str(cm.exception))
        self.assertEqual(store.list_runs()[0]["status"], "failed")

    def test_rerun_needs_the_reason_again(self):
        src = paths.run_path("old-1")
        src.mkdir(parents=True)
        for name, text in (("bundle.json", "{}"), ("manifest.json", "{}"),
                           ("run.json", json.dumps(self.locked_cfg(unlock_holdout="first time")))):
            (src / name).write_text(text, encoding="utf-8")
        with holding("2025-12-30"):
            with self.assertRaises(holdout.HoldoutLocked):
                runs.rerun("old-1")
            with mock.patch("stockify.oracle.sod_state.loads_bundle", side_effect=RuntimeError("stop")):
                with self.assertRaises(RuntimeError):
                    runs.rerun("old-1", unlock_holdout="second look")


def cli_run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(BundleTest):
    def setUp(self):
        super().setUp()
        support.reset_sqlite_singleton(store)
        self.addCleanup(support.reset_sqlite_singleton, store)
        self.path = paths.lab_dir() / "h.run.json"
        self.path.write_text(json.dumps(run_config(strategy=BUY)), encoding="utf-8")

    def test_cli_flag_sets_the_reason(self):
        seen = {}

        def fake_launch(config, **kw):
            seen["cfg"] = config
            raise runs.RunFailed("x", "stop")
        with mock.patch.object(runs, "launch", side_effect=fake_launch):
            cli_run("run", "--config", str(self.path), "--unlock-holdout", "final check")
        self.assertEqual(seen["cfg"]["unlock_holdout"], "final check")

    def test_locked_run_exits_2_naming_the_date(self):
        with holding("2025-12-30"):
            code, _, err = cli_run("run", "--config", str(self.path))
            self.assertEqual(code, 2)
            self.assertIn("2025-12-30", err)
            self.assertNotIn("Traceback", err)

    def test_oracle_run_is_locked_too_and_takes_the_flag(self):
        with holding("2025-12-30"):
            code, _, err = cli_run("oracle", "run", "--config", str(self.path))
            self.assertIn("2025-12-30", err)
            self.assertNotEqual(code, 0)
            seen = {}

            def fake_run(c, **kw):
                seen["cfg"] = c
                raise runconfig.RunConfigError("stop")
            with mock.patch("stockify.oracle.run", side_effect=fake_run):
                cli_run("oracle", "run", "--config", str(self.path), "--unlock-holdout", "final check")
            self.assertEqual(seen["cfg"]["unlock_holdout"], "final check")

    def test_oracle_run_enforces_inside_oracle_run(self):
        from stockify import oracle
        with holding("2025-12-30"), self.assertRaises(holdout.HoldoutLocked):
            oracle.run(run_config(strategy=BUY))
        self.assertEqual(list(paths.runs_dir().glob("*")) if paths.runs_dir().exists() else [], [])

    def test_rerun_flag_is_passed_on(self):
        with mock.patch.object(runs, "rerun", side_effect=runs.RunFailed("r", "stop")) as m:
            cli_run("run", "--rerun", "old-1", "--unlock-holdout", "second look")
        m.assert_called_once_with("old-1", unlock_holdout="second look")


@support.requires_fastapi
class ApiTests(test_api_tests.ApiTestBase):
    def test_holdout_locked_is_a_run_config_error_exit_2_and_api_422_naming_the_date(self):
        self.assertTrue(issubclass(holdout.HoldoutLocked, runconfig.RunConfigError))
        body = cfg(to="2026-09-01")
        body["strategy"] = BUY
        with holding():
            r = self.post(body)
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn(HOLD, json.dumps(r.json()))
        self.assertNotIn("Traceback", r.text)
        self.assertEqual(store.list_runs(), [])
        with holding():
            r = self.post({**body, "unlock_holdout": "final check"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(len(store.holdout_unlock_rows()), 1)


if __name__ == "__main__":
    unittest.main()
