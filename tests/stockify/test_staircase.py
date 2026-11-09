"""MEZ 4.9: the margin staircase in the oracle (spec §5.1, §8): B1 exact SPAN + NSE ELM, B2 exact SPAN + rule ELM,
B3 calibrated SPAN-lite, B4 placeholder SPAN-lite, B5 flat, chosen per underlying per check and recorded.

The SPAN entries are built from the trimmed real 2026-11-09 i1 file (tests/fixtures/span), as bundle.span_section
writes them; the ELM % are made up (decimal strings, as the bundle carries them).
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

from stockify import bundle as bundle_mod
from stockify import numerics, timeutil
from stockify.fno.span_exact import SpanMissingError, span_paise, span_requirement
from stockify.fno.span_file import parse_span
from stockify.fno.spanlite import SpanLiteMargin
from stockify.fno.staircase import STEPS, SpanExactMargin, StaircaseMargin
from stockify.oracle.margin import FlatMargin

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "span" / "nsccl.20261109.i1.trim.spn"
DAY = "2026-11-09"
EXPIRY_DAY = "2026-11-17"                                  # the fixture's weekly NIFTY expiry
CREATED = timeutil.ist_ns(date(2026, 11, 8), "20:26")      # the i1 file's <created>
NOW = timeutil.ist_ns(date(2026, 11, 9), "10:00")
LAST = timeutil.ist_ns(date(2026, 11, 23), "15:30")
SPOT, BANK_SPOT = 2_498_640, 5_885_065                    # paise: the file's NIFTY / BANKNIFTY spot
FUT_PRICE = 2_505_000

ADDON = [{"applies": {}, "effective_from": "2024-11-20", "pct": "0.02", "pct_ppb": 20_000_000}]
FLAT = [{"applies": {}, "effective_from": "2015-01-01", "index_pct": "0.1", "stock_pct": "0.2",
         "index_pct_ppb": 100_000_000, "stock_pct_ppb": 200_000_000}]


def leg_row(iid: int, kind: str, expiry: str, strike: int | None, und: str = "NIFTY", uid: int = 1) -> dict:
    return {"id": iid, "kind": kind, "underlying": und, "underlying_id": uid, "underlying_class": "INDEX",
            "expiry": expiry, "actual_expiry": None, "strike_paise": strike,
            "last_trading_ns": timeutil.ist_ns(date.fromisoformat(expiry), "15:30")}


ROWS = {r["id"]: r for r in (
    leg_row(2, "FUT", "2026-11-23", None),
    leg_row(10, "CE", "2026-11-23", 2_500_000), leg_row(11, "PE", "2026-11-23", 2_500_000),
    leg_row(12, "CE", "2026-11-17", 2_000_000),                       # strike 20,000: not in the file
    leg_row(13, "CE", EXPIRY_DAY, 2_500_000), leg_row(14, "PE", EXPIRY_DAY, 2_500_000),
    leg_row(20, "FUT", "2026-11-23", None, "BANKNIFTY", 3),
)}
PRICES = {1: SPOT, 3: BANK_SPOT, 2: FUT_PRICE, 20: 5_900_000}


def _key(row: dict) -> tuple:
    return row["kind"], row["expiry"], None if row["kind"] == "FUT" else row["strike_paise"]


def span_entry(created_ns: int = CREATED) -> dict:
    """The fixture's NIFTY and BANKNIFTY as one bundle span entry (bundle._span_commodity's shape)."""
    f = parse_span(FIXTURE, {"NIFTY", "BANKNIFTY"})
    keys: dict[str, set] = {}
    for r in ROWS.values():
        keys.setdefault(r["underlying"], set()).add(_key(r))
    return {"created_ns": created_ns, "file": "span/2026/nsccl.20261109.i1.zip", "sha256": "0" * 64,
            "scan_points": [], "commodities": {code: bundle_mod._span_commodity(f.commodities[code], keys[code])
                                               for code in sorted(keys)}}


def elm_day(pct: dict[int, str]) -> dict:
    out: dict = {}
    for iid, p in pct.items():
        r = ROWS[iid]
        out.setdefault(r["underlying"], {})[f"{r['kind']}|{r['expiry']}|{'' if r['kind'] == 'FUT' else r['strike_paise']}"] = p
    return out


def spanlite_row(calibration_id: str = "calibration-test") -> dict:
    row = {"applies": {"underlying_class": "INDEX"}, "effective_from": "2015-01-01", "psr_pct": "0.06",
           "vsr_pct": "0.25", "extreme_multiple": 3, "extreme_weight": "0.35", "somc_per_lot_paise": 200_000,
           "elm_pct": "0.02", "default_iv": "0.15", "calibration_id": calibration_id}
    for f in ("psr_pct", "vsr_pct", "extreme_weight", "elm_pct", "default_iv"):
        row[f"{f}_ppb"] = numerics.to_ppb(row[f])
    return row


class FakeEngine:
    def __init__(self, span: dict | None = None, elm: dict | None = None, spanlite: list | None = None,
                 now: int = NOW):
        self.bundle = {"margin_params": {"spanlite": [spanlite_row()] if spanlite is None else spanlite,
                                         "flat": FLAT, "expiry_day_short_index_option_addon": ADDON},
                       "broker_policy": {"mis_margin_pct": []}, "days": [],
                       "pricing": {"risk_free": [{"effective_from": "2015-01-01", "rate_ppb": 65_000_000}]},
                       "span": {DAY: [span_entry()]} if span is None else span,
                       "elm": {DAY: elm_day({2: "2", 10: "2", 11: "2", 20: "2"})} if elm is None else elm}
        self.now, self.labels = now, set()

        class Ctx:
            def chain(self, und, exp):  # noqa: ANN001, ANN202
                return None

        self.ctx = Ctx()

    def spec(self, iid: int, d: str) -> dict:
        return {"lot": 65}


def price(iid: int) -> int | None:
    return PRICES.get(iid)


def rupee_up_ppb(paise_ppb: int) -> int:
    return numerics.ceil_to(numerics.ceil_div(paise_ppb, 10**9), 100)


NIFTY = parse_span(FIXTURE, {"NIFTY"}).commodities["NIFTY"]


def exact_span(legs: list[tuple[int, int]]) -> int:
    return span_paise(span_requirement(NIFTY, [(_key(ROWS[i]), u) for i, u in legs]))


def nrml(legs: list[tuple[int, int]]) -> list[tuple[int, str, int]]:
    return [(i, "NRML", u) for i, u in legs]


class StaircaseTests(unittest.TestCase):
    def test_step_names(self):
        self.assertEqual(STEPS, ("B1_span_nse_elm", "B2_span_rule_elm", "B3_spanlite_calibrated",
                                 "B4_spanlite_placeholder", "B5_flat"))

    def test_b1_when_file_and_elm_present(self):
        """A short straddle priced by the file in force (exact SPAN) and the ELM file's % on the index price."""
        e = FakeEngine()
        m = StaircaseMargin(e.bundle, e)
        legs = [(10, -65), (11, -65)]
        [b] = m.breakdown(nrml(legs), ROWS.__getitem__, price, DAY)
        self.assertEqual(b.step, "B1_span_nse_elm")
        self.assertEqual(b.scan, exact_span(legs))
        self.assertEqual(b.elm, rupee_up_ppb(20_000_000 * SPOT * 130))      # 2 % × ₹24,986.40 × 130 units
        self.assertEqual(b.elm, 6_496_500)
        self.assertEqual(b.addons, 0)
        self.assertEqual(b.total, b.scan + b.elm)
        self.assertEqual(m.requirement(nrml(legs), ROWS.__getitem__, price, DAY), b.total)

    def test_b1_futures_exposure_on_the_futures_price_bought_options_none(self):
        e = FakeEngine()
        m = StaircaseMargin(e.bundle, e)
        legs = [(2, -65), (10, 65)]                      # a short future hedged with a bought call
        [b] = m.breakdown(nrml(legs), ROWS.__getitem__, price, DAY)
        self.assertEqual(b.step, "B1_span_nse_elm")
        self.assertEqual(b.scan, exact_span(legs))
        self.assertEqual(b.elm, rupee_up_ppb(20_000_000 * FUT_PRICE * 65))

    def test_missing_strike_steps_whole_underlying_down(self):
        """One NIFTY leg is missing from the file in force: all NIFTY legs go to SPAN-lite together (never mixed);
        BANKNIFTY, fully in the file, stays exact. Each underlying's step is recorded."""
        e = FakeEngine()
        m = StaircaseMargin(e.bundle, e)
        legs = nrml([(10, -65), (12, -65), (20, -30)])
        nifty, bank = m.breakdown(legs, ROWS.__getitem__, price, DAY)
        self.assertEqual((nifty.underlying_id, nifty.step), (1, "B3_spanlite_calibrated"))
        lite = SpanLiteMargin(e.bundle, e).breakdown(nrml([(10, -65), (12, -65)]), ROWS.__getitem__, price, DAY)
        self.assertEqual((nifty.scan, nifty.somc, nifty.elm, nifty.addons),
                         (lite[0].scan, lite[0].somc, lite[0].elm, lite[0].addons))
        self.assertEqual((bank.underlying_id, bank.step), (3, "B1_span_nse_elm"))
        self.assertIn("margin:B3_spanlite_calibrated", m.labels)
        self.assertIn("margin:B1_span_nse_elm", m.labels)

    def test_nothing_in_force_yet_steps_down(self):
        """An entry applies from its own created_ns: before it, nothing is in force."""
        e = FakeEngine(span={DAY: [span_entry(created_ns=NOW + 1)]})
        m = StaircaseMargin(e.bundle, e)
        [b] = m.breakdown(nrml([(10, -65), (11, -65)]), ROWS.__getitem__, price, DAY)
        self.assertEqual(b.step, "B3_spanlite_calibrated")
        e.now = NOW + 1
        [b] = m.breakdown(nrml([(10, -65), (11, -65)]), ROWS.__getitem__, price, DAY)
        self.assertEqual(b.step, "B1_span_nse_elm")
        for span in ({DAY: []}, {}):                     # a day with no usable file; no span section at all
            e = FakeEngine(span=span)
            [b] = StaircaseMargin(e.bundle, e).breakdown(nrml([(10, -65)]), ROWS.__getitem__, price, DAY)
            self.assertEqual(b.step, "B3_spanlite_calibrated")

    def test_later_entry_takes_over_from_its_created_time(self):
        """Two entries: the later one (here without BANKNIFTY) is used from its created_ns on."""
        later = span_entry(created_ns=NOW + 10)
        del later["commodities"]["BANKNIFTY"]
        e = FakeEngine(span={DAY: [span_entry(), later]})
        m = StaircaseMargin(e.bundle, e)
        [b] = m.breakdown(nrml([(20, -30)]), ROWS.__getitem__, price, DAY)
        self.assertEqual(b.step, "B1_span_nse_elm")
        e.now = NOW + 10
        [b] = m.breakdown(nrml([(20, -30)]), ROWS.__getitem__, price, DAY)
        self.assertEqual(b.step, "B3_spanlite_calibrated")

    def test_b1_does_not_add_expiry_day_addon_b2_does(self):
        """On the options' expiry day NSE's ELM % (4 / 5 %) already holds the uplift: B1 adds no X-9 add-on. Without
        an ELM % for every leg, B2 takes ELM from the dated rules (2 %) and adds the X-9 add-on (2 %)."""
        span = {EXPIRY_DAY: [span_entry()]}
        now = timeutil.ist_ns(date(2026, 11, 17), "10:00")
        legs = nrml([(13, -65), (14, -65)])
        e = FakeEngine(span=span, elm={EXPIRY_DAY: elm_day({13: "4", 14: "5"})}, now=now)
        [b1] = StaircaseMargin(e.bundle, e).breakdown(legs, ROWS.__getitem__, price, EXPIRY_DAY)
        self.assertEqual(b1.step, "B1_span_nse_elm")
        self.assertEqual(b1.elm, rupee_up_ppb((40_000_000 + 50_000_000) * SPOT * 65))
        self.assertEqual(b1.addons, 0)
        for elm in ({}, {EXPIRY_DAY: elm_day({13: "4"})}):   # no file; a file lacking one leg
            with self.subTest(elm=elm):
                e = FakeEngine(span=span, elm=elm, now=now)
                [b2] = StaircaseMargin(e.bundle, e).breakdown(legs, ROWS.__getitem__, price, EXPIRY_DAY)
                self.assertEqual(b2.step, "B2_span_rule_elm")
                self.assertEqual(b2.scan, b1.scan)
                self.assertEqual(b2.elm, rupee_up_ppb(20_000_000 * SPOT * 130))
                self.assertEqual(b2.addons, rupee_up_ppb(20_000_000 * SPOT * 130))

    def test_b3_requires_non_placeholder_row(self):
        """Without SPAN: an adopted (non-placeholder) SPAN-lite row → B3; the placeholder row → B4; no row → B5 flat."""
        legs = nrml([(10, -65), (12, -65)])
        for rows, step in (([spanlite_row("calibration-2026-11-09")], "B3_spanlite_calibrated"),
                           ([spanlite_row("placeholder")], "B4_spanlite_placeholder"), ([], "B5_flat")):
            with self.subTest(step=step):
                e = FakeEngine(spanlite=rows)
                m = StaircaseMargin(e.bundle, e)
                [b] = m.breakdown(legs, ROWS.__getitem__, price, DAY)
                self.assertEqual(b.step, step)
        flat = FlatMargin(e.bundle).requirement(legs, ROWS.__getitem__, price, DAY)
        self.assertEqual((b.scan, b.somc, b.elm, b.addons, b.total), (flat, 0, 0, 0, flat))

    def test_no_rule_elm_row_steps_b2_down_to_flat(self):
        """B2 needs the dated rule ELM (the SPAN-lite row); without it and without an ELM file, flat."""
        e = FakeEngine(spanlite=[], elm={})
        [b] = StaircaseMargin(e.bundle, e).breakdown(nrml([(10, -65)]), ROWS.__getitem__, price, DAY)
        self.assertEqual(b.step, "B5_flat")

    def test_step_days_counted_in_labels(self):
        e = FakeEngine(span={DAY: [span_entry()], "2026-11-10": []}, elm={DAY: elm_day({10: "2"})})
        m = StaircaseMargin(e.bundle, e)
        legs = nrml([(10, -65)])
        m.requirement(legs, ROWS.__getitem__, price, DAY)
        m.requirement(legs, ROWS.__getitem__, price, DAY)
        m.requirement(legs, ROWS.__getitem__, price, "2026-11-10")
        self.assertTrue({"margin:B1_span_nse_elm", "margin_steps:B1_span_nse_elm=1", "margin:B3_spanlite_calibrated",
                         "margin_steps:B3_spanlite_calibrated=1"} <= m.labels)
        m.requirement([], ROWS.__getitem__, price, "2026-11-11")    # no legs: no step used
        self.assertFalse(any("=2" in x for x in m.labels))

    def test_span_exact_model_has_no_fallback(self):
        """The strict model raises naming the missing contract (never SPAN-lite, never ₹0)."""
        e = FakeEngine()
        m = SpanExactMargin(e.bundle, e)
        [b] = m.breakdown(nrml([(10, -65), (11, -65)]), ROWS.__getitem__, price, DAY)
        self.assertEqual(b.step, "B1_span_nse_elm")
        with self.assertRaises(SpanMissingError) as cm:
            m.requirement(nrml([(10, -65), (12, -65)]), ROWS.__getitem__, price, DAY)
        self.assertIn("CE 2026-11-17 2000000", str(cm.exception))
        self.assertIn("NIFTY", str(cm.exception))
        e = FakeEngine(span={DAY: [span_entry(created_ns=NOW + 1)]})
        with self.assertRaises(SpanMissingError) as cm:
            SpanExactMargin(e.bundle, e).requirement(nrml([(10, -65)]), ROWS.__getitem__, price, DAY)
        self.assertIn("no SPAN file in force", str(cm.exception))
        e = FakeEngine(elm={})                             # exact SPAN, ELM from the rules: B2
        [b] = SpanExactMargin(e.bundle, e).breakdown(nrml([(10, -65)]), ROWS.__getitem__, price, DAY)
        self.assertEqual(b.step, "B2_span_rule_elm")


