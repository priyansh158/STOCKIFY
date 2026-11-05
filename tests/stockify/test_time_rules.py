"""Invariant I-4: only timeutil.py and daemon/clock.py read the wall clock.

Everything else takes time from `stockify.timeutil` or an injected Clock, so
the engine, the replay ticker and tests can control it.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

PACKAGE = Path(__file__).resolve().parents[2] / "stockify"
ALLOWED = {PACKAGE / "timeutil.py", PACKAGE / "daemon" / "clock.py"}
BANNED = [
    re.compile(r"\bdatetime\.now\("),
    re.compile(r"\bdatetime\.utcnow\("),
    re.compile(r"\bdate\.today\("),
    re.compile(r"\btime\.time(_ns)?\("),
    re.compile(r"\btime\.localtime\("),
    re.compile(r"\bfromtimestamp\((?![^)]*tz=)"),
]


class TimeRuleTests(unittest.TestCase):
    def test_no_wall_clock_reads_outside_timeutil(self):
        offenders = []
        for path in sorted(PACKAGE.rglob("*.py")):
            if path in ALLOWED:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if any(p.search(code) for p in BANNED):
                    offenders.append(f"{path.relative_to(PACKAGE)}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "wall-clock reads outside timeutil:\n" + "\n".join(offenders))

    def test_the_pattern_catches_what_it_should(self):
        for bad in ("x = datetime.now()", "time.time()", "date.today()",
                    "datetime.fromtimestamp(t)", "time.time_ns()"):
            self.assertTrue(any(p.search(bad) for p in BANNED), bad)
        self.assertFalse(any(p.search("datetime.fromtimestamp(t, tz=timezone.utc)") for p in BANNED))


if __name__ == "__main__":
    unittest.main()
