"""GK 5.3 (M5.4; D-71): a replay killed by the OS resumes where it stopped and matches an uninterrupted run (E-02).

The real-kill test starts the daemon as a subprocess in a throwaway copy of the test sandbox (`--replay-sandbox`),
ends it with `TerminateProcess` from inside, restarts it, finalises, and compares with a twin that never stopped.
It never touches the real lab or the real recorder, and asserts only on bytes and rows, never on timing."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf

import stockify_paths
from stockify import compare, engine, paths, store
from stockify.config import loader
from stockify.daemon import exitcodes, journal, lifecycle, market_day
from stockify.data import packs

DAY = pf.DAY
SPEC = f"replay:{DAY.isoformat()}:max"
ROOT = Path(__file__).resolve().parents[3]

# The child gets the fixture's universe (the real one does not hold the fixture's chain), then runs the real CLI or
# finalises a day. Nothing else differs from a production process.
CHILD = (
    "import json, sys\n"
    "from datetime import date\n"
    "from stockify.config import loader\n"
    "real, uni = loader.load_json, json.loads(sys.argv[1])\n"
    "loader.load_json = lambda name, *a, **kw: uni if name == 'universe.json' else real(name, *a, **kw)\n"
    "if sys.argv[2] == 'finalise':\n"
    "    from stockify.daemon import finalise\n"
    "    r = finalise.finalise_day(sys.argv[3], date.fromisoformat(sys.argv[4]), files_complete=True, cutoff_passed=True)\n"
    "    print(r.status, r.error or '')\n"
    "    sys.exit(0 if r.status == 'finalised' else 1)\n"
    "from stockify import __main__ as cli\n"
    "sys.exit(cli.main(sys.argv[2:]))\n")


def decode(sid: str) -> list[dict]:
    _, body, _ = journal.read(paths.journal_file(sid, DAY))
    out, off = [], 0
    while off < len(body):
        rec, off = packs.decode(body, off)
        out.append(rec)
    return out


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class E02KillTest(pf.PaperDayTest):
    def setUp(self):
        super().setUp()
        self.record(seconds=600)                 # 09:15:00 .. 09:25:00
        self.cfg = self.session_cfg(strategy={"kind": "script", "actions": []})
        store.create_session("s1", "s1", self.cfg, 1)
        lifecycle.arm("s1", wall_ns=2)
        self.work = Path(tempfile.mkdtemp(prefix="e02-", dir=str(support.SANDBOX_ROOT)))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def home(self, name: str) -> Path:
        """`name`'s own copy of the sandbox: the recorded day, the refdata, the armed session."""
        home = self.work / name
        if not home.exists():
            shutil.copytree(stockify_paths.state_dir(), home / "STOCKIFY" / "kite")
        return home

    def child(self, name: str, *args: str, appdata: Path | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1",
               "LOCALAPPDATA": str(appdata or self.home(name))}
        return subprocess.run([sys.executable, "-c", CHILD, json.dumps(pf.UNIVERSE), *args], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=240)

    def daemon(self, name: str, *extra: str) -> subprocess.CompletedProcess:
        return self.child(name, "daemon", "--replay-sandbox", str(self.home(name)), "--ticker", SPEC,
                          "--sessions", "s1", *extra,
                          appdata=self.work / "boot")      # the flag, not the environment, moves the lab

    def finalise(self, name: str) -> None:
        done = self.child(name, "finalise", "s1", DAY.isoformat())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def lab(self, name: str) -> Path:
        return self.home(name) / "STOCKIFY" / "kite" / "lab"

    def day_folder(self, name: str) -> Path:
        return self.lab(name) / "journals" / "s1" / DAY.strftime("%Y%m%d")

    def journal_bytes(self, name: str) -> bytes:
        return (self.lab(name) / "journals" / "s1" / f"{DAY:%Y%m%d}.evt").read_bytes()

    def test_kill_then_restart_equals_uninterrupted(self):                     # E-02, real subprocesses
        twin = self.daemon("twin")
        self.assertEqual(twin.returncode, 0, twin.stdout + twin.stderr)
        killed = self.daemon("k", "--inject", "kill@09:20:00")
        self.assertEqual(killed.returncode, 137, killed.stdout + killed.stderr)
        partial = (self.lab("k") / "journals" / "s1" / f"{DAY:%Y%m%d}.evt").stat().st_size
        self.assertLess(partial, len(self.journal_bytes("twin")))             # it really stopped part-way
        restarted = self.daemon("k")
        self.assertEqual(restarted.returncode, 0, restarted.stdout + restarted.stderr)
        self.finalise("twin")
        self.finalise("k")
        self.assertEqual(compare.compare(self.day_folder("k"), self.day_folder("twin")), [])
        self.assertEqual(self.journal_bytes("k"), self.journal_bytes("twin"))
        self.assertEqual(self.eod_state("k"), self.eod_state("twin"))
        self.assertTrue(self.eod_state("twin"))

    def eod_state(self, name: str) -> str:
        conn = sqlite3.connect(self.lab(name) / "lab.sqlite")
        try:
            return conn.execute("SELECT eod_state_json FROM session_days WHERE session_id = 's1'").fetchone()[0]
        finally:
            conn.close()


