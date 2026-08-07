#!/usr/bin/env python3
"""Run the STOCKIFY test suite.

    python3 tests/run_tests.py              # everything
    python3 tests/run_tests.py -v           # per-test names
    python3 tests/run_tests.py tick_buffer  # only modules matching a substring

Exit codes:
    0  all green (tracked defects in test_known_defects.py count as green)
    1  a real failure or error
    2  an "unexpected success" — a tracked defect started passing, so its
       @unittest.expectedFailure should be removed and the test promoted

Nothing here writes to the repo: all on-disk state goes to a temp sandbox that
is removed on exit (see tests/support/__init__.py).
"""
from __future__ import annotations

import argparse
import atexit
import logging
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tests.support as support  # noqa: E402


def build_suite(pattern: str = "") -> unittest.TestSuite:
    loader = unittest.TestLoader()
    discovered = loader.discover(start_dir=str(REPO_ROOT / "tests"),
                                 top_level_dir=str(REPO_ROOT))
    if not pattern:
        return discovered

    def keep(suite):
        out = unittest.TestSuite()
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                child = keep(item)
                if child.countTestCases():
                    out.addTest(child)
            elif pattern.lower() in item.id().lower():
                out.addTest(item)
        return out

    return keep(discovered)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the STOCKIFY test suite.")
    parser.add_argument("pattern", nargs="?", default="",
                        help="only run tests whose id contains this substring")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--keep-sandbox", action="store_true",
                        help="do not delete the temp state dir on exit")
    args = parser.parse_args()

    # The modules under test log warnings on their error paths; those paths are
    # deliberately exercised, so silence them to keep the report readable.
    logging.disable(logging.CRITICAL)

    if not args.keep_sandbox:
        atexit.register(support.cleanup_sandbox)

    verbosity = 2 if args.verbose else (0 if args.quiet else 1)
    suite = build_suite(args.pattern)

    started = time.time()
    result = unittest.TextTestRunner(verbosity=verbosity, stream=sys.stderr).run(suite)
    elapsed = time.time() - started

    total = result.testsRun
    failures = len(result.failures)
    errors = len(result.errors)
    skipped = len(result.skipped)
    tracked = len(result.expectedFailures)
    unexpected = len(result.unexpectedSuccesses)
    passed = total - failures - errors - skipped - tracked - unexpected

    print()
    print("─" * 62)
    print(f"  STOCKIFY suite — {total} tests in {elapsed:.2f}s")
    print(f"    passed            {passed}")
    if skipped:
        print(f"    skipped           {skipped}")
    if tracked:
        print(f"    tracked defects   {tracked}   (expected failures)")
    if failures:
        print(f"    FAILURES          {failures}")
    if errors:
        print(f"    ERRORS            {errors}")
    if unexpected:
        print(f"    FIXED             {unexpected}   <- promote these")
    print("─" * 62)

    if unexpected:
        print()
        print("  A tracked defect now passes. Remove its @unittest.expectedFailure")
        print("  in tests/test_known_defects.py and move it into the live suite:")
        for test in result.unexpectedSuccesses:
            print(f"    - {test.id()}")
        return 2

    if failures or errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
