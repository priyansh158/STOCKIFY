"""M4.13: the side-by-side strategy comparison (stockify/tools/compare_strategies.py) on chain test worlds.

The intraday runs use the two-day chain worlds of the SPAN-lite tests (quotes, quote fills, SPAN-lite margin). The
end-of-day run uses daily bars made from the same quotes (each option's close = its last mid that day, the index's
last value; published at 19:00 IST), with next-day-close fills: the same days, the same market.
"""
from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, golden, timeutil
from stockify import strategy as S
from stockify.strategies._margin_watch import LOG_PREFIX, MarginWatch
from stockify.tools import compare_strategies as C
from tests.stockify.test_spanlite_parity import spanlite_bundle, spanlite_world

STRADDLE = "stockify.strategies.short_straddle_stop:ShortStraddleStop"
FLY = "stockify.strategies.iron_fly:IronFly"
EOD = "stockify.strategies.eod_short_straddle:EodShortStraddle"
INTRADAY = {"underlying": "NIFTY", "entry_hhmm": "09:30", "exit_hhmm": "11:00", "lots": 1}


def eod_world(intraday: dict) -> dict:
    """Daily bars from the intraday world's quotes: each instrument's last mid (else LTP) per day; eod_close fills."""
    g = copy.deepcopy(intraday)
    last: dict[tuple[int, str], int] = {}
    for e in sorted(intraday["events"], key=lambda e: e["t"]):
        d = timeutil.ns_to_ist(e["t"]).date().isoformat()
        if e["kind"] == "INDEX":
            last[(1, d)] = e["value"]
        elif e["kind"] in ("TICK_FULL", "TICK_QUOTE"):
            bids, asks = e.get("bids") or [], e.get("asks") or []
            px = (bids[0][0] + asks[0][0]) // 2 // 5 * 5 if bids and asks else e["ltp"]
            last[(e["instrument"], d)] = max(5, px)
    g["events"] = []
    for (iid, d), px in sorted(last.items(), key=lambda kv: (kv[0][1], kv[0][0] == 1, kv[0][0])):
        g["events"].append({"kind": "BAR", "instrument": iid, "interval_s": 86400, "start": f"{d}T00:00:00+05:30",
                            "t": f"{d}T19:00:00+05:30", "o": px, "h": px, "l": px, "c": px, "v": 100_000})
    g["run"] = {**g["run"], "fill_model": {"kind": "eod_close", "half_spread_ticks": 1}}
    return g


