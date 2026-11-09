"""MEZ 4.10: exact SPAN and the margin staircase on the oracle and the engine, with byte-identical artifacts.

The SPAN sections are cut from the trimmed real 2026-11-09 i1 file (tests/fixtures/span) by
`stockify.tools.fuzz.fixture_span_entry`, its dates moved onto the chain world's expiry. The worlds are the
SPAN-lite parity worlds (test_spanlite_parity: two days, the second the options' expiry day) plus a far future, so
calendar spreads form; a trader sells straddles, buys wings (flies), buys single options, trades both futures
(a calendar) and closes legs, logging funds after every order, so the margin is compared all day long.

`tests/fixtures/span/span_vectors.json` holds the oracle's exact SPAN of chosen books on the fixture's commodities;
the engine's doctest (engine/tests/test_span_exact.cpp) checks itself against it, and the first test here checks
the file is still what the oracle computes (regenerate with `python -m tests.stockify.test_span_parity --write`).
"""
from __future__ import annotations

import copy
import csv
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod
from stockify import engine, golden, timeutil
from stockify.fno.span_exact import SpanCalcError, span_paise, span_requirement
from stockify.fno.span_file import parse_span
from stockify.fno.staircase import STEPS, commodity_from_bundle
from stockify.oracle import run_bundle as oracle_run
from stockify.tools.fuzz import FIXTURE_SPAN, fixture_elm, fixture_span_entry
from tests.stockify.test_chain_view import EXPIRY, STRIKES
from tests.stockify.test_engine_parity import assert_same_artifacts
from tests.stockify.test_spanlite_parity import OptionTrader, spanlite_bundle, spanlite_world

VECTORS = Path(__file__).resolve().parents[1] / "fixtures" / "span" / "span_vectors.json"
FAR = "2026-09-09"          # the fixture's 29-Dec-2026 future, moved as the chain world's expiry moves 23-Nov → 4-Aug
D1, D2 = "2026-08-03", EXPIRY


# ── The vectors the engine's doctest reads ──────────────────────────────────

def _cases() -> list[tuple[str, str, list[tuple[tuple, int]]]]:
    def ce(e: str, k: int) -> tuple:
        return "CE", e, k

    def pe(e: str, k: int) -> tuple:
        return "PE", e, k

    n23, n17, f23, f29 = "2026-11-23", "2026-11-17", ("FUT", "2026-11-23", None), ("FUT", "2026-12-29", None)
    return [
        ("short straddle", "NIFTY", [(ce(n23, 2500000), -65), (pe(n23, 2500000), -65)]),
        ("iron fly", "NIFTY", [(ce(n23, 2500000), -65), (pe(n23, 2500000), -65), (ce(n23, 2520000), 65),
                               (pe(n23, 2480000), 65)]),
        ("bought call", "NIFTY", [(ce(n23, 2500000), 65)]),
        ("bought strangle", "NIFTY", [(ce(n17, 2510000), 130), (pe(n17, 2490000), 65)]),
        ("short future", "NIFTY", [(f23, -65)]),
        ("calendar futures", "NIFTY", [(f23, 65), (f29, -65)]),
        ("uneven calendar", "NIFTY", [(f23, 130), (f29, -65), (ce(n17, 2500000), -65)]),
        ("option calendar", "NIFTY", [(ce(n17, 2500000), -65), (ce(n23, 2500000), 130)]),
        ("netted legs", "NIFTY", [(f23, 65), (f23, -30), (ce(n23, 2500000), -65), (ce(n23, 2500000), 65)]),
        ("all netted away", "NIFTY", [(f23, 65), (f23, -65)]),
        ("bank straddle", "BANKNIFTY", [(ce("2026-11-23", 5900000), -30), (pe("2026-11-23", 5900000), -30)]),
        ("bank calendar", "BANKNIFTY", [(f23, -30), (f29, 30), (ce("2026-11-23", 5900000), -30)]),
        ("missing strike", "NIFTY", [(ce(n23, 2000000), -65), (ce(n23, 2500000), -65)]),
        ("weight 0.8: exact spreads", "NIFTY_W08", [(f23, 65), (f29, -65)]),
        ("weight 3: spreads not exact", "NIFTY_W3", [(f23, 65), (f29, -65)]),
        ("som binds", "NIFTY_SOM", [(ce(n23, 2500000), -65), (ce(n23, 2520000), 65), (pe(n17, 2475000), -65)]),
        ("cvf 2: refused", "NIFTY_CVF2", [(ce(n23, 2500000), -65)]),
    ]


