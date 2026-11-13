"""M3 review fixes for the engine (RD 4.n): inputs that crashed or aborted the process now fail cleanly.

A crash kills the Python interpreter, so each case runs in a subprocess and checks that it exits with a clear
error instead. Skipped when the engine is not built.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine
from stockify.strategy import Strategy

REPO = Path(__file__).resolve().parents[2]


def _world(lot: int = 1, tick: int = 5, extra_id: int | None = None) -> dict:
    """One traded equity (id 1) with the given spec; `extra_id` adds an untraded instrument with that id."""
    iid = 1
    rows = [{"id": iid, "kind": "EQ", "tradingsymbol": "TEST",
             "specs": [{"from": "2026-01-01", "lot": lot, "tick_paise": tick}]}]
    if extra_id is not None:
        rows.append({"id": extra_id, "kind": "EQ", "tradingsymbol": "OTHER",
                     "specs": [{"from": "2026-01-01", "lot": 1, "tick_paise": 5}]})
    return {"id": "BAD", "bundle": {"dates": ["2026-03-02"], "instruments": rows},
            "run": {"fill_model": {"kind": "bar"}, "capital_paise": 10_000_000, "price_basis": "raw"},
            "events": [{"kind": "BAR", "instrument": iid, "interval_s": 60, "start": f"2026-03-02T09:{m}:00+05:30",
                        "o": 10000, "h": 10000, "l": 10000, "c": 10000, "v": 1000} for m in (15, 16, 17)],
            "strategy": {"actions": [{"on_event": 1, "place": {"instrument": iid, "side": "BUY", "qty": 3,
                                                                "product": "CNC", "type": "LIMIT",
                                                                "price": 10003}}]}}


def _run(impl: str, world: dict) -> subprocess.CompletedProcess:
    """Run one world on one implementation in a fresh interpreter; returns the finished process."""
    with tempfile.TemporaryDirectory(prefix="hardening-") as tmp:
        spec = Path(tmp) / "world.json"
        spec.write_text(json.dumps(world), encoding="utf-8")
        code = textwrap.dedent(f"""
            import json, sys
            from pathlib import Path
            import tests.support
            from stockify import golden
            from stockify.{'oracle' if impl == 'oracle' else 'engine'} import run_bundle
            g = json.loads(Path(r"{spec}").read_text())
            folder = Path(r"{tmp}")
            try:
                run_bundle(golden.build_bundle(g), [golden.build_pack(g, folder)], folder / "run", report=False)
            except Exception as e:
                print(f"ERROR {{type(e).__name__}}: {{e}}")
                sys.exit(1)
            print("OK")
        """)
        return subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class BadSpecTests(unittest.TestCase):
    def _fails_cleanly(self, world: dict, words: str) -> None:
        for impl in ("oracle", "engine"):
            with self.subTest(impl=impl):
                p = _run(impl, world)
                self.assertEqual(p.returncode, 1, f"{impl} exited {p.returncode} (a crash?): {p.stderr[-500:]}")
                self.assertIn("ERROR", p.stdout)
                self.assertIn(words, p.stdout)

    def test_a_lot_of_zero_is_refused_when_the_bundle_loads(self):
        self._fails_cleanly(_world(lot=0), "lot must be positive")

    def test_a_tick_of_zero_is_refused_when_the_bundle_loads(self):
        self._fails_cleanly(_world(tick=0), "tick_paise must be positive")

    def test_an_instrument_id_beyond_uint32_is_refused(self):
        self._fails_cleanly(_world(extra_id=2**32), "id must be between")

    def test_a_good_spec_still_runs(self):
        for impl in ("oracle", "engine"):
            self.assertEqual(_run(impl, _world()).stdout.strip(), "OK", impl)


STOCKSIM_RUN = REPO / "engine" / "build" / "win-msvc-release" / "stocksim_run.exe"


@unittest.skipUnless(STOCKSIM_RUN.is_file(), "stocksim_run is not built")
class StocksimRunFailureTests(unittest.TestCase):
    """Every failure after the arguments are read exits 1 with events_log.txt written, never an abort."""

    def _bundle(self, folder: Path) -> Path:
        from stockify import bundle as bundle_mod, golden
        g = _world()
        b = golden.build_bundle(g)
        b["strategy"] = {"kind": "cpp", "ref": "noop_cpp", "params": {}, "source_sha256": "0" * 64}
        bundle_mod.write(b, folder)
        return folder / "bundle.json"

    def test_an_artifact_that_cannot_be_written_exits_1_with_the_log(self):
        with tempfile.TemporaryDirectory(prefix="hardening-") as tmp:
            folder = Path(tmp)
            out = folder / "out"
            (out / "orders.csv").mkdir(parents=True)     # a directory where the file must go
            p = subprocess.run([str(STOCKSIM_RUN), "--bundle", str(self._bundle(folder)), "--out", str(out),
                                "--lab", str(folder)], capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 1, f"exit {p.returncode} (an abort?): {p.stderr[-400:]}")
            self.assertIn("the run stops", p.stderr)
            log = (out / "events_log.txt").read_text(encoding="utf-8")
            self.assertIn("ERROR the run stops", log)
            self.assertIn("orders.csv", log)

    def _with_pack(self, folder: Path, tamper: bool) -> Path:
        """A noop_cpp bundle naming its pack by SHA-256, as Python's resolver writes it; `tamper` then changes one
        byte of the pack (still well-formed)."""
        import hashlib
        from stockify import bundle as bundle_mod, golden
        g = _world()
        pack = golden.build_pack(g, folder)
        sha = hashlib.sha256(pack.read_bytes()).hexdigest()
        b = golden.build_bundle(g)
        b["strategy"] = {"kind": "cpp", "ref": "noop_cpp", "params": {}, "source_sha256": "0" * 64}
        b["packs"] = [{"sha256": sha, "path": pack.name, "price_basis": "raw", "spec": {}}]
        bundle_mod.write(b, folder)
        if tamper:
            data = bytearray(pack.read_bytes())
            data[-1] ^= 1                                  # the last record's last byte
            pack.write_bytes(bytes(data))
        return folder / "bundle.json"

    def test_a_pack_that_does_not_match_its_hash_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="hardening-") as tmp:
            folder = Path(tmp)
            p = subprocess.run([str(STOCKSIM_RUN), "--bundle", str(self._with_pack(folder, True)), "--out",
                                str(folder / "out"), "--lab", str(folder)], capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(p.returncode, 1, p.stderr)
            self.assertIn("hash mismatch", p.stderr)
            self.assertIn("hash mismatch", (folder / "out" / "events_log.txt").read_text(encoding="utf-8"))
            self.assertFalse((folder / "out" / "orders.csv").exists())

    def test_a_pack_that_matches_its_hash_runs(self):
        with tempfile.TemporaryDirectory(prefix="hardening-") as tmp:
            folder = Path(tmp)
            p = subprocess.run([str(STOCKSIM_RUN), "--bundle", str(self._with_pack(folder, False)), "--out",
                                str(folder / "out"), "--lab", str(folder)], capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_good_run_still_exits_0(self):
        with tempfile.TemporaryDirectory(prefix="hardening-") as tmp:
            folder = Path(tmp)
            p = subprocess.run([str(STOCKSIM_RUN), "--bundle", str(self._bundle(folder)), "--out",
                                str(folder / "out"), "--lab", str(folder)], capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue((folder / "out" / "determinism.json").is_file())



# ── one call at a time, and nothing after a failure (review fixes #3, #4) ──────────────────────────────────

def _engine_for(world: dict, folder: Path, strategy):  # noqa: ANN001, ANN202
    from stockify import bundle as bundle_mod, golden
    pack, b = golden.build_pack(world, folder), golden.build_bundle(world)
    bundle_mod.write(b, folder)
    e = engine.load().Engine(str(folder / "bundle.json"))
    e.add_pack(str(pack))
    if isinstance(strategy, str):
        e.set_strategy_cpp(strategy)
    else:
        e.set_strategy_py(strategy)
    return e


def _many_ticks(n: int) -> dict:
    """One equity with n quote ticks over a day (a run long enough to overlap with another thread)."""
    from stockify import golden
    start = golden.ts("2026-03-02T09:15:00+05:30")
    step = (6 * 3600 * 1_000_000_000) // n
    ev = [{"kind": "TICK_QUOTE", "instrument": 1, "t": start + k * step, "ltp": 10000 + (k % 7) * 5,
           "volume": k} for k in range(n)]
    w = _world()
    w["events"], w["strategy"] = ev, {"actions": []}
    return w


def _outcome(fn) -> str:  # noqa: ANN001
    """'ok' or 'Type: message'. The exception is dropped here, so no traceback keeps the engine (and its mapped
    pack) alive."""
    try:
        fn()
        return "ok"
    except Exception as err:  # noqa: BLE001
        return f"{type(err).__name__}: {err}"


class _CallsBack(Strategy):
    """Calls back into its own engine from a hook."""

    def __init__(self, call: str):
        self.call, self.engine = call, None

    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_bars(1, 60)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        getattr(self.engine, self.call)()


class _Raises(Strategy):
    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_bars(1, 60)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        raise ZeroDivisionError("planted")


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class OneCallAtATimeTests(unittest.TestCase):
    def test_a_strategy_calling_back_into_its_engine_is_refused(self):
        for call in ("drain_outputs", "finish"):
            with self.subTest(call=call), tempfile.TemporaryDirectory(prefix="hardening-") as tmp:
                s = _CallsBack(call)
                e = _engine_for(_world(), Path(tmp), s)
                s.engine = e
                got = _outcome(e.run)
                s.engine = None
                del e
                self.assertRegex(got, r"^(RunError|RuntimeError): .*busy")

    def test_another_thread_is_refused_while_a_cpp_run_has_the_gil_released(self):
        import threading
        with tempfile.TemporaryDirectory(prefix="hardening-") as tmp:
            e = _engine_for(_many_ticks(300_000), Path(tmp), "noop_cpp")
            t = threading.Thread(target=e.run)
            seen = []
            t.start()
            while t.is_alive():
                got = _outcome(e.events_log)
                if got != "ok":
                    seen.append(got)
            t.join()
            after = _outcome(e.events_log)
            del e
            self.assertTrue(seen, "the second thread was never refused")
            self.assertTrue(all("busy" in x for x in seen), seen[:3])
            self.assertEqual(after, "ok")                         # fine once the run is over

    def test_after_a_failed_run_the_engine_refuses_everything_but_its_log(self):
        with tempfile.TemporaryDirectory(prefix="hardening-") as tmp:
            e = _engine_for(_world(), Path(tmp), _Raises())
            first = _outcome(e.run)
            log = list(e.events_log())
            later = [_outcome(lambda: e.finish()), _outcome(lambda: e.push(b"")), _outcome(e.drain_outputs),
                     _outcome(lambda: e.write_artifacts(str(Path(tmp) / "out")))]
            del e
            self.assertTrue(first.startswith("ZeroDivisionError"), first)
            self.assertTrue(any("planted" in x for x in log))    # the traceback is still readable
            for got in later:
                self.assertRegex(got, r"^(RunError|RuntimeError): .*failed")
            self.assertFalse((Path(tmp) / "out" / "orders.csv").exists())


# ── live batches (review fix #5) ──────────────────────────────────────────────────────────────────────────

def _records(n: int, t0: int = 1_000) -> list[bytes]:
    """n encoded CLOCK records at t0, t0 + 1, … (whole records, no header, as a live journal pushes them)."""
    from stockify.data import packs as P
    return [P.enc_clock(t0 + k) for k in range(n)]


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class LiveBatchTests(unittest.TestCase):
    def setUp(self):
        self.t = engine.load().testing

    def test_pushes_before_a_read_lose_nothing(self):
        r = _records(6)
        got = self.t.live_steps([("push", r[0] + r[1]), ("push", r[2] + r[3]), ("push", r[4] + r[5]), ("read", -1)])
        self.assertEqual(got[:3], ["ok", "ok", "ok"])
        self.assertEqual([k[0] for k in got[3]], [1000, 1001, 1002, 1003, 1004, 1005])

    def test_a_half_read_batch_is_kept_until_it_is_read(self):
        r = _records(6)
        got = self.t.live_steps([("push", r[0] + r[1] + r[2]), ("read", 1), ("push", r[3]), ("push", r[4] + r[5]),
                                 ("read", -1)])
        self.assertEqual([k[0] for k in got[1]], [1000])
        self.assertEqual([k[0] for k in got[4]], [1001, 1002, 1003, 1004, 1005])

    def test_a_rejected_batch_does_not_move_the_ordering_mark(self):
        r = _records(4)
        bad = r[2] + _records(1, t0=500)[0]               # in order, then a record earlier than everything
        got = self.t.live_steps([("push", r[0]), ("read", -1), ("push", bad), ("push", r[1] + r[2] + r[3]),
                                 ("read", -1)])
        self.assertEqual(got[0], "ok")
        self.assertTrue(got[2].startswith("error: pushed record out of order"), got[2])
        self.assertEqual(got[3], "ok", "a valid batch after a rejected one was refused")
        self.assertEqual([k[0] for k in got[4]], [1001, 1002, 1003])  # nothing of the rejected batch was kept


# ── checked arithmetic (review fix #7) ───────────────────────────────────────────────────────────────────

@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class CheckedArithmeticTests(unittest.TestCase):
    def test_bars_with_the_most_negative_n_is_python_slicing_not_an_overflow(self):
        """rows[-n:] with n = -2**63 is rows[2**63:], an empty list. The engine negated n in int64 (overflow, then
        a negative start index). It runs in a subprocess, since a bad index can crash the process."""
        code = textwrap.dedent("""
            import tempfile, tests.support
            from pathlib import Path
            from stockify import golden, engine as E, bundle as bundle_mod
            from stockify.strategy import Strategy
            from tests.stockify.test_engine_hardening import _world
            class Probe(Strategy):
                def on_start(self, ctx):
                    ctx.subscribe_bars(1, 60)
                def on_bar(self, ctx, bar):
                    ctx.log("info", f"bars {len(ctx.bars(1, 60, -2**63))} {len(ctx.bars(1, 60, -1))}")
            with tempfile.TemporaryDirectory() as tmp:
                f = Path(tmp); g = _world(); g["strategy"] = {"actions": []}
                pack, b = golden.build_pack(g, f), golden.build_bundle(g)
                from stockify.oracle import run_bundle
                run_bundle(b, [pack], f / "o", strategy=Probe(), report=False)
                E.run_bundle(b, [pack], f / "e", strategy=Probe(), report=False)
                for impl in ("o", "e"):
                    print(impl, [x.split(" INFO ")[1] for x in (f / impl / "events_log.txt").read_text().splitlines()
                                 if " INFO bars " in x])
        """)
        p = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, f"exit {p.returncode}: {p.stderr[-600:]}")
        lines = dict(x.split(" ", 1) for x in p.stdout.strip().splitlines())
        self.assertEqual(lines["o"], lines["e"])
        self.assertIn("bars 0 ", lines["o"])

if __name__ == "__main__":
    unittest.main()
