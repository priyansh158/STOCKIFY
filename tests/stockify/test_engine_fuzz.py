"""A3 P-04: differential fuzzing of the oracle against the engine (M3.12).

Each seed builds a random three-day world (bars, daily bars or ticks; an equity, a CAS stock, an index with
expiring futures and options, a stock future; gaps, price bands, settlement prices that sometimes never
arrive) and random run settings (fill model, slippage, latency, policies, mark, real charge rows). A random
trader, drawing only from ctx.rng() so both implementations make the same choices, places, modifies and
cancels orders with valid and invalid values. The run folders must agree (A4 §9.3). A failing seed is
reproduced with `fuzz_world(seed)`; the world, the trader and the shrinker live in
stockify/tools/fuzz.py (`python -m stockify tools fuzz` is the weekly 5,000-seed job).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden, timeutil
from stockify.tools.fuzz import RandomTrader, _real_charges, fuzz_world, run_both  # noqa: F401 — one copy, re-exported
from tests.stockify.test_engine_parity import assert_same_artifacts

SEEDS = range(4000, 4200)  # 200 seeds in the regular run (A3 P-04); the weekly job uses 5,000


def causality_problems(run_dir: Path, pack: Path) -> list[str]:
    """A3 P-07 (A4 §4.0), checked against the pack itself rather than either implementation: a bar fill's bar
    starts (effectively, at the continuous open) at or after the order's activation; a fill on snapshot
    evidence has its earlier snapshot s0 at or after it; a book fill has its snapshot s1 strictly after it."""
    import csv
    from collections import defaultdict
    from stockify.data import packs as P

    def ist_date(t: int) -> str:
        return timeutil.ns_to_ist(t).date().isoformat()

    bundle = json.loads((run_dir / "bundle.json").read_text(encoding="utf-8"))
    cont: dict[tuple[str, str], int] = {}
    for d in bundle["days"]:
        for g, phases in d["groups"].items():
            starts = [p["start_ns"] for p in phases if p["phase"] == "CONTINUOUS"]
            if starts:
                cont[(g, d["date"])] = min(starts)
    rows = {i["id"]: i for i in bundle["instruments"]}

    def group(iid: int, d: str) -> str:
        g = rows[iid]["session_group"]
        return ("CM_CAS" if d in rows[iid]["cas_dates"] else "CM") if g == "CM_CAS" else g

    _, recs = P.read(pack)
    bars: dict[tuple[int, int], list[dict]] = defaultdict(list)
    s0_times: dict[tuple[int, int], list[int | None]] = defaultdict(list)
    state: dict[int, tuple[str, dict | None]] = {}  # iid -> (day, last snapshot)
    for r in recs:
        k, iid, t = r["kind"], r["instrument_id"], r["t_event"]
        if k == "GAP":
            for j in ([iid] if r["scope"] == 1 else list(state)):
                if j in state:
                    state[j] = (state[j][0], None)
        elif k == "BAR":
            bars[(iid, t)].append(r)
        elif k in ("TICK_QUOTE", "TICK_FULL"):
            day, last = state.get(iid, (None, None))
            if day != ist_date(t):
                last = None
            s0_times[(iid, t)].append(last["t_event"] if last else None)
            state[iid] = (ist_date(t), r)
        elif k == "INDEX":
            day, last = state.get(iid, (None, None))
            state[iid] = (ist_date(t), None if day != ist_date(t) else last)
    with (run_dir / "orders.csv").open(encoding="utf-8") as fh:
        t_act = {int(o["order_id"]): int(o["t_activate_ns"]) for o in csv.DictReader(fh)}
    # orders.csv holds the final t_activate; a modify moves it to max(old, now + latency) (A4 §3.6). So the
    # final value is the one in force for fills after an order's last MODIFY; earlier fills are skipped.
    last_modify: dict[int, int] = {}
    with (run_dir / "order_events.csv").open(encoding="utf-8") as fh:
        for e in csv.DictReader(fh):
            if e["event"] == "MODIFY":
                last_modify[int(e["order_id"])] = int(e["t_ns"])
    problems = []
    with (run_dir / "fills.csv").open(encoding="utf-8") as fh:
        for f in csv.DictReader(fh):
            iid, t, ev, ta = int(f["instrument_id"]), int(f["t_ns"]), f["evidence"], t_act[int(f["order_id"])]
            if t <= last_modify.get(int(f["order_id"]), -1):
                continue
            if ev in ("bar_open", "bar_path", "eod_close"):
                b = min(bars[(iid, t)], key=lambda x: x["interval_s"])
                d = ist_date(b["start_ns"])
                eff = max(b["start_ns"], cont.get((group(iid, d), d), b["start_ns"]))
                ok = eff >= ta
            elif ev.startswith("book_lvl_") or ev == "book_cross":
                ok = t > ta
            else:  # vwap_int, ltp, trade_through
                ok = any(s0 is not None and s0 >= ta for s0 in s0_times[(iid, t)])
            if not ok:
                problems.append(f"fill {f['fill_id']} ({ev}) of order {f['order_id']} at {t}: t_activate {ta}")
    return problems


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class DifferentialFuzzTests(unittest.TestCase):
    def test_p04_oracle_and_engine_agree_on_random_worlds(self):
        for seed in SEEDS:
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="fuzz-") as tmp:
                folder = Path(tmp)
                oracle_error, engine_error = run_both(seed, folder)
                if oracle_error or engine_error:
                    self.assertTrue(oracle_error is not None and engine_error is not None,
                                    f"seed {seed}: oracle {oracle_error!r} / engine {engine_error!r}")
                    continue
                assert_same_artifacts(self, folder / "oracle", folder / "engine")
                self.assertEqual(causality_problems(folder / "engine", folder / f"FUZZ{seed}.evt"), [])

    def test_dt01_the_same_engine_run_twice_is_byte_identical(self):
        for seed in list(SEEDS)[:12]:
            g = fuzz_world(seed)
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix="dt01-") as tmp:
                folder = Path(tmp)
                pack = golden.build_pack(g, folder)
                bundle = golden.build_bundle(g)
                ids = [i["id"] for i in bundle["instruments"]]
                runs = []
                for n in (1, 2):
                    res = engine.run_bundle(bundle, [pack], folder / f"run{n}", strategy=RandomTrader(ids, 30),
                                            report=False)
                    runs.append((folder / f"run{n}" / "determinism.json").read_bytes())
                    self.assertEqual(len(res.hashes), 9)
                self.assertEqual(runs[0], runs[1])


if __name__ == "__main__":
    unittest.main()