def _commodities() -> dict[str, dict]:
    f = parse_span(FIXTURE_SPAN, {"NIFTY", "BANKNIFTY"})
    out = {code: bundle_mod._span_commodity(cc, set(cc.contracts)) for code, cc in sorted(f.commodities.items())}
    w08 = copy.deepcopy(out["NIFTY"])
    w3 = copy.deepcopy(out["NIFTY"])
    for sp in w08["calendar_spreads"]:
        for leg in sp["legs"]:
            leg["weight"] = "0.8"
    for sp in w3["calendar_spreads"]:
        for leg in sp["legs"]:
            leg["weight"] = "3"
    som = copy.deepcopy(out["NIFTY"])
    som["som_tiers"][0]["rates"]["1"] = "2412.5"
    cvf = copy.deepcopy(out["NIFTY"])
    cvf["cvf"]["OOP"] = "2.00"
    out.update(NIFTY_W08=w08, NIFTY_W3=w3, NIFTY_SOM=som, NIFTY_CVF2=cvf)
    return out


def oracle_vectors() -> dict:
    commodities = _commodities()
    cases = []
    for name, code, legs in _cases():
        case = {"name": name, "commodity": code,
                "legs": [{"kind": k[0], "expiry": k[1], "strike_paise": k[2], "units": u} for k, u in legs]}
        try:
            res = span_requirement(commodity_from_bundle(code, commodities[code]), legs)
        except SpanCalcError as exc:
            case["error"] = str(exc)
        else:
            if res.missing:
                case["missing"] = [list(k) for k in res.missing]
            else:
                case.update({f: str(getattr(res, f)) for f in ("scan", "intra_spread", "inter_credit", "som",
                                                                "long_option_value", "short_option_value", "span")})
                case["span_paise"] = span_paise(res)
        cases.append(case)
    return {"note": "Generated by tests/stockify/test_span_parity.py from the oracle (stockify.fno.span_exact); "
                    "do not edit by hand.", "commodities": commodities, "cases": cases}


class SpanVectorTests(unittest.TestCase):
    def test_vectors_file_is_what_the_oracle_computes(self):
        self.assertEqual(json.loads(VECTORS.read_text(encoding="utf-8")), oracle_vectors())

    def test_vectors_cover_every_element(self):
        cases = oracle_vectors()["cases"]
        done = [c for c in cases if "span" in c]
        self.assertTrue(any(float(c["intra_spread"]) > 0 for c in done))                 # calendar spreads formed
        self.assertTrue(any("." in c["intra_spread"] and float(c["intra_spread"]) > 0 for c in done))
        self.assertTrue(any(float(c["som"]) > 0 and c["span"] == c["som"] for c in done))   # SOM binds
        self.assertTrue(any(float(c["long_option_value"]) > 0 for c in done))
        self.assertTrue(any(float(c["span"]) == 0 for c in done))
        self.assertTrue(any("missing" in c for c in cases))
        self.assertEqual(sum("error" in c for c in cases), 2)


# ── Worlds on both implementations ──────────────────────────────────────────

def staircase_world(seed: int) -> dict:
    """The SPAN-lite parity world (two days, NIFTY future and options) plus a far future (id 3) quoted beside the
    near one, so the trader can hold a calendar spread."""
    g = spanlite_world(seed, True)
    g["bundle"]["instruments"].append(
        {"id": 3, "kind": "FUT", "tradingsymbol": "NIFTYFUT2", "underlying": "NIFTY", "underlying_id": 1,
         "expiry": FAR, "specs": [{"from": "2026-01-01", "lot": 75, "tick_paise": 10}]})
    far = []
    for e in g["events"]:
        if e.get("instrument") == 2 and e["kind"] == "TICK_FULL":
            x = copy.deepcopy(e)
            x.update(instrument=3, t=e["t"] + 1_000_000, ltp=e["ltp"] + 13_290)
            x["bids"] = [[b[0] + 13_290, b[1], b[2]] for b in e.get("bids", [])]
            x["asks"] = [[a[0] + 13_290, a[1], a[2]] for a in e.get("asks", [])]
            far.append(x)
    g["events"] = sorted(g["events"] + far, key=lambda e: e["t"])
    return g


