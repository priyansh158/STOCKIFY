"""CW 2: the watchlist page's sums (web/src/lib/watchlistMath.ts) on hand cases.

Needs node (>= 22.18, which runs .ts directly); skipped with a message when it is missing."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

REPO = Path(__file__).resolve().parents[3]
CHECK = Path(__file__).with_name("watchlist_check.ts")


@unittest.skipUnless(shutil.which("node"), "node is not installed: the watchlist helper checks need node >= 22.18")
class WatchlistMathChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(CHECK)], capture_output=True, text=True, timeout=60, cwd=REPO,
                             encoding="utf-8")
        if out.returncode != 0:
            raise AssertionError(f"watchlist_check.ts failed:\n{out.stderr}")
        cls.r = json.loads(out.stdout)

    def test_distance_below_the_high(self):
        self.assertEqual(self.r["below"], [0.1, 0, -0.1, None, None, None, None])

    def test_distance_above_the_low(self):
        self.assertEqual(self.r["above"], [0.5, 0, -0.1, None, None, None])

    def test_live_price_in_whole_paise(self):
        self.assertEqual(self.r["ltp"], [123450, 7, 150000, 1999])


if __name__ == "__main__":
    unittest.main()