# ── A run on the oracle: the step column and the labels ─────────────────────

def _synthetic_entry(g: dict, created_ns: int) -> dict:
    """A made-up SPAN entry for the chain world's NIFTY contracts: risk arrays in whole rupees per unit."""
    moves = [0, 0, 1, 1, -1, -1, 2, 2, -2, -2, 3, 3, -3, -3, 6, -6]     # thirds of the range
    contracts = []
    for r in sorted(g["bundle"]["instruments"], key=lambda r: (r["kind"], r.get("strike_paise") or 0)):
        if r["kind"] == "FUT":
            ra, delta = [str(-m * 500) for m in moves], "1"
        elif r["kind"] == "CE":
            ra, delta = [str(-m * 250 + 20) for m in moves], "0.5"
        elif r["kind"] == "PE":
            ra, delta = [str(m * 250 + 20) for m in moves], "-0.5"
        else:
            continue
        contracts.append({"kind": r["kind"], "expiry": r["expiry"], "strike_paise": r.get("strike_paise")
                          if r["kind"] != "FUT" else None, "price": "100", "delta": delta, "vol": "0.15",
                          "risk_array": ra, "composite_delta": delta})
    cc = {"spot": "25000", "cvf": {"FUT": "1.00", "OOP": "1.00"}, "som_method": "GROSS", "contracts": contracts,
          "series": [], "scan_tiers": [], "intra_tiers": [], "inter_tiers": [],
          "som_tiers": [{"number": 1, "start": None, "end": None, "rates": {"1": "0"}}], "calendar_spreads": []}
    return {"created_ns": created_ns, "file": "span/2026/nsccl.20260803.i1.zip", "sha256": "0" * 64,
            "scan_points": [], "commodities": {"NIFTY": cc}}


