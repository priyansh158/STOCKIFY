"""Oracle vs engine on the same bundle, packs and Python strategy (M3 rule: parity section by section).

`Recorder` places no orders; it logs every callback and the answers of the context methods that exist so
far, so `events_log.txt` from the oracle and the engine's log must be identical line for line. It covers
A4 §1 (event order, phases, timers, CLOCK, day end), §2 (market state, bars built from snapshots, gaps,
price bands, staleness) and the instrument specs. Later sections add order-placing strategies here.
Skipped when the engine is not built.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden
from stockify.oracle import run_bundle
from stockify.strategy import Strategy

MIN = 60 * 1_000_000_000


class Recorder(Strategy):
    def __init__(self, instruments: list[int]):
        self.ids = instruments
        self.periodic = None
        self.fired = 0

    def _state(self, ctx, iid: int) -> str:  # noqa: ANN001
        spec = ctx.spec(iid)
        return (f"phase={ctx.phase(iid)} stale={ctx.stale(iid)} last={ctx.last_tick(iid)!r} depth={ctx.depth(iid)!r} "
                f"spec={sorted(spec.items()) if spec else None}")

    def on_start(self, ctx):  # noqa: ANN001
        ctx.log("info", f"start {ctx.now()} {ctx.today()} params={list(ctx.params.items())}")
        for iid in self.ids:
            ctx.subscribe_ticks(iid)
            for isec in (60, 300, 86400):
                ctx.subscribe_bars(iid, isec)
            ctx.log("info", f"{iid} {self._state(ctx, iid)}")
        r = ctx.rng()
        ctx.log("info", f"rng {r.below(1000)} {r.random()!r} {r.below(7)}")
        once = ctx.schedule_at(ctx.now() + 37 * MIN)
        self.periodic = ctx.schedule_every(15 * MIN, ctx.now() + 15 * MIN)
        ctx.log("info", f"timers {once} {self.periodic}")
        try:
            ctx.schedule_at(ctx.now())
        except ValueError as e:
            ctx.log("info", f"refused: {e}")

    @staticmethod
    def _indicators(ctx, iid: int, isec: int) -> str:  # noqa: ANN001
        return (f"sma={ctx.sma(iid, isec, 3)!r} ema={ctx.ema(iid, isec, 3)!r} rsi={ctx.rsi(iid, isec, 2)!r} "
                f"atr={ctx.atr(iid, isec, 2)!r} vwap={ctx.session_vwap(iid)!r}")

    @staticmethod
    def _fno(ctx) -> str:  # noqa: ANN001
        parts = []
        for u in sorted({r["underlying"] for r in ctx._rows() if r.get("underlying")}):
            opt = ctx.expiries(u, "opt")
            parts.append(f"{u} fut={ctx.expiries(u, 'FUT')} opt={opt} ce={ctx.expiries(u, 'CE')} "
                         f"futs={ctx.futures_chain(u)} chain={ctx.chain(u, opt[0]) if opt else None}")
        return " | ".join(parts) + f" none={ctx.chain('NOPE', '2026-13-01')} {ctx.futures_chain('NOPE')}"

    def on_bar(self, ctx, bar):  # noqa: ANN001
        recent = ctx.bars(bar.instrument, bar.interval_s, 2)
        ctx.log("info", f"bar {bar!r} recent={[(b.start_ns, b.close) for b in recent]} "
                        f"all={len(ctx.bars(bar.instrument, bar.interval_s, 0))} {self._state(ctx, bar.instrument)} "
                        f"{self._indicators(ctx, bar.instrument, bar.interval_s)}")

    def on_tick(self, ctx, tick):  # noqa: ANN001
        ctx.log("info", f"tick {tick!r} {self._state(ctx, tick.instrument)}")

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        self.fired += 1
        ctx.log("info", f"timer {timer_id} at {ctx.now()} rng {ctx.rng().below(100)}")
        if timer_id == self.periodic and self.fired >= 6:
            ctx.cancel_timer(timer_id)
            ctx.log("info", "periodic cancelled")

    def on_session_phase(self, ctx, change):  # noqa: ANN001
        ctx.log("info", f"phase {change!r} " + " ".join(ctx.phase(i) for i in self.ids) + f" fno: {self._fno(ctx)}")

    def on_session_end(self, ctx):  # noqa: ANN001
        ctx.log("info", f"end {ctx.now()} {ctx.today()}")


def _tick_world(seed: int) -> dict:
    """Two trading days of random snapshots for an equity, an index, a future and a CAS stock, with gaps,
    price bands and settlement prices (the goldens are bar-based; this covers A4 §2 on tick data)."""
    import random
    from datetime import date
    from stockify import timeutil
    rng = random.Random(seed)
    spec = [{"from": "2026-01-01", "lot": 1, "tick_paise": 5}]
    g = {"id": f"TICKS{seed}",
         "bundle": {"dates": ["2026-08-03", "2026-08-04"],
                    "instruments": [
                        {"id": 1, "kind": "EQ", "tradingsymbol": "AAA", "specs": spec},
                        {"id": 2, "kind": "INDEX", "tradingsymbol": "IDX", "specs": spec},
                        {"id": 3, "kind": "FUT", "tradingsymbol": "IDXFUT", "underlying_id": 2, "expiry": "2026-08-27",
                         "specs": [{"from": "2026-01-01", "lot": 50, "tick_paise": 10}]},
                        {"id": 4, "kind": "EQ", "tradingsymbol": "CASX", "session_group": "CM_CAS",
                         "cas_dates": ["2026-08-04"], "specs": spec}],
                    "sessions_by_date": {"2026-08-04": {"CM_CAS": [["PREOPEN", "09:00", "09:15"],
                                                                   ["CONTINUOUS", "09:15", "15:15"],
                                                                   ["CAS", "15:15", "15:35"],
                                                                   ["POSTCLOSE", "15:50", "16:00"]]}}},
         "run": {"fill_model": {"kind": "snapshot_quote"}, "capital_paise": 10_000_000, "price_basis": "raw"},
         "strategy": {"actions": []}, "events": []}
    for d in (date(2026, 8, 3), date(2026, 8, 4)):
        lo, hi = timeutil.ist_ns(d, "08:58"), timeutil.ist_ns(d, "15:45")
        state = {i: {"ltp": rng.randint(9_000, 11_000) * 5, "vol": 0} for i in (1, 3, 4)}
        idx = rng.randint(2_400_000, 2_600_000)
        for t in sorted(rng.randint(lo, hi) for _ in range(160)):
            k = rng.random()
            if k < 0.6:
                iid = rng.choice([1, 3, 4])
                s = state[iid]
                s["ltp"] = max(5, s["ltp"] + rng.choice([-2, -1, 0, 1, 2]) * 5)
                s["vol"] += rng.choice([0, 0, 10, 100, 1000])
                e = {"kind": rng.choice(["TICK_FULL", "TICK_QUOTE"]), "instrument": iid, "t": t, "ltp": s["ltp"],
                     "volume": s["vol"] - (50 if rng.random() < 0.05 and s["vol"] > 50 else 0),
                     "atp": s["ltp"] - 5, "high": s["ltp"] + 10, "low": s["ltp"] - 10, "open": s["ltp"]}
                if e["kind"] == "TICK_FULL":
                    e["bids"] = [[s["ltp"] - 5 * j, 10 * j, j] for j in range(1, rng.randint(1, 5) + 1)]
                    e["asks"] = [[s["ltp"] + 5 * j, 10 * j, j] for j in range(1, rng.randint(0, 5) + 1)]
            elif k < 0.75:
                idx += rng.randint(-500, 500)
                e = {"kind": "INDEX", "instrument": 2, "t": t, "value": idx}
            elif k < 0.85:
                e = {"kind": "GAP", "instrument": rng.choice([0, 1, 3]), "t": t,
                     "phase": rng.choice(["start", "end"]), "reason": rng.choice(["disconnect", "stall"])}
            elif k < 0.93:
                p = state[1]["ltp"]
                e = {"kind": "PRICE_BAND", "instrument": 1, "t": t, "lower": p - 500, "upper": p + 500}
            else:
                e = {"kind": "SETTLE", "instrument": rng.choice([2, 3]), "t": t, "date": d.isoformat(),
                     "price": idx, "subtype": rng.choice(["final", "daily"])}
            g["events"].append(e)
    return g


def _with_clocks(pack: Path, seed: int) -> Path:
    """The pack with CLOCK records inserted at random times (A4 §1.6: they must change nothing)."""
    import random
    import struct
    from stockify.data import packs as P
    rng = random.Random(seed)
    data = pack.read_bytes()
    recs, off = [], 64
    while off < len(data):
        size = struct.unpack_from("<H", data, off + 2)[0]
        recs.append(data[off:off + size])
        off += size
    times = [struct.unpack_from("<q", r, 8)[0] for r in recs]
    recs += [P.enc_clock(rng.choice(times) + rng.choice([0, 1, 250_000_000])) for _ in range(len(recs) // 3)]
    keyed = sorted(((struct.unpack_from("<q", r, 8)[0], P.RANK[struct.unpack_from("<H", r)[0]], n), r)
                   for n, r in enumerate(recs))
    body = b"".join(P._with_seq(r, i) for i, (_, r) in enumerate(keyed, start=1))
    ts = [k[0] for k, _ in keyed]
    header = P.HEADER.pack(P.MAGIC, P.VERSION, P.FLAG_CLOCK, 64, len(keyed), min(ts), max(ts), 0)
    out = pack.with_name(pack.stem + "_clock.evt")
    out.write_bytes(header + body)
    return out


def _cases():
    """Every golden (variants included) as (name, g)."""
    out = []
    for _, g in golden.goldens():
        out.append((g["id"], g))
        for v in g.get("variants", []):
            h = {k: x for k, x in g.items() if k != "variants"}
            for part in ("bundle", "run", "strategy"):
                if part in v:
                    h[part] = golden._merge(h.get(part, {}), v[part])
            if "drop_events" in v:
                h["events"] = [e for n, e in enumerate(g["events"], start=1) if n not in v["drop_events"]]
            out.append((f"{g['id']}[{v['name']}]", h))
    return out


CSV_AND_LOG = ("orders.csv", "order_events.csv", "fills.csv", "charges.csv", "positions_eod.csv", "equity.csv",
               "margin_daily.csv", "plots.csv", "events_log.txt")


def assert_same_artifacts(test: unittest.TestCase, oracle_dir: Path, engine_dir: Path) -> None:
    """DT-02 (A4 §9.3): CSVs and events_log byte-identical; metrics.json equal within a relative 1e-12."""
    import json
    import math
    for name in CSV_AND_LOG:
        a = (oracle_dir / name).read_bytes()
        b = (engine_dir / name).read_bytes()
        if a != b:
            la, lb = a.decode().splitlines(), b.decode().splitlines()
            for n, (x, y) in enumerate(zip(la, lb)):
                if x != y:
                    test.fail(f"{name} line {n + 1} differs\n oracle: {x}\n engine: {y}")
            test.fail(f"{name}: {len(la)} lines vs {len(lb)} lines")
    ma = json.loads((oracle_dir / "metrics.json").read_text())
    mb = json.loads((engine_dir / "metrics.json").read_text())

    def close(x, y, path: str) -> None:  # noqa: ANN001
        if isinstance(x, dict):
            test.assertEqual(sorted(x), sorted(y), path)
            for k in x:
                close(x[k], y[k], f"{path}.{k}")
        elif isinstance(x, float) or isinstance(y, float):
            test.assertTrue(type(x) is type(y) and math.isclose(x, y, rel_tol=1e-12, abs_tol=1e-300), f"{path}: {x!r} vs {y!r}")
        else:
            test.assertEqual(x, y, path)
    close(ma, mb, "metrics")


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class GoldenArtifactParityTests(unittest.TestCase):
    """Every golden world run by its own scripted strategy: the oracle's and the engine's run folders agree."""

    def test_every_golden_world_gives_the_same_artifacts(self):
        from stockify import engine as engine_mod
        for name, g in _cases():
            if g.get("expect", {}).get("error"):
                continue  # covered by `golden run --impl engine`, which checks the error message
            with self.subTest(golden=name), tempfile.TemporaryDirectory(prefix="parity-golden-") as tmp:
                folder = Path(tmp)
                pack = golden.build_pack(g, folder)
                bundle = golden.build_bundle(g)
                run_bundle(bundle, [pack], folder / "oracle", report=False)
                engine_mod.run_bundle(bundle, [pack], folder / "engine", report=False)
                assert_same_artifacts(self, folder / "oracle", folder / "engine")


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class RecorderParityTests(unittest.TestCase):
    def test_every_golden_world_logs_the_same(self):
        mod = engine.load()
        for name, g in _cases():
            with self.subTest(golden=name), tempfile.TemporaryDirectory(prefix="parity-") as tmp:
                folder = Path(tmp)
                pack = golden.build_pack(g, folder)
                bundle = golden.build_bundle(g)
                ids = [i["id"] for i in bundle["instruments"]]
                run_bundle(bundle, [pack], folder / "oracle", strategy=Recorder(ids), report=False)
                expected = (folder / "oracle" / "events_log.txt").read_text(encoding="utf-8").splitlines()

                e = mod.Engine(str(folder / "oracle" / "bundle.json"))
                e.add_pack(str(pack))
                e.set_strategy_py(Recorder(ids))
                e.run()
                got = e.events_log()
                del e
                self.assertGreater(len(expected), 10)
                for n, (a, b) in enumerate(zip(got, expected)):
                    if a != b:
                        self.fail(f"line {n + 1} differs\n engine: {a}\n oracle: {b}")
                self.assertEqual(len(got), len(expected))

    def test_random_tick_worlds_log_the_same(self):
        """Snapshots, index values, gaps, bands, settlement prices, built bars and CLOCKs (A4 §1.6, §2)."""
        mod = engine.load()
        for seed in range(3302, 3312):
            g = _tick_world(seed)
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="parity-ticks-") as tmp:
                folder = Path(tmp)
                pack = _with_clocks(golden.build_pack(g, folder), seed)
                bundle = golden.build_bundle(g)
                ids = [i["id"] for i in bundle["instruments"]]
                run_bundle(bundle, [pack], folder / "oracle", strategy=Recorder(ids), report=False)
                expected = (folder / "oracle" / "events_log.txt").read_text(encoding="utf-8").splitlines()
                e = mod.Engine(str(folder / "oracle" / "bundle.json"))
                e.add_pack(str(pack))
                e.set_strategy_py(Recorder(ids))
                e.run()
                got = e.events_log()
                del e
                self.assertGreater(sum("tick Tick" in x for x in expected), 100)
                self.assertGreater(sum("built=True" in x for x in expected), 20)
                for n, (a, b) in enumerate(zip(got, expected)):
                    if a != b:
                        self.fail(f"line {n + 1} differs\n engine: {a}\n oracle: {b}")
                self.assertEqual(len(got), len(expected))

    def _engine_log(self, bundle_path: Path, ids: list[int], pack: Path | None = None, batches=None) -> list[str]:  # noqa: ANN001
        e = engine.load().Engine(str(bundle_path))
        e.set_strategy_py(Recorder(ids))
        if batches is None:
            e.add_pack(str(pack))
            e.run()
        else:
            for b in batches:
                e.push(b)
            e.finish()
        out = e.events_log()
        del e
        return out

    @staticmethod
    def _records(pack: Path) -> list[bytes]:
        import struct
        data = pack.read_bytes()
        recs, off = [], 64
        while off < len(data):
            size = struct.unpack_from("<H", data, off + 2)[0]
            recs.append(data[off:off + size])
            off += size
        return recs

    def test_p02_push_in_batches_matches_run(self):
        """A4 §1.4 / A3 P-02: pushing the records in pieces, down to one record per batch (so a boundary falls
        at every timer, phase and bar-end time), gives the same log as run()."""
        import random
        rng = random.Random(3301)
        worlds = [(name, g) for name, g in _cases()] + [(f"ticks{s}", _tick_world(s)) for s in (3312, 3313)]
        for name, g in worlds:
            with self.subTest(world=name), tempfile.TemporaryDirectory(prefix="parity-push-") as tmp:
                folder = Path(tmp)
                pack = _with_clocks(golden.build_pack(g, folder), 7)
                bundle = golden.build_bundle(g)
                ids = [i["id"] for i in bundle["instruments"]]
                run_bundle(bundle, [pack], folder / "oracle", strategy=Recorder(ids), report=False)
                expected = (folder / "oracle" / "events_log.txt").read_text(encoding="utf-8").splitlines()
                records = self._records(pack)
                singles = records
                pieces, i = [], 0
                while i < len(records):
                    k = rng.randint(1, 6)
                    pieces.append(b"".join(records[i:i + k]))
                    i += k
                bundle_path = folder / "oracle" / "bundle.json"
                self.assertEqual(self._engine_log(bundle_path, ids, batches=singles), expected)
                self.assertEqual(self._engine_log(bundle_path, ids, batches=pieces), expected)

    def test_p03_clock_records_change_nothing(self):
        """A4 §1.6 / A3 P-03 on the engine: the same world with and without CLOCK records logs the same."""
        for seed in (3314, 3315, 3316):
            g = _tick_world(seed)
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="parity-clock-") as tmp:
                folder = Path(tmp)
                plain = golden.build_pack(g, folder)
                clocked = _with_clocks(plain, seed)
                bundle = golden.build_bundle(g)
                ids = [i["id"] for i in bundle["instruments"]]
                run_bundle(bundle, [plain], folder / "oracle", strategy=Recorder(ids), report=False)
                bundle_path = folder / "oracle" / "bundle.json"
                self.assertGreater(len(self._records(clocked)), len(self._records(plain)))
                self.assertEqual(self._engine_log(bundle_path, ids, pack=clocked),
                                 self._engine_log(bundle_path, ids, pack=plain))


if __name__ == "__main__":
    unittest.main()