def staircase_bundle(g: dict, model: str = "staircase", *, d1: str = "normal", d2: str = "normal",
                     elm1: str | None = "2", elm2: str | None = "3.5", drop: frozenset = frozenset(),
                     calibration_id: str = "test", spanlite: bool = True) -> dict:
    """The world's bundle with span / elm sections from the fixture. Per day `normal` (the previous evening's file),
    `late` (created 12:00 IST: nothing in force before), `none` ([]) or `drop` (lacking the keys in `drop`);
    `elm1` / `elm2` the day's ELM % (None: no ELM file that day)."""
    g = copy.deepcopy(g)
    g["run"]["margin"] = {"model": model}
    b = spanlite_bundle(g, calibration_id=calibration_id)
    if not spanlite:
        b["margin_params"]["spanlite"] = []
    rows = b["instruments"]
    span, elm = {}, {}
    for day, how, pct in ((D1, d1, elm1), (D2, d2, elm2)):
        d = date.fromisoformat(day)
        evening = timeutil.ist_ns(date.fromordinal(d.toordinal() - 1), "20:26")
        span[day] = {"normal": [fixture_span_entry("NIFTY", rows, EXPIRY, evening)],
                     "late": [fixture_span_entry("NIFTY", rows, EXPIRY, timeutil.ist_ns(d, "12:00"))],
                     "none": [],
                     "drop": [fixture_span_entry("NIFTY", rows, EXPIRY, evening, drop)]}[how]
        if pct is not None:
            elm[day] = fixture_elm("NIFTY", rows, pct)
    b.update(span=span, span_skipped={}, elm=elm)
    return b


class SpanTrader(OptionTrader):
    """OptionTrader (straddles, wings, the future, closes) plus single bought options and calendar spreads on the
    two futures; funds logged after every order."""

    def on_tick(self, ctx, tick):  # noqa: ANN001
        if tick.instrument != 1:
            return
        rng = ctx.rng()
        k = rng.below(6)
        if k == 0:                                      # a calendar: near and far futures, opposite sides
            side = "BUY" if rng.below(2) else "SELL"
            for iid, s in ((2, side), (3, "SELL" if side == "BUY" else "BUY")):
                oid = ctx.place(iid, s, 75, "NRML", "MARKET", protection=5000)
                ctx.log("info", f"order {oid} {iid} {s} 75 {ctx.order(oid).status} funds {ctx.funds()!r}")
            return
        if k == 1:                                      # a single bought option
            c = ctx.chain("NIFTY", EXPIRY)
            if c and c.atm_strike_paise is not None:
                by = {(r["strike_paise"], r["kind"]): r["instrument_id"] for r in c}
                iid = by[(STRIKES[rng.below(len(STRIKES))], "CE" if rng.below(2) else "PE")]
                oid = ctx.place(iid, "BUY", 75, "NRML", "MARKET", protection=5000)
                ctx.log("info", f"order {oid} {iid} BUY 75 {ctx.order(oid).status} funds {ctx.funds()!r}")
            return
        super().on_tick(ctx, tick)


def run_pair(g: dict, b: dict, folder: Path) -> tuple[Exception | None, Exception | None]:
    pack = golden.build_pack(g, folder)
    out: list[Exception | None] = []
    for impl, runner in (("oracle", oracle_run), ("engine", engine.run_bundle)):
        try:
            runner(copy.deepcopy(b), [pack], folder / impl, strategy=SpanTrader(), report=False)
            out.append(None)
        except Exception as exc:  # noqa: BLE001 — both sides must fail alike
            out.append(exc)
    return out[0], out[1]


