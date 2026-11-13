"""M1.8 / M3.14: two builders of the same pack at once (the API runs tests side by side). Packs are
content-addressed, so the loser accepts the winner's file, even while an engine has it memory-mapped; a
different file under that name is never overwritten. Uses the M2.1 offline bars fixture."""
from __future__ import annotations

import json
import mmap
import shutil
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401  — must precede project imports

from stockify import paths
from stockify.data import packs
from tests.stockify.test_bundle import BundleTest, run

REPO = Path(__file__).resolve().parents[2]


class _Mapped:
    """Hold a pack memory-mapped and its manifest open, as a running engine and a reader would."""

    def __init__(self, sha: str):
        self.files = [packs.pack_path(sha).open("rb"), packs.manifest_path(sha).open("rb")]
        self.map = mmap.mmap(self.files[0].fileno(), 0, access=mmap.ACCESS_READ)

    def close(self):
        self.map.close()
        for f in self.files:
            f.close()


class PackRaceTests(BundleTest):
    def setUp(self):
        super().setUp()
        self.spec = run()["data"]["packs"][0]["spec"]

    def _leftovers(self, sha: str) -> list[str]:
        return sorted(p.name for p in paths.packs_dir().iterdir() if p.name not in (f"{sha}.evt", f"{sha}.json"))

    def test_the_mapped_target_really_refuses_a_replace(self):
        """The precondition of the race: Windows will not replace a mapped file (so the fix is needed)."""
        sha = packs.build(self.spec)
        held = _Mapped(sha)
        self.addCleanup(held.close)
        other = paths.packs_dir() / "other.tmp"
        shutil.copyfile(packs.pack_path(sha), other)
        with self.assertRaises(PermissionError):
            other.replace(packs.pack_path(sha))
        other.unlink()

    def _builders(self, n: int, *flags: str) -> list[str]:
        """n child processes building the spec at the same time (as API-launched runs do, I-9)."""
        import json
        import subprocess
        import sys
        procs = [subprocess.Popen([sys.executable, "-m", "tests.stockify.pack_race_builder", json.dumps(self.spec), *flags],
                                  cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(n)]
        out = []
        for p in procs:
            stdout, stderr = p.communicate(timeout=300)
            self.assertEqual(p.returncode, 0, stderr)
            out.append(stdout.strip())
        return out

    def test_racing_builders_all_return_the_same_pack(self):
        sha = packs.build(self.spec)
        before = packs.pack_path(sha).read_bytes()
        held = _Mapped(sha)
        self.addCleanup(held.close)
        self.assertEqual(self._builders(3, "--miss-first"), [sha] * 3)
        self.assertEqual(held.map[:], before)                    # the mapped pack is untouched
        self.assertEqual(self._leftovers(sha), [])              # no tmp pack or manifest left behind
        self.assertEqual(packs.manifest(sha)["sha256"], sha)

    def test_a_different_file_under_the_name_is_never_overwritten(self):
        sha = packs.build(self.spec)
        held = _Mapped(sha)
        self.addCleanup(held.close)
        before = packs.pack_path(sha).read_bytes()
        tmp = paths.packs_dir() / ".building-test.evt"
        tmp.write_bytes(before + b"different")
        target = packs.pack_path(sha)
        real_exists = Path.exists
        with mock.patch.object(Path, "exists", lambda p: False if p == target else real_exists(p)):
            with self.assertRaises(PermissionError):
                packs._publish(tmp, target, "0" * 64)       # our bytes hash to something else
        self.assertEqual(target.read_bytes(), before)
        tmp.unlink()

    def test_two_processes_with_no_engine_running(self):
        results = self._builders(2)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self._leftovers(results[0]), [])


    def test_many_builders_at_once_repeatedly(self):
        """The API case, stressed: 5 processes build one spec at once (no pack yet), 20 rounds. The manifest
        replace races too (TM 3.8); every round must give one sha and leave nothing behind."""
        import threading
        import time
        stop, idle = threading.Event(), threading.Lock()   # idle: held by the cleanup between rounds

        def reader():  # the API and engines read manifests while runs build: that is what refuses a replace
            while not stop.is_set():
                with idle:
                    for m in paths.packs_dir().glob("*.json"):
                        try:
                            with m.open("rb") as f:
                                f.read()
                        except OSError:
                            pass
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        self.addCleanup(stop.set)
        started = time.perf_counter()
        for n in range(20):
            with idle:
                for f in paths.packs_dir().iterdir():
                    f.unlink()
            with self.subTest(round=n):
                results = self._builders(5)
                self.assertEqual(len(set(results)), 1, results)
                self.assertEqual(self._leftovers(results[0]), [])
                self.assertEqual(packs.manifest(results[0])["sha256"], results[0])
            if time.perf_counter() - started > 60:
                break


    def test_an_open_manifest_with_the_same_content_is_accepted(self):
        sha = packs.build(self.spec)
        target = packs.manifest_path(sha)
        text = target.read_text(encoding="utf-8")
        with target.open("rb"):                            # a reader has it open: the replace is refused
            packs._publish_text(target, text)
            reordered = json.dumps(json.loads(text), indent=1, sort_keys=False) + chr(10)
            packs._publish_text(target, reordered)            # same content, other spelling: still accepted
        self.assertEqual(target.read_text(encoding="utf-8"), text)
        self.assertEqual(self._leftovers(sha), [])

    def test_a_refused_read_mid_replace_is_retried(self):
        """The TM 3.8 failure: our replace is refused, and so is our first read of the existing manifest,
        because another builder is replacing it at that moment. Look again instead of failing."""
        sha = packs.build(self.spec)
        target = packs.manifest_path(sha)
        text = target.read_text(encoding="utf-8")
        real_read = Path.read_text
        refused = []

        def read_once_refused(p: Path, *a, **kw):  # noqa: ANN002, ANN003, ANN202
            if p == target and not refused:
                refused.append(p)
                raise PermissionError(13, "Access is denied", str(p))
            return real_read(p, *a, **kw)
        with target.open("rb"), mock.patch.object(Path, "read_text", read_once_refused):
            packs._publish_text(target, text)
        self.assertEqual(refused, [target])
        self.assertEqual(self._leftovers(sha), [])

    def test_an_open_manifest_with_other_content_is_never_overwritten(self):
        sha = packs.build(self.spec)
        target = packs.manifest_path(sha)
        text = target.read_text(encoding="utf-8")
        other = json.loads(text)
        other["records"] += 1
        with target.open("rb"), self.assertRaises(PermissionError):
            packs._publish_text(target, json.dumps(other))
        self.assertEqual(target.read_text(encoding="utf-8"), text)
        self.assertEqual(self._leftovers(sha), [])
        self.assertTrue(packs._same_manifest(text, json.dumps({**json.loads(text), "created_ns": 5})))
        self.assertIsNone(packs._same_manifest("{half", text))


if __name__ == "__main__":
    import unittest
    unittest.main()
