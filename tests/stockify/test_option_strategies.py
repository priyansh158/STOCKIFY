"""M4.13 (IF 4.1): the intraday option-selling reference strategies, short_straddle_stop and iron_fly.

On the chain worlds of test_chain_view (a NIFTY chain quoted through the morning, with gaps and stale stretches),
with the quote-based fill model and the flat T0 margin: oracle and engine give identical run folders (DT-02), and
the mechanics hold (wings before the short legs, each stop at the entry price + stop_pct rounded up to the tick,
the time exit closes what is left, no entry without fresh two-sided quotes)."""
from __future__ import annotations

import csv
import random
import tempfile
import unittest
from collections import Counter
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden, strategy as S
from stockify.oracle import run_bundle as oracle_run
from tests.stockify.test_chain_view import chain_bundle, chain_world
from tests.stockify.test_engine_parity import assert_same_artifacts

REFS = {"straddle": "stockify.strategies.short_straddle_stop:ShortStraddleStop",
        "fly": "stockify.strategies.iron_fly:IronFly"}


def _params(rng: random.Random, fly: bool) -> dict:
    p = {"entry_hhmm": rng.choice(["09:20", "09:30", "09:41"]), "exit_hhmm": rng.choice(["10:30", "11:15", "12:05"]),
         "stop_pct": rng.choice(["0.02", "0.05", "0.1", "0.3"]), "lots": rng.choice([1, 2])}
    if fly:
        p["wing_strikes"] = rng.choice([1, 2, 3])
    return p


def _world(seed: int):  # noqa: ANN202
    g = chain_world(seed, with_future=seed % 2 == 0)
    g["run"]["capital_paise"] = 10**9                       # ₹1 crore: the flat margin on two short legs fits
    return g


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _run(g: dict, params: dict, ref: str, folder: Path, impl: str) -> Path:
    pack = golden.build_pack(g, folder)
    b = chain_bundle(g)
    b["strategy"]["params"] = params
    out = folder / impl
    (engine.run_bundle if impl == "engine" else oracle_run)(b, [pack], out, strategy=S.load(ref), report=False)
    return out


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class ParityTests(unittest.TestCase):
    def test_both_strategies_give_identical_run_folders(self):
        tally = Counter()
        for seed in range(6100, 6130):
            rng = random.Random(seed)
            for key in ("straddle", "fly"):
                params = _params(rng, key == "fly")
                with self.subTest(seed=seed, strategy=key), tempfile.TemporaryDirectory(prefix="ifly-") as tmp:
                    folder = Path(tmp)
                    g = _world(seed)
                    o = _run(g, params, REFS[key], folder, "oracle")
                    e = _run(g, params, REFS[key], folder, "engine")
                    assert_same_artifacts(self, o, e)
                    for r in _rows(o / "orders.csv"):
                        tally[f"{key} {r['tag']} {r['status']}"] += 1
                    log = (o / "events_log.txt").read_text(encoding="utf-8")
                    tally[f"{key} skipped"] += log.count(" skip: ")
        # the worlds exercise every path: entries, stops that fire and ones cancelled, time exits, skips
        for key in ("straddle", "fly"):
            self.assertGreater(tally[f"{key} entry COMPLETE"], 10, tally)
            self.assertGreater(tally[f"{key} stop COMPLETE"], 0, tally)
            self.assertGreater(tally[f"{key} stop CANCELLED"], 0, tally)
            self.assertGreater(tally[f"{key} time_exit COMPLETE"], 0, tally)
        self.assertGreater(tally["fly wing COMPLETE"], 10, tally)
        self.assertGreater(tally["straddle skipped"] + tally["fly skipped"], 0, tally)


class MechanicsTests(unittest.TestCase):
    def _orders(self, seed: int, ref: str, params: dict) -> list[dict]:
        with tempfile.TemporaryDirectory(prefix="ifly-") as tmp:
            out = _run(_world(seed), params, ref, Path(tmp), "oracle")
            return _rows(out / "orders.csv")

    def _entered_seed(self, ref: str, params: dict) -> tuple[int, list[dict]]:
        for seed in range(6100, 6160):
            orders = self._orders(seed, ref, params)
            if any(o["tag"] == "entry" and o["status"] == "COMPLETE" for o in orders):
                return seed, orders
        self.fail("no world entered")

    def test_stops_sit_at_entry_plus_pct_rounded_up(self):
        params = {"entry_hhmm": "09:30", "exit_hhmm": "11:00", "stop_pct": "0.07", "lots": 1}
        _, orders = self._entered_seed(REFS["straddle"], params)
        by_id = {o["order_id"]: o for o in orders}
        entries = [o for o in orders if o["tag"] == "entry"]
        stops = [o for o in orders if o["tag"] == "stop"]
        self.assertEqual(len(entries), 2)
        self.assertEqual({o["side"] for o in entries}, {"SELL"})
        filled = [o for o in entries if int(o["filled_qty"])]
        self.assertEqual(len(stops), len(filled))
        for stop in stops:
            entry = next(o for o in filled if o["instrument_id"] == stop["instrument_id"])
            raw = Decimal(int(entry["avg_price_paise"])) * Decimal("1.07")
            expected = int((raw / 5).to_integral_value(rounding=ROUND_CEILING)) * 5
            self.assertEqual((stop["side"], stop["type"], int(stop["trigger_paise"]), stop["qty"]),
                             ("BUY", "SL-M", expected, entry["filled_qty"]))
            self.assertGreater(int(stop["order_id"]), int(entry["order_id"]))
        self.assertTrue(by_id)

    def test_the_iron_fly_buys_its_wings_first(self):
        params = {"entry_hhmm": "09:30", "exit_hhmm": "11:00", "stop_pct": "0.3", "wing_strikes": 2}
        _, orders = self._entered_seed(REFS["fly"], params)
        first_four = [(o["tag"], o["side"]) for o in sorted(orders, key=lambda o: int(o["order_id"]))[:4]]
        self.assertEqual(first_four, [("wing", "BUY"), ("wing", "BUY"), ("entry", "SELL"), ("entry", "SELL")])

    def test_no_entry_without_fresh_two_sided_quotes(self):
        params = {"entry_hhmm": "09:30", "exit_hhmm": "11:00", "stop_pct": "0.3"}
        g = _world(6100)
        g["events"] = [e for e in g["events"] if e.get("kind") != "TICK_FULL"]       # quotes without depth only
        with tempfile.TemporaryDirectory(prefix="ifly-") as tmp:
            out = _run(g, params, REFS["straddle"], Path(tmp), "oracle")
            self.assertEqual(_rows(out / "orders.csv"), [])
            self.assertIn("skip:", (out / "events_log.txt").read_text(encoding="utf-8"))

    def test_bad_params_are_refused(self):
        for ref, bad in ((REFS["straddle"], {"stop_pct": "0"}), (REFS["fly"], {"wing_strikes": 0})):
            with self.subTest(ref=ref), tempfile.TemporaryDirectory(prefix="ifly-") as tmp:
                with self.assertRaises(ValueError):
                    _run(_world(6100), bad, ref, Path(tmp), "oracle")


if __name__ == "__main__":
    unittest.main()
