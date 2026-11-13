"""CH 6.24: the C++ strategy twins (orb_cpp, sma_cross_cpp) keep their in-day state across a restart, as their Python
originals do (A2 §4.1, A4 §1.10).

A paper session saves each strategy's state at day end and loads it the next morning instead of calling on_start.
So a twin that saves "{}" resumes with its parameters, range, position and `closed` flag gone. Here the same seeded
world is run day by day with a seeded mix of implementations (Python oracle or C++ engine, twin or original), each
day resumed from the previous day's saved state; every mix must write the same states and the same artifacts
(orders.csv with notes, fills.csv, plots.csv and the rest) as the all-Python run, byte for byte.
"""
from __future__ import annotations

import copy
import json
import random
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, engine, golden
from stockify import strategy as S
from stockify.oracle.runner import load_records
from tests.stockify.test_engine_sod_state import engine_run, split_pack
from tests.stockify.test_engine_strategies import REFS, daily_world, minute_world
from tests.stockify.test_sod_state import FILES, _run, day_bundles, slice_records

ARTIFACTS = FILES + ("plots.csv",)


def _inputs(g: dict, params: dict, folder: Path) -> tuple[dict, Path]:
    g = copy.deepcopy(g)
    g["run"]["seed_mode"] = "per_day"                              # paper sessions run per_day (A4 §4.5)
    bundle = golden.build_bundle(g)
    bundle["strategy"]["params"] = copy.deepcopy(params)
    return bundle, golden.build_pack(g, folder)


def _differs(a: Path, b: Path) -> str | None:
    for name in ARTIFACTS:
        x, y = (a / name).read_bytes(), (b / name).read_bytes()
        if x != y:
            la, lb = x.decode().splitlines(), y.decode().splitlines()
            for n in range(max(len(la), len(lb))):
                p, q = (la[n] if n < len(la) else "<end>"), (lb[n] if n < len(lb) else "<end>")
                if p != q:
                    return f"{name} line {n + 1}\n  a: {p}\n  b: {q}"
    return None


def run_mix(bundle: dict, pack: Path, ref: str, cpp: str, picks: list[str], folder: Path) -> list[str]:
    """Day by day, each day from the previous day's eod_state, on the implementation `picks[i]` names: "py" is the
    oracle with the Python strategy, "cpp" the engine with the twin, "engine_py" the engine with the Python one."""
    records = load_records([pack])
    parts = slice_records(records, bundle["days"])
    folder.mkdir(parents=True, exist_ok=True)
    packs = split_pack(pack, bundle["days"], folder)
    state, states = None, []
    for i, b in enumerate(day_bundles(bundle)):
        b["sod_state"] = json.loads(state) if state is not None else None
        out = folder / f"day{i}"
        if picks[i] == "py":
            out.mkdir(parents=True, exist_ok=True)
            state = _run(b, parts[i], S.load(ref), out).eod_state()
        elif picks[i] == "cpp":
            state = engine_run(b, packs[i], cpp, out)
        else:
            state = engine_run(b, packs[i], S.load(ref), out)
        states.append(state)
    return states


def check_world(case: unittest.TestCase, g: dict, params: dict, ref: str, cpp: str, rng: random.Random) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="strat-state-", dir=str(tests.support.SANDBOX_ROOT)) as tmp:
        folder = Path(tmp)
        bundle, pack = _inputs(g, params, folder)
        n = len(bundle["days"])
        base = run_mix(bundle, pack, ref, cpp, ["py"] * n, folder / "py")
        picks = [rng.choice(["py", "cpp", "cpp", "engine_py"]) for _ in range(n)]
        picks[rng.randrange(n)] = "cpp"                            # at least one resume on the twin
        mixed = run_mix(bundle, pack, ref, cpp, picks, folder / "mix")
        for i in range(n):
            case.assertEqual(base[i], mixed[i], (picks, i))        # the saved states are the same bytes
            case.assertIsNone(_differs(folder / "py" / f"day{i}", folder / "mix" / f"day{i}"), (picks, i))
        return [json.loads(s)["strategy"] for s in base]           # what each day saved


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class ResumeThroughTheTwins(unittest.TestCase):
    def test_orb_cpp_resumes_as_the_python_strategy_does(self):
        saved: list[dict] = []
        for seed in range(7300, 7340):
            g, params = minute_world(seed)
            params["short"] = seed % 2 == 0
            with self.subTest(seed=seed):
                saved += check_world(self, g, params, REFS["orb"], "orb_cpp", random.Random(seed))
        self.assertGreater(sum(1 for st in saved if st), 40)
        self.assertTrue(any(st.get("traded") for st in saved), "no saved day ended with a trade")
        self.assertTrue(any(st.get("stop_ids") for st in saved), "no saved day carried a stop")
        self.assertTrue(any(st.get("closed") for st in saved) and any(st.get("hi") is not None for st in saved))

    def test_sma_cross_cpp_resumes_as_the_python_strategy_does(self):
        saved: list[dict] = []
        for seed in range(7400, 7415):
            g, params = daily_world(seed, future=seed % 3 == 0)
            with self.subTest(seed=seed):
                saved += check_world(self, g, params, REFS["sma_cross"], "sma_cross_cpp", random.Random(seed))
        self.assertGreater(sum(1 for st in saved if st), 15)

    def test_the_twin_state_is_the_python_state_with_the_same_keys(self):
        for ref, cpp, make in ((REFS["orb"], "orb_cpp", minute_world), (REFS["sma_cross"], "sma_cross_cpp", daily_world)):
            g, params = next(make(s) for s in range(7500, 7600) if len(make(s)[0]["bundle"]["dates"]) > 1)
            with tempfile.TemporaryDirectory(prefix="strat-keys-", dir=str(tests.support.SANDBOX_ROOT)) as tmp:
                bundle, pack = _inputs(g, params, Path(tmp))
                want = json.loads(run_mix(bundle, pack, ref, cpp, ["py"] * len(bundle["days"]), Path(tmp) / "a")[0])["strategy"]
                got = json.loads(run_mix(bundle, pack, ref, cpp, ["cpp"] * len(bundle["days"]), Path(tmp) / "b")[0])["strategy"]
                self.assertEqual(got, want)
                self.assertIn("symbol", got)


