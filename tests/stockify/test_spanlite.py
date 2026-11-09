"""M4.8: T1 SPAN-lite margin in the oracle (C-12; A3 U-07)."""
from __future__ import annotations

import math
import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify import numerics
from stockify.fno import black76 as B
from stockify.fno.spanlite import IV_FALLBACK_LABEL, SpanLiteMargin
from stockify.strategy import ChainView

DAY = "2026-08-03"
EXPIRY = "2026-08-06"
NOW = 1_785_729_600_000_000_000          # 2026-08-03 09:30 IST
LAST = NOW + 3 * 86_400 * 10**9 + 6 * 3_600 * 10**9


def spanlite_rows(**over) -> list[dict]:
    row = {"applies": {"underlying_class": "INDEX"}, "effective_from": "2015-01-01", "psr_pct": "0.06",
           "vsr_pct": "0.25", "extreme_multiple": 3, "extreme_weight": "0.35", "somc_per_lot_paise": 200_000,
           "elm_pct": "0.02", "default_iv": "0.15", "calibration_id": "test"}
    row.update(over)
    for f in ("psr_pct", "vsr_pct", "extreme_weight", "elm_pct", "default_iv"):
        row[f"{f}_ppb"] = numerics.to_ppb(row[f])
    return [row]


ADDON = [{"applies": {}, "effective_from": "2024-11-20", "pct": "0.02", "pct_ppb": 20_000_000}]


class FakeEngine:
    """Just enough engine: bundle, now, labels, spec, and ctx.chain from a dict of iv_mid by instrument."""

    def __init__(self, rows: list[dict], ivs: dict[int, float | None], forward: float | None, atm: int | None,
                 **span_over):
        self.bundle = {"margin_params": {"spanlite": spanlite_rows(**span_over), "flat": [],
                                         "expiry_day_short_index_option_addon": ADDON},
                       "broker_policy": {"mis_margin_pct": []},
                       "days": [],          # FlatMargin reads the trading days (D-60 delivery schedule)
                       "pricing": {"risk_free": [{"effective_from": "2015-01-01", "rate_ppb": 65_000_000}]}}
        self.now, self.labels, self._rows = NOW, set(), {r["id"]: r for r in rows}
        engine = self

        class Ctx:
            def chain(self, und, exp):  # noqa: ANN001, ANN202
                opts = sorted((r for r in rows if r["kind"] in ("CE", "PE") and r["expiry"] == exp),
                              key=lambda r: (r["strike_paise"], r["kind"]))
                return ChainView([{"instrument_id": r["id"], "kind": r["kind"], "strike_paise": r["strike_paise"],
                                   "iv_mid": engine.ivs.get(r["id"])} for r in opts], forward, "future", atm)

        self.ctx, self.ivs = Ctx(), dict(ivs)

    def spec(self, iid: int, d: str) -> dict:
        return {"lot": 75}

    def inst(self, iid: int) -> dict:
        return self._rows[iid]


def option(iid: int, kind: str, strike_rupees: int, expiry: str = EXPIRY) -> dict:
    return {"id": iid, "kind": kind, "underlying": "NIFTY", "underlying_id": 1, "underlying_class": "INDEX",
            "expiry": expiry, "actual_expiry": None, "strike_paise": strike_rupees * 100, "last_trading_ns": LAST}


FUT = {"id": 2, "kind": "FUT", "underlying": "NIFTY", "underlying_id": 1, "underlying_class": "INDEX",
       "expiry": EXPIRY, "actual_expiry": None, "strike_paise": None, "last_trading_ns": LAST}
SPOT = 2_500_000                          # ₹25,000 in paise


def model(rows, ivs, forward=25_000_00.0, atm=2_500_000, **over):  # noqa: ANN001, ANN201
    e = FakeEngine(rows, ivs, forward, atm, **over)
    return SpanLiteMargin(e.bundle, e), e


def price_of(paise: dict[int, int]):  # noqa: ANN201
    return lambda iid: paise.get(iid)


