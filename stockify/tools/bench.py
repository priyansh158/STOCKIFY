"""Engine benchmark (M3.13): replay one recorded day and report events per second.

    python -m stockify tools bench --pack <sha or unique prefix> [--reps 3]

Three strategies, each run `reps` times (the median is reported), timing `Engine.run()` only (bundle
resolution, pack mapping and artifact writing are outside the timing):

* (a) `noop_cpp`: every instrument's ticks, nothing done (the engine's own cost per event);
* (b) a no-op Python strategy on every instrument's ticks (one Python call per tick: slow by design, A2 §3);
* (c) a Python strategy on 10 instruments: SMA crossover on minute bars built from the ticks, trading;
* (a) again through `stocksim_run` (no Python at all), when the exe is built: the whole process (start, pack
  mapping, run, artifacts) and the run alone as the tool reports it (`duration_ms`).

The bundle is resolved from the pack with the shipped dated config, as a run would be (read-only on the lab).
"""
from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from stockify import bundle as bundle_mod, engine, paths, runconfig
from stockify.strategy import Strategy


class NoopPy(Strategy):
    def on_start(self, ctx):  # noqa: ANN001
        for row in ctx._rows():
            ctx.subscribe_ticks(row["id"])


class SmaOnTen(Strategy):
    """SMA(5) vs SMA(20) on minute bars for the given instruments; one unit (a lot) long or flat each."""

    def __init__(self, ids: list[int]):
        self.ids = ids

    def on_start(self, ctx):  # noqa: ANN001
        for iid in self.ids:
            ctx.subscribe_bars(iid, 60)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        fast, slow = ctx.sma(bar.instrument, 60, 5), ctx.sma(bar.instrument, 60, 20)
        if fast != fast or slow != slow:
            return
        spec = ctx.spec(bar.instrument)
        kind_eq = spec is not None and spec["lot"] == 1
        product = "MIS" if kind_eq else "NRML"
        pos = ctx.position(bar.instrument, product)
        held = pos.qty if pos else 0
        if fast > slow and held == 0 and spec is not None:
            ctx.place(bar.instrument, "BUY", spec["lot"], product, "MARKET", tag="bench")
        elif fast < slow and held > 0:
            ctx.place(bar.instrument, "SELL", held, product, "MARKET", tag="bench")


def cpu_model() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except OSError:
        return platform.processor()


def machine() -> str:
    return (f"{cpu_model()}, {os.cpu_count()} logical cores; {platform.platform()}; Python "
            f"{platform.python_version()}; engine {engine.build_info()['engine_build_id']}")


def pack_file(sha: str) -> Path:
    found = sorted(paths.packs_dir().glob(f"{sha}*.evt"))
    if len(found) != 1:
        raise SystemExit(f"pack {sha!r}: {len(found)} matches in {paths.packs_dir()}")
    return found[0]


def resolve(pack: Path) -> tuple[dict, dict]:
    """The bundle for the pack (resolved as a run would be), and the pack's manifest."""
    manifest = json.loads(pack.with_suffix(".json").read_text(encoding="utf-8"))
    cfg = runconfig.load({"schema": "stockify.run/1", "name": "bench",
                          # resolved with a Python placeholder; each case below sets its own strategy
                          "strategy": {"kind": "python", "ref": "stockify.strategies.buy_and_hold:BuyAndHold"},
                          "data": {"packs": [{"sha256": manifest["sha256"]}]}, "capital_paise": 10**9,
                          "price_basis": "raw", "fill_model": {"kind": "snapshot_quote"},
                          "costs": {"allow_unverified": True}})
    return bundle_mod.resolve_bundle(cfg), manifest


def busiest(pack: Path, bundle: dict, n: int) -> list[int]:
    """The n tradable instruments with the most ticks in the pack."""
    from collections import Counter
    from stockify.data import packs as P
    _, recs = P.read(pack)
    ticks = Counter(r["instrument_id"] for r in recs if r["kind"] in ("TICK_FULL", "TICK_QUOTE"))
    tradable = {r["id"] for r in bundle["instruments"] if r["kind"] != "INDEX"}
    return [iid for iid, _ in ticks.most_common() if iid in tradable][:n]