class StaircaseRunTests(unittest.TestCase):
    def test_steps_recorded_in_csv_and_labels(self):
        from stockify.oracle import run_bundle
        from tests.stockify.test_spanlite_parity import OptionTrader, spanlite_bundle, spanlite_world
        from stockify import golden
        g = spanlite_world(4811, True)
        g["run"]["margin"] = {"model": "staircase"}
        b = spanlite_bundle(g)
        self.assertEqual(b["run"]["margin"]["model"], "staircase")
        d1, d2 = b["days"][0]["date"], b["days"][1]["date"]
        b["span"] = {d1: [_synthetic_entry(g, timeutil.ist_ns(date(2026, 8, 2), "20:26"))], d2: []}
        b["span_skipped"] = {}
        elm = {}
        for r in b["instruments"]:
            if r["kind"] in ("FUT", "CE", "PE"):
                elm[f"{r['kind']}|{r['expiry']}|{'' if r['kind'] == 'FUT' else r['strike_paise']}"] = "2"
        b["elm"] = {d1: {"NIFTY": elm}}
        with tempfile.TemporaryDirectory(prefix="staircase-") as tmp:
            folder = Path(tmp)
            run_bundle(copy.deepcopy(b), [golden.build_pack(g, folder)], folder / "oracle", strategy=OptionTrader(),
                       report=False)
            text = (folder / "oracle" / "margin_daily.csv").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("trade_date,underlying_id,model,step,scan,somc,elm,addons,total\n"))
            with (folder / "oracle" / "margin_daily.csv").open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            labels = json.loads((folder / "oracle" / "manifest.json").read_text())["labels"]
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r["model"], "staircase")
            self.assertEqual(r["step"], "B1_span_nse_elm" if r["trade_date"] == d1 else "B3_spanlite_calibrated")
            self.assertEqual(int(r["total"]), max(int(r["scan"]), int(r["somc"])) + int(r["elm"]) + int(r["addons"]))
        self.assertIn("margin:B1_span_nse_elm", labels)
        self.assertIn("margin_steps:B1_span_nse_elm=1", labels)
        self.assertIn("margin:B3_spanlite_calibrated", labels)       # day 2's checks: no file in force
        self.assertIn("margin_steps:B3_spanlite_calibrated=1", labels)
        for x in labels:
            if x.startswith("margin_steps:"):
                self.assertIn(x.split(":")[1].split("=")[0], STEPS)

    def test_spanlite_rows_carry_their_step(self):
        """The spanlite model writes its step too: B3 for an adopted row, B4 for the placeholder."""
        from stockify.oracle import run_bundle
        from tests.stockify.test_spanlite_parity import OptionTrader, spanlite_bundle, spanlite_world
        from stockify import golden
        for cal, step in (("test", "B3_spanlite_calibrated"), ("placeholder", "B4_spanlite_placeholder")):
            with self.subTest(cal=cal), tempfile.TemporaryDirectory(prefix="spanlite-step-") as tmp:
                folder = Path(tmp)
                g = spanlite_world(4811, True)
                b = spanlite_bundle(g, calibration_id=cal)
                run_bundle(b, [golden.build_pack(g, folder)], folder / "oracle", strategy=OptionTrader(),
                           report=False)
                with (folder / "oracle" / "margin_daily.csv").open(encoding="utf-8", newline="") as f:
                    rows = list(csv.DictReader(f))
                self.assertTrue(rows)
                self.assertEqual({(r["model"], r["step"]) for r in rows}, {("spanlite", step)})


if __name__ == "__main__":
    unittest.main()
