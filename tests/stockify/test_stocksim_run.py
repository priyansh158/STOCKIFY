"""M3.11: the C++ `stocksim_run` CLI (C++-registered strategies, no Python) writes the same deterministic files
as `stockify.engine.run_bundle`. Skipped when the engine (and so the exe) is not built."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, paths
from stockify.oracle.artifacts import DETERMINISTIC
from tests.stockify.test_engine_strategies import _materialise, daily_world

REPO = Path(__file__).resolve().parents[2]
EXE = REPO / "engine" / "build" / "win-msvc-release" / "stocksim_run.exe"


@unittest.skipUnless(engine.available() and EXE.exists(), "the engine and stocksim_run are not built")
class StocksimRunTests(unittest.TestCase):
    def setUp(self):
        tests.support.fresh_cache_dir(type(self).__name__)
        self.lab = paths.lab_dir()

    def _bundle(self, seed: int, kind: str = "cpp") -> tuple[dict, Path]:
        """A golden-world bundle whose pack sits in $LAB/packs, named the way resolve_bundle names it."""
        scratch = self.lab / "scratch"
        scratch.mkdir(exist_ok=True)
        g, params = daily_world(seed)
        pack, b = _materialise(g, params, scratch)
        sha = hashlib.sha256(pack.read_bytes()).hexdigest()
        target = paths.packs_dir() / f"{sha}.evt"
        shutil.copyfile(pack, target)
        b["packs"] = [{"sha256": sha, "path": f"packs/{sha}.evt", "price_basis": "raw"}]
        if kind == "cpp":
            b["strategy"] = {"kind": "cpp", "ref": "sma_cross_cpp", "params": params, "source_sha256": "0" * 64}
        else:
            b["strategy"] = {"kind": "python", "ref": "stockify.strategies.sma_cross:SmaCross", "params": params,
                             "source_sha256": "0" * 64}
        return b, target

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(EXE), *args], capture_output=True, text=True, timeout=120)

    def test_same_files_as_the_python_runner(self):
        fills = 0
        for seed in (5209, 5212, 5201):
            with self.subTest(seed=seed):
                b, pack = self._bundle(seed)
                ref = paths.runs_dir() / f"ref{seed}"
                engine.run_bundle(b, [pack], ref, report=False)
                out = paths.runs_dir() / f"cli{seed}"
                proc = self._run("--bundle", str(ref / "bundle.json"), "--out", str(out))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                for name in [*DETERMINISTIC, "determinism.json"]:
                    self.assertEqual((out / name).read_bytes(), (ref / name).read_bytes(), name)
                m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
                py = json.loads((ref / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(m["engine"], "stocksim_run")
                for k in ("engine_build_id", "bundle_sha256", "labels", "packs"):
                    self.assertEqual(m[k], py[k], k)
                self.assertIsInstance(m["duration_ms"], int)
                fills += len((out / "fills.csv").read_text().splitlines()) - 1
        self.assertGreater(fills, 5)

    def test_lab_override(self):
        b, pack = self._bundle(5209)
        ref = paths.runs_dir() / "ref"
        engine.run_bundle(b, [pack], ref, report=False)
        elsewhere = self.lab.parent / "elsewhere"
        elsewhere.mkdir()
        shutil.copyfile(ref / "bundle.json", elsewhere / "bundle.json")
        out = self.lab.parent / "out"
        self.assertNotEqual(self._run("--bundle", str(elsewhere / "bundle.json"), "--out", str(out)).returncode, 0)
        proc = self._run("--bundle", str(elsewhere / "bundle.json"), "--out", str(out), "--lab", str(self.lab))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((out / "determinism.json").read_bytes(), (ref / "determinism.json").read_bytes())

    def test_a_python_strategy_is_refused(self):
        b, pack = self._bundle(5209, kind="python")
        ref = paths.runs_dir() / "py"
        engine.run_bundle(b, [pack], ref, report=False)
        proc = self._run("--bundle", str(ref / "bundle.json"), "--out", str(paths.runs_dir() / "nope"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("only C++-registered strategies", proc.stderr)
        self.assertFalse((paths.runs_dir() / "nope").exists())

    def test_a_failure_is_logged_and_exits_1(self):
        b, pack = self._bundle(5209)
        ref = paths.runs_dir() / "ref"
        engine.run_bundle(b, [pack], ref, report=False)
        pack.unlink()
        out = paths.runs_dir() / "broken"
        proc = self._run("--bundle", str(ref / "bundle.json"), "--out", str(out))
        self.assertEqual(proc.returncode, 1)
        log = (out / "events_log.txt").read_text(encoding="utf-8")
        self.assertIn("ERROR the run stops: ", log)
        self.assertFalse((out / "orders.csv").exists())

    def test_usage_and_version(self):
        self.assertEqual(self._run().returncode, 2)
        self.assertEqual(self._run("--bundle").returncode, 2)
        proc = self._run("--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(engine.build_info()["engine_build_id"], proc.stdout)


if __name__ == "__main__":
    unittest.main()
