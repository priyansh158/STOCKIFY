"""XI 5.12 (M5 demo; E-03): a replayed day with a disconnect, a token expiry and a kill passes journal parity.

The run is three real daemon subprocesses in a throwaway copy of the test sandbox (`--replay-sandbox`): the first ends
with the token expiry (exit 76) and, after a fake newer token file is written there, the second with the kill (exit 137);
the third finishes the day. A twin run has the same disconnect and token faults and no kill. The test never touches the
real lab, never contacts Kite, and asserts only on rows and bytes, never on timing."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf
from tests.stockify.daemon.test_e02_kill import DAY, E02KillTest

from stockify import engine, store
from stockify.daemon import journal, lifecycle
from stockify.data import packs

DISCONNECT = "disconnect@09:20:00+90s"
TOKEN = "token_expiry@09:25:00+60s"
KILL = "kill@09:30:00"


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class E03FaultsTest(E02KillTest):
    """Reuses E-02's sandbox copies and child helpers; only the scenario differs (the E-02 test itself is not re-run)."""

    test_kill_then_restart_equals_uninterrupted = None            # inherited, not part of this case

    def setUp(self):
        pf.PaperDayTest.setUp(self)
        self.record(seconds=1200)                                 # 09:15:00 .. 09:35:00
        ce = self.ces[4]                                          # one order before the faults, one after the kill
        self.cfg = self.session_cfg(strategy={"kind": "script", "actions": [
            {"on_event": 3000, "place": {"instrument": ce, "side": "BUY", "qty": 195, "product": "NRML",
                                         "type": "LIMIT", "price": 11620}},
            {"on_event": 9500, "place": {"instrument": ce, "side": "BUY", "qty": 65, "product": "NRML",
                                         "type": "MARKET"}}]})
        store.create_session("s1", "s1", self.cfg, 1)
        lifecycle.arm("s1", wall_ns=2)
        self.work = Path(tempfile.mkdtemp(prefix="e03-", dir=str(support.SANDBOX_ROOT)))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def new_login(self, name: str) -> None:
        """A fake token file inside the sandbox: the daemon only stats it, and nothing here reads or sends it."""
        f = self.home(name) / "STOCKIFY" / "kite" / "kite_token.json"
        f.write_text(json.dumps({"access_token": "fake-not-a-token"}), encoding="utf-8")

    def ok(self, name: str, *extra: str) -> None:
        r = self.daemon(name, *extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def cli(self, name: str, *args: str):  # noqa: ANN201
        return self.child(name, *args)

    def records(self, name: str) -> list[dict]:
        _, body, _ = journal.read(self.lab(name) / "journals" / "s1" / f"{DAY:%Y%m%d}.evt")
        out, off = [], 0
        while off < len(body):
            rec, off = packs.decode(body, off)
            out.append(rec)
        return out

    def coverage(self, name: str) -> dict:
        conn = sqlite3.connect(self.lab(name) / "lab.sqlite")
        try:
            return json.loads(conn.execute(
                "SELECT coverage_json FROM session_days WHERE session_id = 's1'").fetchone()[0])
        finally:
            conn.close()

    def rows(self, name: str, table: str) -> list[tuple]:
        conn = sqlite3.connect(self.lab(name) / "lab.sqlite")
        try:
            return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        finally:
            conn.close()

    def test_disconnect_token_expiry_and_kill_pass_parity_and_equal_a_twin(self):
        twin_first = self.daemon("twin", "--inject", DISCONNECT, "--inject", TOKEN)
        self.assertEqual(twin_first.returncode, 76, twin_first.stdout + twin_first.stderr)
        self.new_login("twin")
        self.ok("twin", "--inject", DISCONNECT, "--inject", TOKEN)

        first = self.daemon("k", "--inject", DISCONNECT, "--inject", TOKEN, "--inject", KILL)
        self.assertEqual(first.returncode, 76, first.stdout + first.stderr)
        self.new_login("k")
        killed = self.daemon("k", "--inject", DISCONNECT, "--inject", TOKEN, "--inject", KILL)
        self.assertEqual(killed.returncode, 137, killed.stdout + killed.stderr)
        self.ok("k", "--inject", DISCONNECT, "--inject", TOKEN)

        self.finalise("twin")
        self.finalise("k")

        reasons = sorted({r["reason"] for r in self.records("k") if r["kind"] == "GAP"})
        self.assertEqual(reasons, ["disconnect", "token_expired"])           # the kill leaves no gap of its own
        cov = self.coverage("k")
        self.assertEqual(cov, self.coverage("twin"))
        continuous = cov["continuous_s"]
        self.assertEqual(Fraction(cov["fraction"]), Fraction(continuous - 150, continuous))
        parity = self.cli("k", "parity", "journal", "--session", "s1", "--date", DAY.isoformat())
        self.assertEqual(parity.returncode, 0, parity.stdout + parity.stderr)
        self.assertTrue(parity.stdout.startswith("identical"), parity.stdout)
        self.assertEqual(self.journal_bytes("k"), self.journal_bytes("twin"))
        for table in ("orders", "order_events", "fills", "charges"):
            mine = self.rows("k", f"session_{table}")
            self.assertEqual(mine, self.rows("twin", f"session_{table}"), table)
        self.assertGreaterEqual(len(self.rows("k", "session_orders")), 2)       # the day really traded
        self.assertTrue(self.rows("k", "session_fills"))


if __name__ == "__main__":
    unittest.main()