class SpanLiteTests(unittest.TestCase):
    def test_a_short_future_by_hand(self):
        """The worst scenario of a short future is the extreme up move: 3 × PSR at weight 0.35 (1.05 PSR) beats
        the plain +1 × PSR. ELM 2 % of notional. No options: no SOMC."""
        m, _ = model([FUT], {})
        legs = [(2, "NRML", -75)]
        [b] = m.breakdown(legs, lambda i: FUT, price_of({1: SPOT, 2: SPOT}), DAY)
        worst = 75 * 25_000.0 * 0.06 * 3 * 0.35                  # rupees
        self.assertEqual(b.scan, math.ceil(worst) * 100)
        self.assertEqual(b.elm, numerics.ceil_to(numerics.ceil_div(20_000_000 * SPOT * 75, 10**9), 100))
        self.assertEqual((b.somc, b.addons), (0, 0))
        self.assertEqual(m.requirement(legs, lambda i: FUT, price_of({1: SPOT, 2: SPOT}), DAY), b.total)
        self.assertEqual(b.total, b.scan + b.elm)

    def test_bought_options_alone_need_no_span(self):
        """MEZ 4.5: a book of bought options only has scan 0 (Kite: span 0; the premium is paid in cash). With a
        sold option or a future beside them, the scan is the scenario loss as before."""
        ce, pe = option(10, "CE", 25_000), option(11, "PE", 24_800)
        rows = {10: ce, 11: pe, 2: FUT}
        m, _ = model([ce, pe, FUT], {10: 0.12, 11: 0.13})
        prices = price_of({1: SPOT, 2: SPOT})
        [b] = m.breakdown([(10, "NRML", 75), (11, "NRML", 150)], rows.__getitem__, prices, DAY)
        self.assertEqual((b.scan, b.somc, b.elm, b.addons), (0, 0, 0, 0))
        [mixed] = m.breakdown([(10, "NRML", 75), (11, "NRML", -75)], rows.__getitem__, prices, DAY)
        self.assertGreater(mixed.scan, 0)
        [hedged] = m.breakdown([(10, "NRML", 75), (2, "NRML", -75)], rows.__getitem__, prices, DAY)
        self.assertGreater(hedged.scan, 0)

    def test_long_and_short_futures_cancel(self):
        m, _ = model([FUT], {})
        [b] = m.breakdown([(2, "NRML", 75), (2, "NRML", -75)], lambda i: FUT, price_of({1: SPOT, 2: SPOT}), DAY)
        self.assertEqual(b.scan, 0)

    def test_a_short_option_scan_is_the_worst_revaluation(self):
        ce = option(10, "CE", 25_000)
        m, _ = model([ce], {10: 0.12})
        [b] = m.breakdown([(10, "NRML", -75)], lambda i: ce, price_of({1: SPOT}), DAY)
        t, r, f = B.year_fraction(NOW, LAST), 0.065, 25_000.0
        v0 = B.price(True, f, 25_000.0, t, r, 0.12)
        losses = [75 * (B.price(True, f * (1 + mv * 0.06), 25_000.0, t, r, 0.12 * (1 + vol * 0.25)) - v0)
                  for mv in (0, 1 / 3, -1 / 3, 2 / 3, -2 / 3, 1, -1) for vol in (1, -1)]
        losses += [0.35 * 75 * (B.price(True, f * (1 + mv * 0.06), 25_000.0, t, r, 0.12) - v0) for mv in (3, -3)]
        self.assertEqual(b.scan, math.ceil(max(losses)) * 100)
        self.assertEqual(b.somc, 200_000)                          # one short lot
        self.assertEqual(b.elm, numerics.ceil_to(numerics.ceil_div(20_000_000 * SPOT * 75, 10**9), 100))
        self.assertEqual(b.addons, 0)                              # not the expiry day

    def test_somc_floor_for_far_otm_shorts(self):
        far = option(11, "CE", 32_000)
        m, _ = model([far], {11: 0.12})
        [b] = m.breakdown([(11, "NRML", -150)], lambda i: far, price_of({1: SPOT}), DAY)
        self.assertLess(b.scan, b.somc)
        self.assertEqual(b.somc, 2 * 200_000)                      # two lots
        self.assertEqual(b.total, b.somc + b.elm)

    def test_u07_an_iron_fly_needs_less_than_a_naked_straddle(self):
        rows = [option(20, "CE", 25_000), option(21, "PE", 25_000), option(22, "CE", 25_500), option(23, "PE", 24_500)]
        ivs = {20: 0.12, 21: 0.12, 22: 0.13, 23: 0.13}
        m, _ = model(rows, ivs)
        inst = {r["id"]: r for r in rows}.__getitem__
        straddle = [(20, "NRML", -75), (21, "NRML", -75)]
        fly = straddle + [(22, "NRML", 75), (23, "NRML", 75)]
        s = m.requirement(straddle, inst, price_of({1: SPOT}), DAY)
        f = m.requirement(fly, inst, price_of({1: SPOT}), DAY)
        self.assertLess(f, s)
        [sb], [fb] = (m.breakdown(x, inst, price_of({1: SPOT}), DAY) for x in (straddle, fly))
        self.assertLess(fb.scan, sb.scan / 2)                      # the wings cap the loss
        self.assertEqual(fb.elm, sb.elm)                           # ELM is on the short legs only

    def test_expiry_day_addon_on_short_index_options(self):
        today = option(30, "PE", 24_800, expiry=DAY)
        m, _ = model([today], {30: 0.12})
        [b] = m.breakdown([(30, "NRML", -75)], lambda i: today, price_of({1: SPOT}), DAY)
        self.assertEqual(b.addons, numerics.ceil_to(numerics.ceil_div(20_000_000 * SPOT * 75, 10**9), 100))
        [b2] = m.breakdown([(30, "NRML", 75)], lambda i: today, price_of({1: SPOT}), DAY)
        self.assertEqual(b2.addons, 0)                             # long: no add-on
        tomorrow = option(31, "PE", 24_800)
        [b3] = m.breakdown([(31, "NRML", -75)], lambda i: tomorrow, price_of({1: SPOT}), DAY)
        self.assertEqual(b3.addons, 0)

    def test_iv_fallbacks_and_their_label(self):
        ce, pe = option(40, "CE", 25_000), option(41, "PE", 25_000)
        inst = {40: ce, 41: pe}.__getitem__
        m, e = model([ce, pe], {40: 0.12, 41: None})
        m.breakdown([(40, "NRML", -75)], inst, price_of({1: SPOT}), DAY)
        self.assertNotIn(IV_FALLBACK_LABEL, e.labels)              # its own mid IV
        e.ivs[40] = None                                           # the quote went away: today's last valid IV
        a = m.breakdown([(40, "NRML", -75)], inst, price_of({1: SPOT}), DAY)
        self.assertNotIn(IV_FALLBACK_LABEL, e.labels)
        e.ivs[40] = 0.12
        self.assertEqual(m.breakdown([(40, "NRML", -75)], inst, price_of({1: SPOT}), DAY), a)
        e.ivs[40], e.ivs[41] = 0.12, None                          # the put has never had an IV: ATM IV (the call's)
        b = m.breakdown([(41, "NRML", -75)], inst, price_of({1: SPOT}), DAY)
        self.assertIn(IV_FALLBACK_LABEL, e.labels)
        m2, e2 = model([ce, pe], {40: None, 41: None})             # nothing at all: default_iv
        c = m2.breakdown([(41, "NRML", -75)], inst, price_of({1: SPOT}), DAY)
        self.assertIn(IV_FALLBACK_LABEL, e2.labels)
        self.assertNotEqual(b, c)                                  # 0.12 at ATM vs the 0.15 default

    def test_rows_by_underlying_and_only_nrml(self):
        ce = option(50, "CE", 25_000)
        bank = {"id": 60, "kind": "FUT", "underlying": "BANKNIFTY", "underlying_id": 5, "underlying_class": "INDEX",
                "expiry": EXPIRY, "actual_expiry": None, "strike_paise": None, "last_trading_ns": LAST}
        m, _ = model([ce, bank], {50: 0.12})
        inst = {50: ce, 60: bank}.__getitem__
        rows = m.breakdown([(60, "NRML", 30), (50, "NRML", -75), (50, "MIS", -75)], inst,
                           price_of({1: SPOT, 5: 5_600_000, 60: 5_600_000}), DAY)
        self.assertEqual([r.underlying_id for r in rows], [1, 5])
        self.assertTrue(all(x % 100 == 0 for r in rows for x in (r.scan, r.somc, r.elm, r.addons)))

    def test_a_missing_row_is_an_error(self):
        m, _ = model([FUT], {})
        m.rows = []
        with self.assertRaises(RuntimeError):
            m.breakdown([(2, "NRML", -75)], lambda i: FUT, price_of({1: SPOT, 2: SPOT}), DAY)



