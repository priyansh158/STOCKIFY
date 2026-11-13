"""OM 6.1 (M6.12, Task 38): the measured-numbers generator. A number with no sample prints "not measured yet",
never a number; every number carries its sample size and source; nothing in the output names a path or a secret."""
from __future__ import annotations

import io
import json
import math
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

import tests.support  # noqa: F401  — must precede project imports

from stockify import __main__ as cli, paths, store
from stockify.tools import measure

ENTRY_KEYS = {"value", "n", "source", "needs"}


def put(kind: str, day: str, outcome: str = "pass", summary: dict | None = None, sid: str = "s1") -> None:
    store.put_parity_result(sid, day, kind, outcome, summary or {}, created_ns=1)


class MeasureTests(tests.support.SandboxedDBTest):
    db_modules = (store,)

    def seeded(self, days: int = 5) -> None:
        for i in range(days):
            day = f"2026-12-{i + 1:02d}"
            put("journal", day, "pass" if i else "fail")
            put("recorder", day, "pass", {"fills_live": 10, "fills_matched": 9, "pnl_diff_paise": -(i + 1) * 100})

    def test_missing_sample_prints_not_measured_yet_never_a_number(self):
        got = measure.collect()
        for name in ("journal_parity", "recorder_parity_nav", "recorder_parity_fills", "margin_error",
                     "throughput_full_day", "determinism_rerun"):
            self.assertIsNone(got[name]["value"], name)
            self.assertTrue(got[name]["needs"], name)
        text = measure.render(got)
        self.assertEqual(text.count("not measured yet"), len(got))
        self.assertIn("5 live days: 0 so far", got["journal_parity"]["needs"])
        # fewer than five live days is still not a number
        self.seeded(days=3)
        part = measure.collect()
        self.assertIsNone(part["journal_parity"]["value"])
        self.assertEqual(part["journal_parity"]["n"], 3)
        self.assertIn("5 live days: 3 so far", part["journal_parity"]["needs"])

    def test_each_number_carries_its_sample_and_source(self):
        self.seeded(days=5)
        got = measure.collect()
        for name, entry in got.items():
            self.assertEqual(set(entry), ENTRY_KEYS, name)
            self.assertTrue(entry["source"], name)
            self.assertIsInstance(entry["n"], int, name)
        self.assertEqual(got["journal_parity"]["value"], {"passed": 4, "total": 5})
        self.assertEqual(got["journal_parity"]["n"], 5)
        self.assertIsNone(got["journal_parity"]["needs"])
        self.assertEqual(got["recorder_parity_nav"]["value"], 300)          # median of |100..500| paise
        self.assertEqual(got["recorder_parity_fills"]["value"], {"matched": 45, "live": 50})
        self.assertIn("parity_results", got["journal_parity"]["source"])

    def test_cross_platform_determinism_is_stated_as_not_measured(self):
        got = measure.collect()
        entry = got["determinism_cross_platform"]
        self.assertIsNone(entry["value"])
        self.assertIn("D-26", entry["needs"])
        self.assertIn("Windows only", entry["needs"])
        self.assertIn("cross-platform not measured", measure.render(got))

    def test_output_has_no_paths_or_secrets(self):
        self.seeded(days=5)
        text = measure.render(measure.collect())
        self.assertEqual(measure.privacy_problems(text), [])
        for bad in (str(tests.support.SANDBOX_ROOT), str(paths.lab_dir()), os.path.expanduser("~"), "KITE", "token"):
            self.assertNotIn(bad.lower(), text.lower())
        self.assertNotIn(":\\", text)
        # the scan itself catches each kind of leak
        for leak in (f"x {paths.lab_dir()} y", f"{os.path.expanduser('~')}", "a KITE key", "the Token", r"C:\Users\z"):
            self.assertTrue(measure.privacy_problems(leak), leak)
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        if user:
            self.assertTrue(measure.privacy_problems(f"by {user}"))

    def test_json_is_finite(self):
        self.seeded(days=5)
        got = measure.collect()
        measure.check_finite(got)
        json.loads(json.dumps(got, allow_nan=False))
        # a NaN or infinity anywhere is refused rather than printed
        with self.assertRaises(ValueError):
            measure.check_finite({"a": {"value": float("nan")}})
        with self.assertRaises(ValueError):
            measure.check_finite({"a": {"value": [float("inf")]}})

    def test_cli_prints_text_and_json(self):
        self.seeded(days=5)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["tools", "measure"]), 0)
        self.assertIn("not measured yet", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["tools", "measure", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["journal_parity"]["n"], 5)


if __name__ == "__main__":
    unittest.main()
