"""OM 6.1 (M6.12, Task 38): the measured-numbers generator. A number with no sample prints "not measured yet",
never a number; every number carries its sample size and source; nothing in the output or the draft names a path,
a host or a secret."""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401  — must precede project imports

from stockify import __main__ as cli, paths, store
from stockify.tools import measure

ENTRY_KEYS = {"value", "n", "source", "needs"}
DRAFT = Path(__file__).resolve().parents[2] / "docs" / "plan" / "writeup_draft.md"
PLAN_ID = re.compile(r"\b(?:D-\d+|M\d\.\d+|M\d\b|OM \d)")


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
        self.assertIn("0 parity days", got["journal_parity"]["needs"])
        self.assertIn("5 re-runs: 0 so far", got["determinism_rerun"]["needs"])
        self.seeded(days=3)
        part = measure.collect()
        self.assertIsNone(part["journal_parity"]["value"])
        self.assertEqual(part["journal_parity"]["n"], 3)           # the sample is the days, not the rows
        self.assertIn("3 parity days", part["journal_parity"]["needs"])

    def test_parity_days_are_never_called_live_days(self):
        # nothing in parity_results or its session can tell a live day from a test day: five days is still
        # "not measured yet", and the text says what was counted
        self.seeded(days=5)
        put("journal", "2026-12-01", "pass", sid="s2")             # a second session on one day: still 5 days
        got = measure.collect()
        for name in ("journal_parity", "recorder_parity_nav", "recorder_parity_fills"):
            self.assertIsNone(got[name]["value"], name)
            self.assertEqual(got[name]["n"], 5, name)
            self.assertIn("live and test", got[name]["needs"], name)
        self.assertIn("live and test", measure.LABELS["journal_parity"])

    def test_determinism_needs_five_reruns(self):
        for i in range(5):
            for rid, role in ((f"r{i}", "trial"), (f"r{i}-rerun", "rerun")):
                store.create_run({"run_id": rid, "role": role, "status": "done"})
                folder = paths.run_path(rid)
                folder.mkdir(parents=True, exist_ok=True)
                same = i != 0 or role == "trial"                    # the first re-run differs from its source
                (folder / "determinism.json").write_text(json.dumps({"a": i if same else 99}), "utf-8")
            if i < 4:
                self.assertIsNone(measure.collect()["determinism_rerun"]["value"])
        got = measure.collect()["determinism_rerun"]
        self.assertEqual(got["n"], 5)
        self.assertEqual(got["value"], {"reproduced": 4, "total": 5})
        self.assertIsNone(got["needs"])

    def test_each_number_carries_its_sample_and_source(self):
        self.seeded(days=5)
        got = measure.collect()
        for name, entry in got.items():
            self.assertEqual(set(entry), ENTRY_KEYS, name)
            self.assertTrue(entry["source"], name)
            self.assertIsInstance(entry["n"], int, name)
        self.assertEqual(got["journal_parity"]["n"], 5)
        self.assertIn("parity_results", got["journal_parity"]["source"])

    def test_cross_platform_determinism_is_stated_as_not_measured(self):
        got = measure.collect()
        entry = got["determinism_cross_platform"]
        self.assertIsNone(entry["value"])
        self.assertIn("Windows only", entry["needs"])
        self.assertIn("cross-platform not measured", measure.render(got))

    def test_no_internal_ids_or_class_names_in_public_text(self):
        text = measure.render(measure.collect())
        self.assertIsNone(PLAN_ID.search(text), PLAN_ID.search(text))
        self.assertNotIn("Error", text)

    def test_output_has_no_paths_or_secrets(self):
        self.seeded(days=5)
        text = measure.render(measure.collect())
        self.assertEqual(measure.privacy_problems(text), [])
        for bad in (str(tests.support.SANDBOX_ROOT), str(paths.lab_dir()), os.path.expanduser("~")):
            self.assertNotIn(bad.lower(), text.lower())

    def test_scan_catches_each_kind_of_leak(self):
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        leaks = [f"x {paths.lab_dir()} y", os.path.expanduser("~"), "KITE_API_KEY", "an access_token here",
                 "api_key=1", "api_secret", "a" * 32, "dGhpcyBpcyBhIGxvbmcgYmFzZTY0IHN0cmluZyBvZiBzb21lIGtpbmQ=",
                 r"C:\Users\z", "d:/lab/runs", r"\\server\share\x", "see 127.0.0.1:8001", "http://localhost/x",
                 "me@example.com", f"on {Path.home().name}"]
        for leak in leaks:
            self.assertTrue(measure.privacy_problems(leak), leak)
        if user:
            self.assertTrue(measure.privacy_problems(f"by {user}"))
        with mock.patch("socket.gethostname", return_value="labbox-17"):
            self.assertTrue(measure.privacy_problems("run on labbox-17"))
        # a broker's name in plain words is fine
        self.assertEqual(measure.privacy_problems("Kite's answers and a tokenizer"), [])

    def test_draft_table_is_the_generators_output_and_the_draft_is_clean(self):
        draft = DRAFT.read_text(encoding="utf-8")
        lines = draft.splitlines()
        self.assertEqual(draft.count("```") % 2, 0, "a code fence is left open")
        self.assertIn("## Measured numbers", lines)
        self.assertIn("## Architecture", lines)
        start = lines.index("## Measured numbers")
        self.assertNotIn("```", "\n".join(lines[start:]))                # the table is outside every fence
        first = next(i for i in range(start, len(lines)) if lines[i].startswith("| Measure |"))
        self.assertFalse(any(x.startswith("#") for x in lines[start + 1:first]))   # right under the heading
        last = first
        while last + 1 < len(lines) and lines[last + 1].startswith("|"):
            last += 1
        table = "".join(line + "\n" for line in lines[first:last + 1])
        self.assertEqual(table, measure.render(measure.collect()))      # the empty sandbox lab
        self.assertEqual(measure.privacy_problems(draft), [])
        self.assertIsNone(PLAN_ID.search(draft), PLAN_ID.search(draft))
        self.assertNotIn("Error", draft)
        self.assertIn("paper trading only, no real orders", draft.lower())

    def test_json_is_finite(self):
        self.seeded(days=5)
        got = measure.collect()
        measure.check_finite(got)
        json.loads(json.dumps(got, allow_nan=False))
        with self.assertRaises(ValueError):
            measure.check_finite({"a": {"value": float("nan")}})
        with self.assertRaises(ValueError):
            measure.check_finite({"a": {"value": [float("inf")]}})

    def test_only_the_stores_own_errors_become_not_measured(self):
        rows = [{"x": 1}]
        with mock.patch.object(store, "margin_calibrations", return_value=rows), \
                mock.patch("stockify.fno.margin_check.scorecard", side_effect=KeyError("bug")):
            with self.assertRaises(KeyError):
                measure.collect()
        with mock.patch.object(store, "margin_calibrations", return_value=rows), \
                mock.patch("stockify.fno.margin_check.scorecard", side_effect=sqlite3.OperationalError("x")):
            got = measure.collect()["margin_error"]
        self.assertIsNone(got["value"])
        self.assertNotIn("Error", got["needs"])

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
