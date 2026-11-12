"""fileio.atomic_write_text: a reader sees the whole old or the whole new file."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports

from stockify.research import fileio


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_replaces_whole_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.md"
            fileio.atomic_write_text(target, "old old old old")
            fileio.atomic_write_text(target, "new é₹")
            self.assertEqual(target.read_text(encoding="utf-8"), "new é₹")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["report.md"])

    def test_failed_write_leaves_the_old_file_and_no_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.md"
            fileio.atomic_write_text(target, "old")
            with mock.patch.object(os, "replace", side_effect=OSError("disk gone")):
                with self.assertRaises(OSError):
                    fileio.atomic_write_text(target, "new")
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["report.md"])


if __name__ == "__main__":
    unittest.main()