def rows_of(folder: Path) -> list[dict]:
    with (folder / "margin_daily.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def labels_of(folder: Path) -> list[str]:
    return sorted(json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["labels"])


def step_counts(folder: Path) -> dict[str, int]:
    """margin_steps:<step>=<days> labels as {step: days}."""
    out = {}
    for x in labels_of(folder):
        if x.startswith("margin_steps:"):
            step, days = x.split(":", 1)[1].split("=")
            out[step] = int(days)
    return out


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class SpanParityTests(unittest.TestCase):
    def assert_same(self, folder: Path) -> None:
        assert_same_artifacts(self, folder / "oracle", folder / "engine")
        self.assertEqual((folder / "oracle" / "margin_daily.csv").read_bytes(),
                         (folder / "engine" / "margin_daily.csv").read_bytes())
        self.assertEqual(labels_of(folder / "oracle"), labels_of(folder / "engine"))

    def test_span_exact_parity_on_fixture_worlds(self):
        """Futures (with calendars), straddles, flies, bought options; under staircase also a missing strike (that
        underlying steps down while it is held) and span_exact where every contract is in the file."""
        missing = frozenset([("CE", EXPIRY, STRIKES[-1]), ("PE", EXPIRY, STRIKES[0])])
        seen: dict[str, int] = {}
        orders = 0
        for seed in (4910, 4911, 4912, 4913):
            for model, over in (("span_exact", {}), ("staircase", {}), ("staircase", {"d1": "drop", "drop": missing})):
                with self.subTest(seed=seed, model=model, **{k: str(v) for k, v in over.items()}), \
                        tempfile.TemporaryDirectory(prefix="span-parity-") as tmp:
                    folder = Path(tmp)
                    g = staircase_world(seed)
                    oe, ee = run_pair(g, staircase_bundle(g, model, **over), folder)
                    self.assertEqual((oe is None, ee is None), (True, True), f"oracle {oe!r} / engine {ee!r}")
                    self.assert_same(folder)
                    for r in rows_of(folder / "oracle"):
                        seen[r["step"]] = seen.get(r["step"], 0) + 1
                        self.assertEqual(r["model"], model)
                        self.assertEqual(int(r["total"]),
                                         max(int(r["scan"]), int(r["somc"])) + int(r["elm"]) + int(r["addons"]))
                    log = (folder / "oracle" / "events_log.txt").read_text(encoding="utf-8")
                    orders += log.count(" order ")
        self.assertGreater(seen.get("B1_span_nse_elm", 0), 0, seen)
        self.assertGreater(orders, 100)

    def test_span_exact_fails_alike_where_it_cannot_price(self):
        """No file in force on day 2 / a strike missing from the file: both implementations fail, naming it."""
        missing = frozenset([("CE", EXPIRY, s) for s in STRIKES] + [("PE", EXPIRY, s) for s in STRIKES])
        for over, text in (({"d2": "none"}, "no SPAN file in force for NIFTY"),
                           ({"d1": "drop", "drop": missing}, "not in the SPAN file in force")):
            with self.subTest(**{k: str(v) for k, v in over.items()}), \
                    tempfile.TemporaryDirectory(prefix="span-exact-fail-") as tmp:
                g = staircase_world(4911)
                oe, ee = run_pair(g, staircase_bundle(g, "span_exact", **over), Path(tmp))
                self.assertIsNotNone(oe)
                self.assertIsNotNone(ee)
                self.assertIn(text, str(oe))
                self.assertEqual(str(ee), str(oe))

    def test_staircase_parity_steps(self):
        """Every step on both implementations, byte for byte, with the same margin_steps labels: B1 (file + ELM
        file), B2 (no ELM file), B3 / B4 (no file in force, an adopted / the placeholder SPAN-lite row), B5 (no
        SPAN-lite row either); a late file switches steps within the day."""
        variants = [
            ("B1", {}),
            ("B2", {"elm1": None, "elm2": None}),
            ("B3", {"d1": "none", "d2": "none"}),
            ("B4", {"d1": "none", "d2": "none", "calibration_id": "placeholder"}),
            ("B5", {"d1": "none", "d2": "none", "spanlite": False, "elm1": None, "elm2": None}),
            ("late", {"d1": "late", "d2": "late"}),
        ]
        reached: dict[str, int] = {}
        for name, over in variants:
            with self.subTest(variant=name), tempfile.TemporaryDirectory(prefix="staircase-parity-") as tmp:
                folder = Path(tmp)
                g = staircase_world(4911)
                oe, ee = run_pair(g, staircase_bundle(g, **over), folder)
                self.assertEqual((oe, ee), (None, None))
                self.assert_same(folder)
                steps = step_counts(folder / "engine")
                for step, days in steps.items():
                    reached[step] = reached.get(step, 0) + days
                    self.assertIn(step, STEPS)
                    self.assertIn(f"margin:{step}", labels_of(folder / "engine"))
                if name.startswith("B"):
                    self.assertIn(next(s for s in STEPS if s.startswith(name)), steps, steps)
                if name == "late":                              # before 12:00 nothing in force, then B1
                    self.assertTrue({"B1_span_nse_elm", "B3_spanlite_calibrated"} <= set(steps), steps)
        self.assertEqual(set(reached), set(STEPS))

    def test_no_span_section_is_labelled_alike(self):
        with tempfile.TemporaryDirectory(prefix="staircase-nosection-") as tmp:
            folder = Path(tmp)
            g = staircase_world(4912)
            b = staircase_bundle(g)
            for key in ("span", "span_skipped", "elm"):
                del b[key]
            self.assertEqual(run_pair(g, b, folder), (None, None))
            self.assert_same(folder)
            self.assertIn("margin_span:no_section", labels_of(folder / "engine"))


if __name__ == "__main__":
    if "--write" in sys.argv:
        VECTORS.write_text(json.dumps(oracle_vectors(), indent=1, sort_keys=False) + "\n", encoding="utf-8")
        print(f"wrote {VECTORS}")
    else:
        unittest.main()
