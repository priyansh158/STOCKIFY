"""D-70 (A4 §3.11): pausing a strategy is a journaled input, the same on the oracle and on the engine.

A `strategy_control` REFDATA record (instrument 0) switches a strategy off and on. While it is off no new hook runs
(each is dropped with a log line), `on_fill` / `on_order_update` are held and delivered in their original order at the
enable, a one-shot timer due meanwhile is dropped, a periodic one loses only the due occurrences, and matching, fills,
charges and settlement go on. The flag, its reasons and the held notifications are part of the day-to-day state, so a
pause carries across a day boundary: a run paused at one day's end and resumed on the next equals the whole run.
Skipped when the engine is not built.
"""
from __future__ import annotations

import copy
import json
import struct
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden
from stockify.data import packs
from stockify.oracle import run_bundle as oracle_run
from stockify.oracle.runner import Engine as OracleEngine, RunError, load_records
from stockify.strategy import Strategy
from tests.stockify.test_engine_parity import assert_same_artifacts
from tests.stockify.test_engine_sod_state import _world_inputs, engine_day_by_day, engine_whole
from tests.stockify.test_engine_strategies import daily_world
from tests.stockify.test_sod_state import first_difference, run_day_by_day, run_whole

H = 3600 * 10**9


class Probe(Strategy):
    """Logs every hook as `H <hook> ...`; buys one unit on the first bar; a 6-hourly timer and two one-shot timers."""

    def __init__(self, one_shot_in_pause: int = 0, one_shot_after: int = 0):
        self.one_shot_in_pause, self.one_shot_after = one_shot_in_pause, one_shot_after
        self.ordered = False
        self.iid = 0

    def on_start(self, ctx):  # noqa: ANN001
        self.iid = ctx.resolve("NSE:TEST")
        ctx.subscribe_bars(self.iid, 86400)
        ctx.schedule_every(6 * H, ctx.now() + H)
        if self.one_shot_in_pause:
            ctx.schedule_at(self.one_shot_in_pause)
        if self.one_shot_after:
            ctx.schedule_at(self.one_shot_after)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        ctx.log("info", f"H on_bar {bar.start_ns}")
        if not self.ordered:
            self.ordered = True
            ctx.place(self.iid, "BUY", 1, "CNC", "MARKET")

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        ctx.log("info", f"H on_timer {timer_id}")

    def on_session_phase(self, ctx, change):  # noqa: ANN001
        ctx.log("info", f"H on_session_phase {change.group} {change.phase}")

    def on_session_end(self, ctx):  # noqa: ANN001
        ctx.log("info", "H on_session_end")

    def on_fill(self, ctx, fill):  # noqa: ANN001
        ctx.log("info", f"H on_fill {fill.order_id}")

    def on_order_update(self, ctx, order):  # noqa: ANN001
        ctx.log("info", f"H on_order_update {order.order_id} {order.status}")

    def save_state(self) -> dict:
        return {"ordered": self.ordered, "iid": self.iid}

    def load_state(self, state: dict) -> None:
        self.ordered, self.iid = state["ordered"], state["iid"]


def ist(day: str, hms: str) -> str:
    return f"{day}T{hms}+05:30"


def control(t: str, enabled: bool, reasons=()) -> dict:  # noqa: ANN001
    return {"kind": "STRATEGY_CONTROL", "t": t, "enabled": enabled, "reasons": list(reasons)}


def world_with(events: list[dict], seed: int = 5700) -> dict:
    g, _ = daily_world(seed)
    g["events"] = g["events"] + events
    g["run"]["seed_mode"] = "per_day"
    return g


def log_lines(folder: Path) -> list[tuple[int, str, str]]:
    out = []
    for line in (folder / "events_log.txt").read_text(encoding="utf-8").splitlines():
        t, level, msg = line.split(" ", 2)
        out.append((int(t), level, msg))
    return out


def hooks(folder: Path) -> list[tuple[int, str]]:
    return [(t, m[2:]) for t, _, m in log_lines(folder) if m.startswith("H ")]