def run_three(seed: int, folder: Path) -> list[tuple[str, Path, str]]:
    intraday = spanlite_world(seed, True)
    out = []
    for title, ref, params, role, world in (
            ("End-of-day straddle (NSE files)", EOD, {"underlying": "NIFTY", "index": "NSE:NIFTY 50", "min_days": 0,
                                                       "lots": 1}, "eod", eod_world(intraday)),
            ("Intraday straddle with stops", STRADDLE, INTRADAY, "straddle", intraday),
            ("Intraday iron fly", FLY, INTRADAY, "iron_fly", intraday)):
        sub = folder / role
        sub.mkdir()
        pack = golden.build_pack(world, sub)
        b = spanlite_bundle(world)
        # as a real launch through the tool would have it: the fill and margin labels, the wrapped strategy
        b["labels"] = [f"fill:{world['run']['fill_model']['kind']}", "margin:spanlite(approx)",
                       "margin_spanlite:placeholder"]
        b["strategy"] = {**b["strategy"], "kind": "python", "ref": C.WATCH_REF,
                         "params": {"inner_ref": ref, **params}}
        engine.run_bundle(b, [pack], sub / "run", strategy=MarginWatch(), report=False)
        out.append((title, sub / "run", role))
    return out


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class CompareTests(unittest.TestCase):
    def test_three_runs_side_by_side(self):
        with tempfile.TemporaryDirectory(prefix="compare-") as tmp:
            runs = run_three(4900, Path(tmp))
            text = C.compare(runs, "straddle_vs_ironfly_test")
            s = {role: C.summarise(t, f, role) for t, f, role in runs}
            for title, folder, role in runs:
                metrics = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
                self.assertIn(f"## {title}", text)
                self.assertEqual(s[role].made_paise, metrics["nav_end_paise"] - metrics["nav_start_paise"])
                self.assertIn(C._rupees(metrics["costs_total_paise"]), text)
                self.assertGreater(s[role].trades + len(list(csv.DictReader((folder / "fills.csv").open()))), 0)
            # the intraday margin peak comes from the margin watch lines, not the (flat) day closes
            log = (runs[2][1] / "events_log.txt").read_text(encoding="utf-8")
            peaks = [int(x.split(LOG_PREFIX)[1]) for x in log.splitlines() if LOG_PREFIX in x]
            self.assertEqual(s["iron_fly"].max_margin_paise, max(peaks))
            self.assertGreater(s["straddle"].max_margin_paise, s["iron_fly"].max_margin_paise)
            self.assertEqual([s[r].strategy for r in ("eod", "straddle", "iron_fly")],
                             ["EodShortStraddle", "ShortStraddleStop", "IronFly"])
            self.assertIn("**End-of-day vs intraday, same days:**", text)
            self.assertIn("**Iron fly vs straddle, margin:**", text)
            self.assertIn("less (", text)
            self.assertIn("| 2026-08-03 |", text)
            self.assertIn("| 2026-08-04 |", text)
            self.assertIn("SPAN-lite margin settings are placeholders", text)
            self.assertIn("fills at the next day's closing price", text)
            self.assertNotIn("$", text)
            self.assertIn("₹", text)

    def test_the_wrapper_changes_nothing_but_the_log(self):
        """MarginWatch only adds log lines: orders, fills, charges and equity equal the unwrapped run's."""
        with tempfile.TemporaryDirectory(prefix="compare-watch-") as tmp:
            folder = Path(tmp)
            g = spanlite_world(4901, True)
            pack = golden.build_pack(g, folder)
            b = spanlite_bundle(g)
            b["strategy"]["params"] = dict(INTRADAY)
            engine.run_bundle(b, [pack], folder / "plain", strategy=S.load(FLY), report=False)
            b["strategy"]["params"] = {"inner_ref": FLY, **INTRADAY}
            engine.run_bundle(b, [pack], folder / "watched", strategy=MarginWatch(), report=False)
            for name in ("orders.csv", "fills.csv", "charges.csv", "equity.csv", "positions_eod.csv"):
                self.assertEqual((folder / "plain" / name).read_bytes(), (folder / "watched" / name).read_bytes(), name)

    def test_config_checks_and_wrapping(self):
        good = {"schema": C.SCHEMA, "name": "x", "runs": [
            {"title": "a", "role": "eod", "config": {"strategy": {"kind": "python", "ref": EOD, "params": {"lots": 1}}}},
            {"title": "b", "config": {"strategy": {"kind": "cpp", "ref": "sma_cross_cpp"}}}]}
        self.assertEqual(C.load_config(good)["name"], "x")
        for bad, why in ((dict(good, schema="nope"), "schema"), (dict(good, name="../x"), "name"),
                         (dict(good, runs=good["runs"][:1]), "two"),
                         (dict(good, runs=[dict(good["runs"][0], role="who")] + good["runs"][1:]), "role")):
            with self.subTest(why=why), self.assertRaisesRegex(C.CompareError, why):
                C.load_config(bad)
        w = C.watched(good["runs"][0]["config"])
        self.assertEqual(w["strategy"]["ref"], C.WATCH_REF)
        self.assertEqual(w["strategy"]["params"], {"inner_ref": EOD, "lots": 1})
        self.assertEqual(C.watched(w), w)                                   # not wrapped twice
        self.assertEqual(C.watched(good["runs"][1]["config"]), good["runs"][1]["config"])  # C++ left alone
        self.assertNotIn("_margin_watch", {s["name"] for s in __import__(
            "stockify.strategies._catalogue", fromlist=["x"]).python_strategies()})

    def test_the_example_config_is_valid(self):
        from stockify import runconfig
        doc = C.load_config(Path(__file__).resolve().parents[2] / "examples" / "runs" /
                            "straddle_vs_ironfly_recorded.json")
        self.assertEqual([r.get("role") for r in doc["runs"]], ["eod", "straddle", "iron_fly"])
        for r in doc["runs"]:
            runconfig.load(r["config"])


if __name__ == "__main__":
    unittest.main()
