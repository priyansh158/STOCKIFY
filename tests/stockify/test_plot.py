"""CH 6.4: `ctx.plot` and plots.csv, the same bytes from the oracle and the engine.

A strategy is a stub whose bar handler runs a script; exceptions it provokes are captured and returned, so the test
can state which error each bad call gives. The engine half is skipped when `stockify._engine` is not built.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden
from stockify.oracle import run_bundle as oracle_run
from stockify.oracle.notes import clean_note
from stockify.strategy import Strategy
from tests.stockify.test_engine_parity import _tick_world, assert_same_artifacts
from tests.stockify.test_engine_strategies import _materialise, daily_world

HEADER = "t_ns,instrument_id,name,kind,value"
CAP_SERIES = "plot limit reached: more than 8 series"
CAP_POINTS = "plot limit reached: more than 200000 points"


class Scripted(Strategy):
    """Runs `script(ctx, bar, n)` on every bar and every tick; an exception it raises is kept in `errors`."""

    def __init__(self, script, subscribe="bars"):  # noqa: ANN001
        self.script, self.errors, self.n, self.subscribe = script, [], 0, subscribe

    def on_start(self, ctx):  # noqa: ANN001
        for r in ctx._rows() if hasattr(ctx, "_rows") else ctx.instrument_rows():
            if r["kind"] in ("EQ", "FUT") and self.subscribe == "bars":
                ctx.subscribe_bars(r["id"], 86400)
            elif r["kind"] in ("EQ",) and self.subscribe == "ticks":
                ctx.subscribe_ticks(r["id"])

    def _go(self, ctx, ev):  # noqa: ANN001
        self.n += 1
        try:
            self.script(ctx, ev, self.n)
        except Exception as e:  # noqa: BLE001
            self.errors.append(e)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        self._go(ctx, bar)

    def on_tick(self, ctx, tick):  # noqa: ANN001
        self._go(ctx, tick)


def _rows(path: Path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"), newline="")))


class PlotBase(unittest.TestCase):
    def world(self, seed=8400, future=False):  # noqa: ANN001, ANN202
        g, _ = daily_world(seed, future=future)
        tmp = tempfile.TemporaryDirectory(prefix="plot-")
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        pack, b = _materialise(g, {}, folder)
        return folder, pack, b

    def oracle(self, script, seed=8400, future=False):  # noqa: ANN001, ANN202
        folder, pack, b = self.world(seed, future)
        s = Scripted(script)
        oracle_run(b, [pack], folder / "oracle", strategy=s, report=False)
        return folder / "oracle", s


class PlotCallTests(PlotBase):
    def test_plot_rejects_floats_nan_and_bad_kinds(self):
        calls = [("name", 1.5), ("name", float("nan")), ("name", True), ("name", "1"), ("name", None)]
        seen = []

        def script(ctx, bar, n):  # noqa: ANN001
            if n > 1:
                return
            for name, v in calls:
                with self.assertRaises(TypeError, msg=repr(v)):
                    ctx.plot(name, v)
            with self.assertRaises(ValueError):
                ctx.plot("a", 1, kind="volume")
            with self.assertRaises(ValueError):
                ctx.plot("a", 1, kind="")
            with self.assertRaises(ValueError):
                ctx.plot("a", 2**63)
            with self.assertRaises(ValueError):
                ctx.plot("a", -(2**63) - 1)
            with self.assertRaises(TypeError):
                ctx.plot("a", 1, kind=3)
            with self.assertRaises(TypeError):
                ctx.plot("a", 1, "price")                       # kind is keyword-only
            with self.assertRaises(TypeError):
                ctx.plot(3, 1)
            with self.assertRaises(TypeError):
                ctx.plot("a", 1, instrument=1.0)
            ctx.plot("edge", 2**63 - 1)
            ctx.plot("edge2", -(2**63))
            seen.append(True)
        folder, s = self.oracle(script)
        self.assertEqual(s.errors, [])
        self.assertEqual(seen, [True])
        rows = _rows(folder / "plots.csv")
        self.assertEqual([r[2] for r in rows[1:3]], ["edge", "edge2"])
        self.assertEqual([r[4] for r in rows[1:3]], [str(2**63 - 1), str(-(2**63))])
        self.assertEqual(len(rows), 3, "a refused call plots nothing")

    def test_plot_name_rules(self):
        def script(ctx, bar, n):  # noqa: ANN001
            if n > 1:
                return
            for bad in ("", "x" * 33, "a\nb", " pad", "pad ", "a\tb", "\ud800", "a\x00", "é" * 33):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    ctx.plot(bad, 1)
            for good in ("x", "x" * 32, "é" * 32, "a b", 'q"u,o', "=SUM", "😀" * 32):
                ctx.plot(good, 1)
        folder, s = self.oracle(script)
        self.assertEqual(s.errors, [])
        names = [r[2] for r in _rows(folder / "plots.csv")[1:]]
        self.assertEqual(sorted(names), sorted(["x", "x" * 32, "é" * 32, "a b", 'q"u,o', "=SUM", "😀" * 32]))
        self.assertEqual({clean_note(x) for x in names}, set(names))

    def test_plot_with_unknown_instrument_raises(self):
        def script(ctx, bar, n):  # noqa: ANN001
            if n > 1:
                return
            with self.assertRaises(ValueError):
                ctx.plot("a", 1, instrument=999)
            with self.assertRaises(ValueError):
                ctx.plot("a", 1, instrument=-1)
            ctx.plot("a", 1, instrument=0)
            ctx.plot("a", 2, instrument=1)
        folder, s = self.oracle(script)
        self.assertEqual(s.errors, [])
        self.assertEqual([(r[1], r[2]) for r in _rows(folder / "plots.csv")[1:]], [("0", "a"), ("1", "a")])

    def test_a_series_keeps_its_first_kind(self):
        def script(ctx, bar, n):  # noqa: ANN001
            if n == 1:
                ctx.plot("s", 1, kind="level")
                ctx.plot("s", 5, instrument=1, kind="price")        # another series: its own kind
            elif n == 2:
                with self.assertRaises(ValueError):
                    ctx.plot("s", 2, kind="price")
                with self.assertRaises(ValueError):
                    ctx.plot("s", 2)                                # the default kind is price
                ctx.plot("s", 2, kind="level")
        folder, s = self.oracle(script)
        self.assertEqual(s.errors, [])
        rows = _rows(folder / "plots.csv")[1:]
        self.assertEqual([r[3] for r in rows if r[1] == "0"], ["level", "level"])
        self.assertEqual([r[3] for r in rows if r[1] == "1"], ["price"])

    def test_same_t_last_value_wins(self):
        def script(ctx, bar, n):  # noqa: ANN001
            if n <= 2:
                for v in (10, 20, 30):
                    ctx.plot("s", v)
                ctx.plot("s", 99, instrument=1)
                ctx.plot("s", 98, instrument=1)
        folder, s = self.oracle(script)
        self.assertEqual(s.errors, [])
        rows = _rows(folder / "plots.csv")[1:]
        self.assertEqual(sorted((r[1], r[4]) for r in rows), [("0", "30"), ("0", "30"), ("1", "98"), ("1", "98")])
        self.assertEqual(len({r[0] for r in rows}), 2, "two distinct event times")

    def test_plots_csv_is_sorted_by_time_instrument_name(self):
        def script(ctx, bar, n):  # noqa: ANN001
            if n <= 3:
                for name in ("b", "a", "B", "é"):
                    ctx.plot(name, n)
                    ctx.plot(name, -n, instrument=1)
        folder, s = self.oracle(script, future=True)
        self.assertEqual(s.errors, [])
        rows = _rows(folder / "plots.csv")
        self.assertEqual(rows[0], HEADER.split(","))
        keys = [(int(r[0]), int(r[1]), r[2]) for r in rows[1:]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), 24)
        self.assertEqual([k[2] for k in keys[:4]], ["B", "a", "b", "é"])

    def test_run_without_plots_has_a_header_only_file(self):
        folder, s = self.oracle(lambda ctx, bar, n: None)
        self.assertEqual((folder / "plots.csv").read_bytes(), (HEADER + "\n").encode())
        h = json.loads((folder / "determinism.json").read_text())
        self.assertEqual(h["plots.csv"], hashlib.sha256((HEADER + "\n").encode()).hexdigest())

    def test_plots_csv_is_a_deterministic_file_with_a_hash(self):
        def script(ctx, bar, n):  # noqa: ANN001
            ctx.plot("close", bar.close)
            ctx.plot("lvl", 12_345, kind="level", instrument=1)
        folder, pack, b = self.world()
        for k in ("a", "b"):
            oracle_run(b, [pack], folder / k, strategy=Scripted(script), report=False)
        a, c = (folder / "a" / "plots.csv").read_bytes(), (folder / "b" / "plots.csv").read_bytes()
        self.assertEqual(a, c)
        self.assertGreater(len(a), len(HEADER) + 2)
        for k in ("a", "b"):
            h = json.loads((folder / k / "determinism.json").read_text())
            self.assertEqual(h["plots.csv"], hashlib.sha256(a).hexdigest())
        from stockify.oracle import artifacts
        names = list(artifacts.DETERMINISTIC)
        self.assertEqual(names.index("plots.csv"), names.index("margin_daily.csv") + 1)
        self.assertEqual(names[names.index("plots.csv") + 1], "metrics.json")

    def test_ninth_series_and_point_200001_are_dropped_with_one_log_line(self):
        def series(ctx, bar, n):  # noqa: ANN001
            if n == 1:
                for i in range(1000):                               # the 9th series on: a thousand dropped calls
                    ctx.plot(f"s{i % 12}", i)
        folder, s = self.oracle(series)
        self.assertEqual(s.errors, [])
        rows = _rows(folder / "plots.csv")[1:]
        self.assertEqual({r[2] for r in rows}, {f"s{i}" for i in range(8)})
        lines = [x for x in (folder / "events_log.txt").read_text().splitlines() if "plot limit" in x]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^\d+ WARN " + CAP_SERIES + "$")
        self.assertEqual(lines[0].split(" ", 2)[0], rows[0][0], "stamped with the engine time of the call")

        folder, pack, b = self.world()
        s = TimerPlotter()
        oracle_run(b, [pack], folder / "o", strategy=s, report=False)
        rows = _rows(folder / "o" / "plots.csv")[1:]
        self.assertEqual(len(rows), 200_000)
        self.assertEqual([r[4] for r in rows[:2]], ["1", "2"])
        self.assertEqual(rows[-1][4], str(-200_000), "a repeat at the same time still replaces at the cap")
        lines = [x for x in (folder / "o" / "events_log.txt").read_text().splitlines() if "plot limit" in x]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^\d+ WARN " + CAP_POINTS + "$")


class TimerPlotter(Strategy):
    """One point per nanosecond-spaced timer firing: 200,000 distinct times, then drops (a thousand of them)."""

    def __init__(self):
        self.k, self.tid = 0, None

    def on_start(self, ctx):  # noqa: ANN001
        ctx.subscribe_bars(ctx.resolve("NSE:TEST"), 86400)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        if self.tid is None:
            self.tid = ctx.schedule_every(1, ctx.now() + 1)

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        self.k += 1
        ctx.plot("a", self.k)
        if self.k == 200_000:
            ctx.plot("a", -self.k)                                  # same time: replaces, never a new point
        if self.k >= 201_000:
            ctx.cancel_timer(timer_id)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class PlotParityTests(PlotBase):
    def both(self, folder, pack, b, make):  # noqa: ANN001, ANN202
        so, se = make(), make()
        oracle_run(b, [pack], folder / "oracle", strategy=so, report=False)
        engine.run_bundle(b, [pack], folder / "engine", strategy=se, report=False)
        self.assertEqual([repr(e) for e in so.errors], [repr(e) for e in se.errors])
        return folder / "oracle", folder / "engine"

    def test_engine_and_oracle_write_identical_plots_csv(self):
        def script(ctx, ev, n):  # noqa: ANN001
            ctx.plot("close", n * 10)
            ctx.plot("close", n * 10 + 1)                           # replaced at the same time
            ctx.plot("ema", 1_000_000 + n, kind="level", instrument=1)
            ctx.plot("ema", n, kind="level", instrument=2)
            ctx.plot("b,\"q\"", -n, instrument=2)
            if n == 1:
                for i in range(20):
                    ctx.plot(f"extra{i}", i)                        # past 8 series: dropped, one log line
        for seed in (8401, 8402, 8403):
            with self.subTest(seed=seed):
                folder, pack, b = self.world(seed, future=True)
                o, e = self.both(folder, pack, b, lambda: Scripted(script))
                assert_same_artifacts(self, o, e)
                self.assertEqual((o / "plots.csv").read_bytes(), (e / "plots.csv").read_bytes())
                self.assertGreater(len(_rows(e / "plots.csv")), 5)
                self.assertEqual(len([x for x in (e / "events_log.txt").read_text().splitlines()
                                      if CAP_SERIES in x]), 1)

    def test_a_snapshot_run_agrees_too(self):
        def script(ctx, tick, n):  # noqa: ANN001
            if n % 3 == 0 and n < 400:
                ctx.plot("ltp", n, instrument=1)
                ctx.plot("ltp", n + 1, instrument=1)
                ctx.plot("all", n)
        for seed in (3302, 3303):
            g = _tick_world(seed)
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="plot-snap-") as tmp:
                folder = Path(tmp)
                pack = golden.build_pack(g, folder)
                b = golden.build_bundle(g)
                self.assertNotEqual(b["run"]["fill_model"]["kind"], "bar")
                o, e = self.both(folder, pack, b, lambda: Scripted(script, subscribe="ticks"))
                assert_same_artifacts(self, o, e)
                self.assertGreater(len(_rows(e / "plots.csv")), 3)

    def test_both_raise_the_same_errors(self):
        def script(ctx, ev, n):  # noqa: ANN001
            if n > 1:
                return
            for call in (lambda: ctx.plot("a", 1.5), lambda: ctx.plot("a", True), lambda: ctx.plot("a", 1, kind="x"),
                         lambda: ctx.plot("", 1), lambda: ctx.plot("a", 1, instrument=77),
                         lambda: ctx.plot("a", 2**63), lambda: ctx.plot("\ud800", 1), lambda: ctx.plot(5, 1),
                         lambda: ctx.plot("a", 1, "price"), lambda: ctx.plot("a", 1, instrument=True)):
                try:
                    call()
                except Exception as x:  # noqa: BLE001
                    ctx.log("info", f"{type(x).__name__}: {x}")
                else:
                    ctx.log("info", "no error")
            ctx.plot("k", 1, kind="level")
            try:
                ctx.plot("k", 1)
            except Exception as x:  # noqa: BLE001
                ctx.log("info", f"{type(x).__name__}: {x}")
        folder, pack, b = self.world()
        o, e = self.both(folder, pack, b, lambda: Scripted(script))
        assert_same_artifacts(self, o, e)
        lines = (e / "events_log.txt").read_text().splitlines()
        self.assertGreaterEqual(sum("Error" in x for x in lines), 9)

    def test_seeded_random_plot_calls_are_byte_identical(self):
        """Property: for seeded random call sequences (repeats at one time, both caps, hostile names) the oracle's and
        the engine's plots.csv and events_log.txt agree byte for byte."""
        alphabet = ['"', ",", " ", "é", "😀", "a", "B", "=", "\n", "\x00", "\ud800", "z", "0"]

        def make_script(seed, many_series, many_points):  # noqa: ANN001, ANN202
            def script(ctx, ev, n):  # noqa: ANN001
                rng = random.Random(seed * 1000 + n)
                k = 40 if many_points else 6
                for _ in range(k):
                    if many_series and rng.random() < 0.5:
                        name = f"s{rng.randrange(20)}"
                    else:
                        name = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 34)))
                    value = rng.choice([0, 1, -1, 2**63 - 1, -(2**63), rng.randrange(-10**6, 10**6)])
                    kind = rng.choice(["price", "level", "price", "bad"])
                    inst = rng.choice([0, 0, 1, 2, 9])
                    try:
                        ctx.plot(name, value, kind=kind, instrument=inst)
                    except (TypeError, ValueError) as x:
                        ctx.log("info", f"{type(x).__name__}")
            return script
        for seed in range(6):
            with self.subTest(seed=seed):
                folder, pack, b = self.world(8500 + seed, future=True)
                o, e = self.both(folder, pack, b, lambda: Scripted(make_script(seed, seed % 2 == 0, seed > 2)))
                assert_same_artifacts(self, o, e)
                self.assertGreater(len(_rows(e / "plots.csv")), 1)

    def test_the_point_cap_is_the_same_in_both(self):
        folder, pack, b = self.world()
        o, e = self.both(folder, pack, b, TimerPlotter)
        self.assertEqual((o / "plots.csv").read_bytes(), (e / "plots.csv").read_bytes())
        self.assertEqual((o / "events_log.txt").read_bytes(), (e / "events_log.txt").read_bytes())
        self.assertEqual(len(_rows(e / "plots.csv")) - 1, 200_000)
        self.assertEqual(len([x for x in (e / "events_log.txt").read_text().splitlines() if CAP_POINTS in x]), 1)


if __name__ == "__main__":
    unittest.main()
