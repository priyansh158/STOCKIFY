"""LCM 6.4 (D-129): a log file shared by several processes rotates without an error on Windows."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

ROOT = Path(__file__).resolve().parents[2]
WORKER = """
import logging, sys
from pathlib import Path
from stockify import log as slog
h = slog.file_handler(Path(sys.argv[1]), max_bytes=3000, backups=200)
lg = logging.getLogger("rot")
lg.propagate = False
lg.addHandler(h)
lg.setLevel(logging.INFO)
for i in range(int(sys.argv[3])):
    lg.info("%s line %d %s", sys.argv[2], i, "x" * 40)
h.close()
"""


class SharedRotationTests(unittest.TestCase):
    def test_two_processes_logging_to_one_path_rotate_without_an_error(self):
        n = 600
        with tempfile.TemporaryDirectory(prefix="lr-") as d:
            path = Path(d) / "shared.log"
            procs = [subprocess.Popen([sys.executable, "-c", WORKER, str(path), tag, str(n)], cwd=ROOT,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for tag in ("A", "B")]
            outs = [p.communicate(timeout=120) for p in procs]
            for p, (out, err) in zip(procs, outs):
                self.assertEqual((p.returncode, err), (0, ""), err)        # no "--- Logging error ---"
            files = sorted(Path(d).glob("shared.log*"))
            self.assertGreater(len(files), 2, "the files should have rotated")
            # Two uncoordinated rotators can still drop a rotated file's lines (each shifts the chain); the claim
            # here is the one that matters: no process raised, both ran to the end, and each wrote its last line.
            text = "".join(f.read_text(encoding="utf-8") for f in files)
            for tag in ("A", "B"):
                self.assertIn(f" {tag} line {n - 1} ", text)

if __name__ == "__main__":
    unittest.main()