def time_run(bundle_path: Path, pack: Path, strategy) -> tuple[float, int]:  # noqa: ANN001
    """Seconds spent in Engine.run(), and the number of orders the run placed."""
    mod = engine.load()
    e = mod.Engine(str(bundle_path))
    e.add_pack(str(pack))
    if isinstance(strategy, str):
        e.set_strategy_cpp(strategy)
    else:
        e.set_strategy_py(strategy)
    t0 = time.perf_counter()
    e.run()
    seconds = time.perf_counter() - t0
    orders = len(e.drain_outputs()["orders"])
    del e
    return seconds, orders


STOCKSIM_RUN = Path(__file__).resolve().parents[2] / "engine" / "build" / "win-msvc-release" / "stocksim_run.exe"


def time_stocksim_run(bundle_path: Path, out_dir: Path) -> tuple[float, float]:
    """(whole process seconds, the run's own seconds from its manifest) for one stocksim_run of noop_cpp."""
    t0 = time.perf_counter()
    proc = subprocess.run([str(STOCKSIM_RUN), "--bundle", str(bundle_path), "--out", str(out_dir),
                           "--lab", str(paths.lab_dir())], capture_output=True, text=True, timeout=600)
    whole = time.perf_counter() - t0
    if proc.returncode != 0:
        raise RuntimeError(f"stocksim_run failed ({proc.returncode}): {proc.stderr.strip()}")
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    return whole, manifest["duration_ms"] / 1000


def run(sha: str, reps: int = 3, out=sys.stdout) -> dict:  # noqa: ANN001
    pack = pack_file(sha)
    bundle, manifest = resolve(pack)
    records = manifest["records"]
    ten = busiest(pack, bundle, 10)
    cases = [("(a) noop_cpp, every instrument's ticks", "cpp", lambda: "noop_cpp"),
             ("(b) no-op Python strategy, every instrument's ticks", "py", NoopPy),
             ("(c) Python SMA crossover on minute bars, 10 instruments", "py", lambda: SmaOnTen(ten))]
    print(f"machine: {machine()}", file=out)
    print(f"pack {manifest['sha256'][:12]}: {manifest['spec'].get('dataset')} "
          f"{manifest['spec'].get('from')}..{manifest['spec'].get('to')}, {records:,} records, "
          f"{len(bundle['instruments'])} instruments; median of {reps} runs of Engine.run()", file=out)
    results = {}
    with tempfile.TemporaryDirectory(prefix="bench-") as tmp:
        bundle_path = Path(tmp) / "bundle.json"
        for label, kind, make in cases:
            b = dict(bundle)
            b["strategy"] = ({"kind": "cpp", "ref": "noop_cpp", "params": {}, "source_sha256": "0" * 64}
                             if kind == "cpp" else b["strategy"])
            bundle_mod.write(b, Path(tmp))
            times, orders = [], 0
            for _ in range(reps):
                s, orders = time_run(bundle_path, pack, make())
                times.append(s)
            med = statistics.median(times)
            results[label] = {"seconds": med, "events_per_s": records / med, "orders": orders,
                              "all_seconds": times}
            print(f"{label}: {med * 1000:,.1f} ms, {records / med:,.0f} events/s, {orders} orders "
                  f"(runs: {', '.join(f'{x * 1000:,.1f}' for x in times)} ms)", file=out, flush=True)
            if kind == "cpp" and STOCKSIM_RUN.exists():
                pairs = [time_stocksim_run(bundle_path, Path(tmp) / f"stocksim_run{i}") for i in range(reps)]
                whole = statistics.median(p[0] for p in pairs)
                inner = statistics.median(p[1] for p in pairs)
                label2 = "(a) noop_cpp through stocksim_run"
                results[label2] = {"seconds": whole, "run_seconds": inner, "events_per_s": records / max(inner, 1e-9)}
                print(f"{label2}: {whole * 1000:,.1f} ms for the whole process; the run itself "
                      f"{inner * 1000:,.0f} ms, {records / max(inner, 1e-9):,.0f} events/s", file=out, flush=True)
    return results
