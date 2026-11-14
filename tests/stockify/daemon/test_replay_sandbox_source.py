"""RW 7.3: `--replay-sandbox DIR` replays a recording that lives in the REAL lab. The source is read from the real lab,
strictly read-only (recorded ticks are irreplaceable); every output stays in the sandbox.

The "real lab" here is a fake one inside the test sandbox; `market_day.real_state_dirs` is pointed at it."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support as support
from tests.stockify.daemon import paper_fixture as pf

import stockify_paths
from stockify import __main__ as cli
from stockify import engine, paths
from stockify.daemon import exitcodes, market_day, replay_ticker
from stockify.daemon.replay_ticker import LandingSource
from stockify.data import refdata

DAY = pf.DAY
SPEC = f"replay:{DAY.isoformat()}:max"


def snapshot(root: Path) -> dict[str, str]:
    """Every file under `root` (a `-shm` index file aside, which a reader may make) with its content hash."""
    out = {}
    for f in sorted(root.rglob("*")):
        if f.is_file() and not f.name.endswith("-shm"):
            out[str(f.relative_to(root))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class SandboxedReplayReadsTheRealLab(pf.PaperDayTest):
    def setUp(self):
        super().setUp()
        self.record(seconds=30)                                   # lands DAY in the (fake) real lab
        self.real_state = stockify_paths.state_dir()
        self.real_lab = self.real_state / "lab"
        self.addCleanup(replay_ticker.set_source_lab, None)
        patcher = mock.patch.object(market_day, "real_state_dirs", lambda: [self.real_state])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sandbox = Path(tempfile.mkdtemp(prefix="stockify-rw73-", dir=str(support.SANDBOX_ROOT)))

    def enter_sandbox(self):
        """What the CLI does for `--replay-sandbox`, in a process whose path helpers the tests have pinned: the
        environment is redirected, so the pinned cache dir follows it."""
        before_env, before_dir = os.environ["LOCALAPPDATA"], support._ACTIVE_CACHE_DIR
        self.addCleanup(os.environ.__setitem__, "LOCALAPPDATA", before_env)
        self.addCleanup(setattr, support, "_ACTIVE_CACHE_DIR", before_dir)
        self.assertIsNone(cli._redirect_state_for_replay(str(self.sandbox)))
        support._ACTIVE_CACHE_DIR = self.sandbox.resolve() / "STOCKIFY" / "kite"
        for mod in self.db_modules:
            support.reset_sqlite_singleton(mod)

    def test_the_source_is_found_in_the_real_lab_and_nothing_there_changes(self):
        support.reset_sqlite_singleton(refdata)                   # nothing of the real lab left open
        before = snapshot(self.real_state)
        self.assertTrue(any(k.endswith(f"ticks_{DAY:%Y%m%d}.sqlite") for k in before))
        self.enter_sandbox()
        self.assertNotEqual(paths.ticks_landing_file(DAY).parent, (self.real_lab / "ticks" / "landing"))
        self.assertFalse(paths.ticks_landing_file(DAY).exists())  # the sandbox itself holds no recording
        source = replay_ticker.source_for(DAY)
        self.addCleanup(source.close)
        self.assertIsInstance(source, LandingSource)
        self.assertEqual(source.db.path, self.real_lab / "ticks" / "landing" / f"ticks_{DAY:%Y%m%d}.sqlite")
        self.assertGreater(sum(1 for _ in source.frames()), 0)
        source.close()
        self.assertEqual(snapshot(self.real_state), before)

    def test_a_recorder_only_replay_runs_from_the_real_lab_and_writes_only_to_the_sandbox(self):
        support.reset_sqlite_singleton(refdata)
        before = snapshot(self.real_state)
        self.enter_sandbox()
        self.assertEqual(market_day.run_market_day(ticker_spec=SPEC, session_ids=[]), exitcodes.OK)
        self.assertEqual(snapshot(self.real_state), before, "the real lab was written to")
        for mod in self.db_modules:
            support.reset_sqlite_singleton(mod)
        after = snapshot(self.real_state)
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)
        self.assertTrue(paths.ticks_replay_file(DAY).is_file())   # the replay's own recording, in the sandbox
        self.assertTrue(str(paths.ticks_replay_file(DAY)).startswith(str(self.sandbox.resolve())))
        self.assertTrue(paths.daemon_status_file().is_file())
        self.assertTrue(paths.refdata_db().is_file())             # the sandbox's own copy of the instruments
        self.assertFalse((self.real_lab / "ticks" / "replay").exists())

    def test_a_recording_the_sandbox_holds_itself_wins_over_the_real_lab(self):
        """The real lab of the machine may hold other days (or none of this one): a day present in the sandbox is
        replayed from there, never reported missing because the real lab lacks it."""
        import shutil
        landing = paths.ticks_landing_file(DAY)
        copy = self.sandbox / "kept.sqlite"
        for side in ("", "-wal", "-shm"):
            if Path(str(landing) + side).is_file():
                shutil.copyfile(str(landing) + side, str(copy) + side)
        self.enter_sandbox()
        own = paths.ticks_landing_file(DAY)
        own.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(copy, own)
        elsewhere = Path(tempfile.mkdtemp(prefix="stockify-emptylab-", dir=str(support.SANDBOX_ROOT)))
        replay_ticker.set_source_lab(elsewhere)                   # a real lab that never recorded the day
        source = replay_ticker.source_for(DAY)
        self.addCleanup(source.close)
        self.assertTrue(str(source.db.path).startswith(str(self.sandbox.resolve())))

    def test_an_unrecorded_day_is_still_reported_as_missing(self):
        self.enter_sandbox()
        with self.assertRaisesRegex(FileNotFoundError, "no recording for 2026-01-02"):
            replay_ticker.source_for(__import__("datetime").date(2026, 1, 2))

    def test_a_landing_file_a_recorder_may_be_writing_is_refused_not_read(self):
        self.enter_sandbox()
        wal = self.real_lab / "ticks" / "landing" / f"ticks_{DAY:%Y%m%d}.sqlite-wal"
        wal.write_bytes(b"rows")
        self.addCleanup(wal.unlink)
        with self.assertRaisesRegex(RuntimeError, "busy"):
            replay_ticker.source_for(DAY)


if __name__ == "__main__":
    unittest.main()