def _first_day_state(make, ref: str, cpp: str, folder: Path) -> tuple[dict, dict, Path, list[dict]]:
    """A multi-day world's second-day bundle, the first day's saved state (from the twin), and the day packs."""
    g, params = next(make(s) for s in range(7500, 7700) if len(make(s)[0]["bundle"]["dates"]) > 1)
    bundle, pack = _inputs(g, params, folder)
    packs = split_pack(pack, bundle["days"], folder)
    days = day_bundles(bundle)
    state = json.loads(engine_run(days[0], packs[0], cpp, folder / "d0"))
    return state, days[1], packs[1], days


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class StateIsReadChecked(unittest.TestCase):
    def _load(self, make, cpp: str, edit) -> None:  # noqa: ANN001
        with tempfile.TemporaryDirectory(prefix="strat-hostile-", dir=str(tests.support.SANDBOX_ROOT)) as tmp:
            folder = Path(tmp)
            state, day1, _, _ = _first_day_state(make, "", cpp, folder)
            strategy = edit(copy.deepcopy(state["strategy"]))
            state["strategy"] = strategy
            (folder / "d1").mkdir()
            bundle_mod.write(json.loads(bundle_mod.dumps(day1)), folder / "d1")
            e = engine.load().Engine(str(folder / "d1" / "bundle.json"))
            e.set_strategy_cpp(cpp)
            e.load_sod_state(json.dumps(state))
            self.assertEqual(json.loads(e.eod_state())["strategy"], strategy)   # what it read it writes back

    def _refuses(self, make, cpp: str, edit, label: str) -> None:  # noqa: ANN001
        with self.subTest(label):
            with self.assertRaises(RuntimeError):
                self._load(make, cpp, edit)

    def test_a_state_written_before_this_change_still_loads(self):
        for make, cpp in ((minute_world, "orb_cpp"), (daily_world, "sma_cross_cpp")):
            with self.subTest(cpp):
                self._load(make, cpp, lambda s: {})

    def test_a_legacy_empty_state_resumes_and_trades_as_the_saved_state_does(self):
        """A session saved before the twins kept any state resumes the twin from "{}": it must configure itself from
        the run's params and trade the day exactly as a resume from the real state does."""
        for make, cpp, seeds in ((minute_world, "orb_cpp", range(7500, 7540)), (daily_world, "sma_cross_cpp", range(7500, 7510))):
            traded = 0
            for seed in seeds:
                g, params = make(seed)
                if len(g["bundle"]["dates"]) < 2:
                    continue
                with self.subTest(cpp=cpp, seed=seed), tempfile.TemporaryDirectory(
                        prefix="strat-legacy-", dir=str(tests.support.SANDBOX_ROOT)) as tmp:
                    folder = Path(tmp)
                    bundle, pack = _inputs(g, params, folder)
                    packs = split_pack(pack, bundle["days"], folder)
                    days = day_bundles(bundle)
                    last = len(days) - 1                      # resume on the last day, after the warm-up days
                    text = None
                    for i in range(last):
                        day = dict(days[i], sod_state=json.loads(text) if text else None)
                        text = engine_run(day, packs[i], cpp, folder / f"d{i}")
                    state = json.loads(text)
                    real = dict(days[last], sod_state=copy.deepcopy(state))
                    legacy_state = copy.deepcopy(state)
                    legacy_state["strategy"] = {}
                    legacy = dict(days[last], sod_state=legacy_state)
                    engine_run(real, packs[last], cpp, folder / "real")
                    engine_run(legacy, packs[last], cpp, folder / "legacy")
                    self.assertIsNone(_differs(folder / "real", folder / "legacy"))
                    traded += len((folder / "legacy" / "fills.csv").read_text(encoding="utf-8").splitlines()) - 1
            self.assertGreater(traded, 0, cpp)

    def test_orb_cpp_refuses_a_hostile_state(self):
        def setkey(k, v):  # noqa: ANN001, ANN202
            def edit(s):  # noqa: ANN001, ANN202
                s[k] = v
                return s
            return edit

        def drop(k):  # noqa: ANN001, ANN202
            def edit(s):  # noqa: ANN001, ANN202
                del s[k]
                return s
            return edit
        self._load(minute_world, "orb_cpp", lambda s: s)            # the unedited state loads
        cases = {
            "iid as text": setkey("iid", "1"), "iid float": setkey("iid", 1.5), "iid negative": setkey("iid", -1),
            "iid past uint32": setkey("iid", 2**32), "qty huge": setkey("qty", 2**63), "qty bool": setkey("qty", True),
            "minutes text": setkey("minutes", "15"), "short as int": setkey("short", 1),
            "symbol number": setkey("symbol", 5), "exit_hhmm null": setkey("exit_hhmm", None),
            "day not a date": setkey("day", "2026-13-01"), "day compact": setkey("day", "20260302"),
            "day number": setkey("day", 20260302), "side unknown": setkey("stop_side", "HOLD"),
            "stop_ids text": setkey("stop_ids", ["x"]), "stop_ids negative": setkey("stop_ids", [-1]),
            "stop_ids huge": setkey("stop_ids", [2**63]), "stop_ids not a list": setkey("stop_ids", 3),
            "entry without a side": lambda s: {**s, "entry_id": 1, "stop_side": None},
            "hi without lo": lambda s: {**s, "hi": 100, "lo": None},
            "lo without hi": lambda s: {**s, "hi": None, "lo": 100},
            "hi float": setkey("hi", 1.5), "open_ns huge": setkey("open_ns", 2**63), "traded as text": setkey("traded", "yes"),
            "closed null": setkey("closed", None), "missing qty": drop("qty"), "missing day": drop("day"),
            "missing range key": drop("hi"), "unknown key": setkey("extra", 1),
            "range keys without a day": lambda s: {**s, "day": None},
            "stop_trigger text": setkey("stop_trigger", "5"), "stop_trigger float": setkey("stop_trigger", 1.5),
            "stop_trigger huge": setkey("stop_trigger", 2**63), "entry_id text": lambda s: {**s, "entry_id": "7"},
            "entry_id negative": lambda s: {**s, "entry_id": -1},
            "stops without a trade": lambda s: {**s, "traded": False, "entry_id": None, "stop_side": None,
                                                "stop_ids": [3]},
            "missing symbol": drop("symbol"), "missing iid": drop("iid"), "missing stop_ids": drop("stop_ids"),
        }
        for label, edit in cases.items():
            self._refuses(minute_world, "orb_cpp", edit, label)

    def test_orb_cpp_reads_a_state_with_no_day_yet(self):
        def before_the_first_bar(s):  # noqa: ANN001, ANN202
            keep = ("symbol", "iid", "minutes", "qty", "short", "exit_hhmm")
            return {**{k: s[k] for k in keep}, "day": None}
        self._load(minute_world, "orb_cpp", before_the_first_bar)

    def test_sma_cross_cpp_refuses_a_hostile_state(self):
        def setkey(k, v):  # noqa: ANN001, ANN202
            def edit(s):  # noqa: ANN001, ANN202
                s[k] = v
                return s
            return edit
        self._load(daily_world, "sma_cross_cpp", lambda s: s)
        cases = {
            "isec zero": setkey("isec", 0), "isec past int32": setkey("isec", 2**31), "isec text": setkey("isec", "60"),
            "fast float": setkey("fast", 2.0), "slow huge": setkey("slow", 2**63), "slow null": setkey("slow", None),
            "iid negative": setkey("iid", -1), "long_short int": setkey("long_short", 1),
            "product number": setkey("product", 3), "symbol null": setkey("symbol", None),
            "short on CNC": lambda s: {**s, "long_short": True, "product": "CNC"},
            "missing fast": lambda s: {k: v for k, v in s.items() if k != "fast"},
            "missing symbol": lambda s: {k: v for k, v in s.items() if k != "symbol"},
            "missing iid": lambda s: {k: v for k, v in s.items() if k != "iid"},
            "unknown key": setkey("hi", 1),
        }
        for label, edit in cases.items():
            self._refuses(daily_world, "sma_cross_cpp", edit, label)


if __name__ == "__main__":
    unittest.main()