class TokenExpiryDurationTest(pf.PaperDayTest):
    """`token_expiry@T+Ns`: the restart resumes at T + N, and the open GAP(token_expired) ends at the first new frame."""

    def setUp(self):
        super().setUp()
        self.record(seconds=600)
        real = loader.load_json
        patcher = mock.patch.object(
            loader, "load_json", lambda name, *a, **kw: pf.UNIVERSE if name == "universe.json" else real(name, *a, **kw))
        patcher.start()
        self.addCleanup(patcher.stop)
        store.create_session("s1", "s1", self.session_cfg(strategy={"kind": "script", "actions": []}), 1)
        lifecycle.arm("s1", wall_ns=2)

    @unittest.skipUnless(engine.available(), "stockify._engine is not built")
    def test_token_expiry_duration_resumes_at_t_plus_n(self):
        first = market_day.run_market_day(ticker_spec=SPEC, session_ids=["s1"],
                                          faults=["token_expiry@09:20:00+120s"])
        self.assertEqual(first, exitcodes.TOKEN_EXPIRED)
        self.assertEqual(market_day.run_market_day(ticker_spec=SPEC, session_ids=["s1"]), exitcodes.OK)
        records = decode("s1")
        gaps = [(r["reason"], r["phase"], r["t_event"]) for r in records if r["kind"] == "GAP"]
        frames = [r["t_event"] for r in records if r["kind"] not in ("GAP", "CLOCK")]
        end = [g for g in gaps if g[0] == "token_expired" and g[1] == 1]
        self.assertEqual(len(end), 1, gaps)
        self.assertEqual([g for g in gaps if g[0] == "process_stop"], [])
        start = [g for g in gaps if g[0] == "token_expired" and g[1] == 0][0]
        self.assertLess(start[2] - pf.t("09:20:00"), 1_000)       # D-68 may bump a stamp by a few ns
        self.assertGreaterEqual(end[0][2], pf.t("09:22:00"))
        self.assertEqual(end[0][2], min(t for t in frames if t >= pf.t("09:22:00")))
        self.assertFalse([t for t in frames if pf.t("09:20:00") <= t < pf.t("09:22:00")])

    @unittest.skipUnless(engine.available(), "stockify._engine is not built")
    def test_resume_skips_frames_at_or_below_each_session_mark(self):
        self.assertEqual(market_day.run_market_day(ticker_spec=SPEC, session_ids=["s1"]), exitcodes.OK)
        before = (paths.journal_file("s1", DAY).read_bytes(), store.session_last_frame("s1", pf.EXPIRY))
        # The ticker is told nothing (no process mark): the session's own mark alone keeps the frames out.
        with mock.patch.object(market_day, "process_mark", lambda sessions: None):
            self.assertEqual(market_day.run_market_day(ticker_spec=SPEC, session_ids=["s1"]), exitcodes.OK)
        self.assertEqual(paths.journal_file("s1", DAY).read_bytes(), before[0])
        self.assertEqual(store.session_last_frame("s1", pf.EXPIRY)[1], before[1][1])


if __name__ == "__main__":
    unittest.main()