class Both:
    """One world run on the oracle and on the engine into two folders."""

    def __init__(self, case: unittest.TestCase, events: list[dict], make=Probe, seed: int = 5700):  # noqa: ANN001
        tmp = tempfile.TemporaryDirectory(prefix="strategy-control-")
        case.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.world = world_with(events, seed)
        self.bundle, self.pack = _world_inputs(self.world, self.folder)
        self.make = make
        self.case = case
        self.dirs: dict[str, Path] = {}
        for impl in ("oracle", "engine"):
            out = self.folder / impl
            go = oracle_run if impl == "oracle" else engine.run_bundle
            go(self.bundle, [self.pack], out, strategy=make(), report=False)
            self.dirs[impl] = out
        assert_same_artifacts(case, self.dirs["oracle"], self.dirs["engine"])

    def lines(self) -> list[tuple[int, str, str]]:
        return log_lines(self.dirs["oracle"])


PAUSE = [control(ist("2026-03-03", "06:30:07"), False, ["manual"]), control(ist("2026-03-05", "06:30:07"), True)]
T1, T2 = golden.ts(ist("2026-03-03", "06:30:07")), golden.ts(ist("2026-03-05", "06:30:07"))


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class StrategyControlTests(unittest.TestCase):
    def test_no_new_hook_runs_while_disabled_but_resting_orders_fill(self):
        run = Both(self, PAUSE)
        inside = [(t, h) for t, h in hooks(run.dirs["oracle"]) if T1 <= t < T2]
        # Only the held notifications come at the enable instant: nothing from the paused stretch before it.
        self.assertEqual([x for x in inside if x[0] != T2 and not x[1].startswith(("on_fill", "on_order_update"))], [])
        skipped = {m for t, _, m in run.lines() if T1 <= t < T2 and "skipped" in m}
        self.assertEqual({m.split(" ")[0] for m in skipped},
                         {"on_bar", "on_timer", "on_session_phase", "on_session_end"})
        self.assertIn("on_bar skipped: strategy disabled (manual)", skipped)
        fills = (run.dirs["oracle"] / "fills.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(fills), 2)                                    # the fill happened inside the pause
        # hooks run again after the pause
        self.assertTrue(any(t > T2 and h.startswith("on_bar") for t, h in hooks(run.dirs["oracle"])))

    def test_reasons_are_listed_sorted_in_the_skip_line(self):
        run = Both(self, [control(ist("2026-03-03", "06:30:07"), False, ["manual", "rms"]),
                          control(ist("2026-03-05", "06:30:07"), True)])
        self.assertIn("on_bar skipped: strategy disabled (manual, rms)", {m for _, _, m in run.lines()})

    def test_missed_fill_and_order_update_delivered_in_order_on_enable(self):
        # paused after the order is live (09:15 day 1) and resumed on day 3: only the fill's notifications are held
        late = [control(ist("2026-03-03", "16:00:07"), False, ["manual"]), control(ist("2026-03-05", "06:30:07"), True)]
        run, plain = Both(self, late), Both(self, [])
        t_fill = next(t for t, h in hooks(plain.dirs["oracle"]) if h.startswith("on_fill"))
        expected = [h for t, h in hooks(plain.dirs["oracle"])
                    if t == t_fill and h.startswith(("on_order_update", "on_fill"))]   # what it would have seen
        self.assertEqual(expected[:2], ["on_order_update 1 COMPLETE", "on_fill 1"])
        for impl in ("oracle", "engine"):
            at_enable = [h for t, h in hooks(run.dirs[impl]) if t == T2]
            self.assertEqual(at_enable, expected, impl)                           # same notifications, same order
        # and before any other hook: nothing else is logged between the enable and the last held notification
        lines = [m for t, _, m in run.lines() if t == T2]
        self.assertEqual(lines[0], "strategy enabled")
        self.assertEqual([m[2:] for m in lines[1:1 + len(expected)]], expected)

    def test_a_pause_over_the_orders_whole_life_holds_every_update_in_order(self):
        run, plain = Both(self, PAUSE), Both(self, [])
        plain_hooks = hooks(plain.dirs["oracle"])
        want = [h for t, h in plain_hooks if T1 <= t < T2 and h.startswith(("on_order_update", "on_fill"))]
        self.assertGreaterEqual(len(want), 3)
        for impl in ("oracle", "engine"):
            self.assertEqual([h for t, h in hooks(run.dirs[impl]) if t == T2][:len(want)], want, impl)

    def test_periodic_timer_loses_only_due_occurrences(self):
        in_pause = golden.ts(ist("2026-03-04", "12:00:00"))
        after = golden.ts(ist("2026-03-06", "12:00:00"))
        run = Both(self, PAUSE, make=lambda: Probe(in_pause, after))
        plain = Both(self, [], make=lambda: Probe(in_pause, after))

        def fires(r: Both) -> list[tuple[int, str]]:
            return [(t, h) for t, h in hooks(r.dirs["oracle"]) if h.startswith("on_timer")]
        full, paused = fires(plain), fires(run)
        self.assertTrue(any(T1 <= t < T2 for t, _ in full))
        self.assertEqual(paused, [x for x in full if not T1 <= x[0] < T2])    # the period is kept
        self.assertIn(in_pause, [t for t, _ in full])
        self.assertNotIn(in_pause, [t for t, _ in paused])                    # the one-shot is gone, not delayed
        self.assertIn(after, [t for t, _ in paused])                          # a later one-shot still fires

    def test_pause_carries_across_a_day_boundary(self):
        # disabled at 15:00 on day 1, resumed on day 3: day 2 (from day 1's eod_state) runs no hook at all
        events = [control(ist("2026-03-03", "15:00:00"), False, ["manual"]),
                  control(ist("2026-03-05", "06:30:07"), True)]
        world = world_with(events)
        with tempfile.TemporaryDirectory(prefix="strategy-control-") as tmp:
            folder = Path(tmp)
            bundle, pack = _world_inputs(world, folder)
            records = load_records([pack])
            states = run_day_by_day(bundle, records, Probe, folder / "o")
            day = {d["date"]: i for i, d in enumerate(bundle["days"])}
            st = json.loads(states[day["2026-03-03"]])
            self.assertEqual((st["strategy_enabled"], st["pause_reasons"]), (False, ["manual"]))
            st = json.loads(states[day["2026-03-04"]])
            self.assertEqual((st["strategy_enabled"], st["pause_reasons"]), (False, ["manual"]))
            self.assertEqual([n["kind"] for n in st["queued_notifications"]].count("fill"), 1)   # held across the night
            self.assertTrue(json.loads(states[day["2026-03-05"]])["strategy_enabled"])
            self.assertEqual(json.loads(states[day["2026-03-05"]])["queued_notifications"], [])
            day2 = [h for t, h in hooks(folder / "o" / f"day{day['2026-03-04']}")]
            self.assertEqual(day2, [])
            day2_log = log_lines(folder / "o" / f"day{day['2026-03-04']}")
            self.assertTrue(any("on_bar skipped: strategy disabled (manual)" in m for _, _, m in day2_log))

    def test_split_equals_whole_when_a_pause_spans_a_day_boundary(self):
        # M-8 (Task 5 review): paused at day N's end, resumed on day N+1, equals the whole run, on both runners
        for events in (
            [control(ist("2026-03-03", "15:00:00"), False, ["manual"]), control(ist("2026-03-05", "06:30:07"), True)],
            [control(ist("2026-03-03", "15:00:00"), False, ["manual"]), control(ist("2026-03-04", "10:00:00"), True),
             control(ist("2026-03-04", "10:30:00"), False, ["a", "b"]), control(ist("2026-03-06", "07:00:00"), True)],
            [control(ist("2026-03-03", "15:00:00"), False, ["manual"])],           # never resumed
        ):
            with self.subTest(events=len(events)), tempfile.TemporaryDirectory(prefix="strategy-control-") as tmp:
                folder = Path(tmp)
                bundle, pack = _world_inputs(world_with(events), folder)
                records = load_records([pack])
                run_whole(bundle, records, Probe, folder / "ow")
                o_states = run_day_by_day(bundle, records, Probe, folder / "od")
                self.assertIsNone(first_difference(folder / "ow" / "whole", folder / "od" / "parts"))
                engine_whole(bundle, pack, Probe, folder / "ew")
                e_states = engine_day_by_day(bundle, pack, Probe, folder / "ed")
                self.assertEqual(o_states, e_states)
                self.assertIsNone(first_difference(folder / "ew" / "whole", folder / "ed" / "parts"))
                self.assertIsNone(first_difference(folder / "ow" / "whole", folder / "ew" / "whole"))

    def test_unknown_subtype_still_raises(self):
        with self.assertRaises(KeyError):
            packs.enc_refdata(0, 1, "no_such_subtype", {})
        with tempfile.TemporaryDirectory(prefix="strategy-control-") as tmp:
            bundle, pack = _world_inputs(world_with(PAUSE), Path(tmp))
            data = bytearray(pack.read_bytes())
            off = 64
            while off < len(data):                                     # the first REFDATA record: subtype 5 -> 6
                kind, size = data[off], struct.unpack_from("<H", data, off + 2)[0]
                if kind == packs.REFDATA_K:
                    struct.pack_into("<H", data, off + 24, 6)
                    break
                off += size
            else:
                self.fail("no REFDATA record in the pack")
            bad = Path(tmp) / "bad.evt"
            bad.write_bytes(bytes(data))
            with self.assertRaises(Exception):
                packs.read(bad)
            with self.assertRaises(Exception) as cm:
                engine.run_bundle(bundle, [bad], Path(tmp) / "out", strategy=Probe(), report=False)
            self.assertIn("subtype", str(cm.exception))

    def test_bad_payloads_are_refused_in_the_same_words(self):
        cases = {
            "strategy_control: payload must be an object": [],
            "strategy_control: payload must have exactly the keys 'enabled' and 'reasons'": {"enabled": True},
            "strategy_control: enabled must be a boolean": {"enabled": 1, "reasons": []},
            "strategy_control: reasons must be a list of strings": {"enabled": False, "reasons": [3]},
            "strategy_control: a reason must not be empty": {"enabled": False, "reasons": [""]},
            "strategy_control: reasons must be sorted and unique": {"enabled": False, "reasons": ["b", "a"]},
            "strategy_control: reasons must be empty when enabled": {"enabled": True, "reasons": ["a"]},
            "strategy_control: reasons must not be empty when disabled": {"enabled": False, "reasons": []},
        }
        for why, payload in cases.items():
            with self.subTest(why=why), tempfile.TemporaryDirectory(prefix="strategy-control-") as tmp:
                folder = Path(tmp)
                bundle, pack = _world_inputs(world_with([]), folder)
                recs = [r for _, r in load_records([pack])]
                t = recs[0]["t_event"]
                extra = packs.enc_refdata(0, t, "strategy_control", payload)
                bad = folder / "bad.evt"
                _write_pack_with(pack, extra, bad)
                with self.assertRaises(Exception) as o:
                    oracle_run(bundle, [bad], folder / "o", strategy=Probe(), report=False)
                with self.assertRaises(Exception) as e:
                    engine.run_bundle(bundle, [bad], folder / "e", strategy=Probe(), report=False)
                self.assertIn(why, str(o.exception))
                self.assertIn(why, str(e.exception))


def _write_pack_with(pack: Path, extra: bytes, out: Path) -> None:
    """The pack with one more record, sorted in by (t_event, rank), seq renumbered."""
    data = pack.read_bytes()
    header = packs.HEADER.unpack_from(data, 0)
    recs, off = [], 64
    while off < len(data):
        size = struct.unpack_from("<H", data, off + 2)[0]
        recs.append(data[off:off + size])
        off += size
    recs.append(extra)
    recs.sort(key=lambda r: (struct.unpack_from("<q", r, 8)[0], packs.RANK[r[0]]))
    ts = [struct.unpack_from("<q", r, 8)[0] for r in recs]
    body = b"".join(packs._with_seq(r, n) for n, r in enumerate(recs, start=1))
    out.write_bytes(packs.HEADER.pack(header[0], header[1], header[2], 64, len(recs), min(ts), max(ts), header[7]) + body)


if __name__ == "__main__":
    unittest.main()
