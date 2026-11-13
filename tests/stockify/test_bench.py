"""M3.13: the benchmark's pieces on a small synthetic tick world (the real run uses a recorded day's pack).

Skipped when the engine is not built (`run.ps1 -BuildEngine`).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, engine, golden
from stockify.tools import bench


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class BenchTests(unittest.TestCase):
    def test_each_case_runs_and_is_timed(self):
        from tests.stockify.test_engine_parity import _tick_world
        g = _tick_world(3320)
        with tempfile.TemporaryDirectory(prefix="bench-") as tmp:
            folder = Path(tmp)
            pack, b = golden.build_pack(g, folder), golden.build_bundle(g)
            ids = [r["id"] for r in b["instruments"] if r["kind"] != "INDEX"]
            for strategy in ("noop_cpp", bench.NoopPy(), bench.SmaOnTen(ids)):
                with self.subTest(strategy=type(strategy).__name__):
                    bundle_mod.write(b, folder)
                    seconds, orders = bench.time_run(folder / "bundle.json", pack, strategy)
                    self.assertGreater(seconds, 0)
                    self.assertGreaterEqual(orders, 0)
        self.assertIn("noop_cpp", engine.load().strategies())
        self.assertIn("logical cores", bench.machine())

    @unittest.skipUnless(bench.STOCKSIM_RUN.exists(), "stocksim_run is not built")
    def test_stocksim_run_is_timed(self):
        import hashlib
        import shutil
        from stockify import paths
        from tests.stockify.test_engine_parity import _tick_world
        tests.support.fresh_cache_dir(type(self).__name__)
        g = _tick_world(3321)
        scratch = paths.lab_dir() / "scratch"
        scratch.mkdir()
        pack = golden.build_pack(g, scratch)
        sha = hashlib.sha256(pack.read_bytes()).hexdigest()
        shutil.copyfile(pack, paths.packs_dir() / f"{sha}.evt")
        b = golden.build_bundle(g)
        b["packs"] = [{"sha256": sha, "path": f"packs/{sha}.evt", "price_basis": "raw"}]
        b["strategy"] = {"kind": "cpp", "ref": "noop_cpp", "params": {}, "source_sha256": "0" * 64}
        bundle_mod.write(b, scratch)
        whole, inner = bench.time_stocksim_run(scratch / "bundle.json", scratch / "out")
        self.assertGreater(whole, inner)
        self.assertGreaterEqual(inner, 0)
        self.assertTrue((scratch / "out" / "determinism.json").exists())
        with self.assertRaises(SystemExit):
            bench.pack_file("ffff")


if __name__ == "__main__":
    unittest.main()