from tests.stockify.test_bundle import BundleTest  # noqa: E402  — the M2.1 offline refdata and bars


class SpanLiteBundleTests(BundleTest):
    def fut_run(self, **kw) -> dict:
        return self.resolve(margin={"model": "spanlite"}, data={"packs": [{"spec": {
            "dataset": "bars", "interval": "day", "instruments": [{"ids": [self.fut]}],
            "from": "2025-12-22", "to": "2025-12-31"}}]}, **kw)

    def with_rows(self, extra: list[dict]):  # noqa: ANN201
        """loader.load_json with extra spanlite rows in margin_params.json (as after an adoption)."""
        from unittest import mock
        from stockify.config import loader
        real = loader.load_json

        def load(name, config_dir=None):  # noqa: ANN001, ANN202
            doc = real(name, config_dir)
            if name == "margin_params.json":
                doc["spanlite"] = doc["spanlite"] + [dict(r) for r in extra]
            return doc
        return mock.patch.object(loader, "load_json", side_effect=load)

    def test_the_label_names_only_the_rows_the_run_can_use(self):
        """MEZ 4.3: a row naming the underlying replaces the class placeholder from its date; the label follows."""
        def nifty(day: str, cid: str) -> dict:
            return {"applies": {"underlying": "NIFTY"}, "effective_from": day, "psr_pct": "0.08", "vsr_pct": "0.3",
                    "extreme_multiple": 3, "extreme_weight": "0.35", "somc_per_lot_paise": 300000,
                    "elm_pct": "0.02", "default_iv": "0.15", "calibration_id": cid, "source": "calibration",
                    "verified": False}
        spanlite_labels = lambda b: [x for x in b["labels"] if x.startswith("margin_spanlite:")]  # noqa: E731
        for extra, want in (
                ([], ["margin_spanlite:placeholder"]),
                ([nifty("2025-01-01", "calibration-a")], ["margin_spanlite:calibration-a"]),       # only the fit
                ([nifty("2025-12-26", "calibration-a")], ["margin_spanlite:calibration-a+placeholder"]),  # mid-run
                ([nifty("2026-01-05", "calibration-a")], ["margin_spanlite:placeholder"]),          # not yet
                ([nifty("2025-01-01", "calibration-a"), nifty("2025-12-29", "calibration-b")],
                 ["margin_spanlite:calibration-a+calibration-b"]),
                ([dict(nifty("2025-01-01", "calibration-a"), applies={"underlying": "BANKNIFTY"})],
                 ["margin_spanlite:placeholder"])):                                               # another index
            with self.subTest(extra=[(r["applies"], r["effective_from"]) for r in extra]), self.with_rows(extra):
                b = self.fut_run()
                self.assertEqual(spanlite_labels(b), want)
        b = self.resolve(margin={"model": "spanlite"})                                  # RELIANCE only: no F&O
        self.assertEqual(spanlite_labels(b), ["margin_spanlite:none"])

    def test_a_spanlite_config_resolves_with_placeholder_rows_and_label(self):
        b = self.fut_run()
        self.assertEqual(b["run"]["margin"]["model"], "spanlite")
        self.assertIn("margin_spanlite:placeholder", b["labels"])
        self.assertIn("margin:spanlite(approx)", b["labels"])
        rows = b["margin_params"]["spanlite"]
        self.assertEqual({r["applies"]["underlying_class"] for r in rows}, {"INDEX", "STOCK"})
        for r in rows:
            self.assertFalse(r["verified"])
            self.assertIn("placeholder", r["source"])
            for f in ("psr_pct", "vsr_pct", "extreme_weight", "elm_pct", "default_iv"):
                self.assertEqual(r[f"{f}_ppb"], numerics.to_ppb(r[f]))
        flat = self.resolve()
        self.assertFalse(any(x.startswith("margin_spanlite") for x in flat["labels"]))


if __name__ == "__main__":
    unittest.main()
