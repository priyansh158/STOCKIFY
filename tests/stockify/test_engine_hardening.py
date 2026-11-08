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

if __name__ == "__main__":
    unittest.main()
