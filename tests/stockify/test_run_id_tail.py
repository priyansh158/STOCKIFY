"""The /tests list tells runs that share a display name apart by the tail of the run id (web/src/lib/runid.ts)."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

REPO = Path(__file__).resolve().parents[2]
CHECK = Path(__file__).with_name("run_id_check.ts")


class RunIdTailTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_tail_is_what_follows_the_name(self):
        out = subprocess.run(["node", str(CHECK)], capture_output=True, text=True, timeout=60, cwd=REPO)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [
            "ada883a0319e", "ada883a0319e-2", "ada883a0319e-rerun",
            "orb_reliance_minute_cpp-367f6479f8c0", "abc", "x-",
        ])

    def test_the_list_page_shows_the_tail_and_wraps_chips_inline(self):
        page = (REPO / "web" / "src" / "app" / "tests" / "page.tsx").read_text(encoding="utf-8")
        self.assertIn("runIdTail(run.run_id, run.name)", page)
        self.assertIn("{run.name && <div", page)          # no name: the link already shows the whole id, so no second line
        css = (REPO / "web" / "src" / "app" / "globals.css").read_text(encoding="utf-8")
        self.assertIn(".stk-run-chips", css)


if __name__ == "__main__":
    unittest.main()
