"""MEZ 4.7: exact SPAN from NSE's risk arrays (oracle), against the trimmed real file of 2026-11-09 (issue 1).

Every expected value below is worked by hand from the fixture's numbers (quoted in the comments). Units: risk-array
values and prices are rupees per unit (one NIFTY unit, cvf 1.00); positions are signed units (NIFTY lot = 65).
"""
from __future__ import annotations

import dataclasses
import unittest
from decimal import Decimal
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify.fno.span_exact import SpanCalcError, SpanMissingError, SpanResult, span_paise, span_requirement
from stockify.fno.span_file import CombinedCommodity, Tier, parse_span

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "span" / "nsccl.20261109.i1.trim.spn"

FUT_NOV = ("FUT", "2026-11-23", None)
FUT_DEC = ("FUT", "2026-12-29", None)
CE_25000 = ("CE", "2026-11-23", 2500000)
PE_25000 = ("PE", "2026-11-23", 2500000)
LOT = 65


def _result(**kw) -> SpanResult:
    base = dict(scan=Decimal(0), intra_spread=Decimal(0), inter_credit=Decimal(0), som=Decimal(0),
                long_option_value=Decimal(0), short_option_value=Decimal(0), span=Decimal(0), missing=[])
    base.update(kw)
    return SpanResult(**base)


class SpanExactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nifty = parse_span(FIXTURE, {"NIFTY"}).commodities["NIFTY"]

    def test_scan_is_worst_of_16_scenarios_for_one_future(self):
        # 23-Nov future ra: 9.72 9.72 -766.81 -766.81 786.25 786.25 -1543.35 -1543.35 1562.79 1562.79 -2319.88
        # -2319.88 2339.32 2339.32 -1627.32 1634.12 (rupees lost per unit long). Worst = 2339.32 (scenarios 13/14,
        # price down one full scan range). Long 65 units: 2339.32 x 65 = 152,055.80. No options, one expiry, no
        # spread, SOM 0 -> span 152,055.80.
        r = span_requirement(self.nifty, [(FUT_NOV, LOT)])
        self.assertEqual(r.missing, [])
        self.assertEqual(r.scan, Decimal("152055.80"))
        self.assertEqual((r.intra_spread, r.inter_credit, r.som), (0, 0, 0))
        self.assertEqual((r.long_option_value, r.short_option_value), (0, 0))
        self.assertEqual(r.span, Decimal("152055.80"))
        # Short 65: worst = -(-2319.88) = 2319.88 (scenario 11/12, price up) x 65 = 150,792.20.
        self.assertEqual(span_requirement(self.nifty, [(FUT_NOV, -LOT)]).scan, Decimal("150792.20"))

    def test_short_straddle_scan_and_som(self):
        # Short 65 CE + 65 PE, 25000, 23-Nov. Per unit of the straddle the loss in scenario s is -(CE[s] + PE[s]):
        #  s1 103.35  s2 -196.67  s3 373.53  s4 198.15  s5 299.35  s6 112.27  s7 971.64  s8 926.07
        #  s9 870.34  s10 844.65  s11 1705.65  s12 1698.87  s13 1620.52  s14 1618.37  s15 1407.91  s16 1373.36
        # (s11: CE -2009.86, PE 304.21 -> 1705.65). Worst 1705.65 x 65 = 110,867.25.
        # Short option value: (348.40 + 326.60) x 65 = 43,875.00. SOM: NIFTY's somTiers rate is 0 -> 0.
        # span = max(110,867.25 + 0 - 0 - 0 + 43,875.00, 0) = 154,742.25.
        r = span_requirement(self.nifty, [(CE_25000, -LOT), (PE_25000, -LOT)])
        self.assertEqual(r.scan, Decimal("110867.25"))
        self.assertEqual(r.short_option_value, Decimal("43875.00"))
        self.assertEqual(r.long_option_value, 0)
        self.assertEqual(r.som, 0)
        self.assertEqual(r.span, Decimal("154742.25"))

    def test_som_rate_applies_per_short_unit_and_binds_when_larger(self):
        # The real NIFTY rate is 0 (finding 1); a made-up rate shows the composition. GROSS: every short option
        # unit counts. Rate 2000 rupees/unit x 130 short units = 260,000 > 154,742.25 -> span = som.
        cc = dataclasses.replace(self.nifty, som_tiers=(Tier(0, None, None, {"1": "2000"}),))
        r = span_requirement(cc, [(CE_25000, -LOT), (PE_25000, -LOT)])
        self.assertEqual(r.som, Decimal("260000"))
        self.assertEqual(r.span, Decimal("260000"))
        # A long option or a future adds nothing to a gross SOM.
        r = span_requirement(cc, [(CE_25000, -LOT), (PE_25000, LOT), (FUT_NOV, LOT)])
        self.assertEqual(r.som, Decimal("130000"))

    def test_long_option_value_credit(self):
        # Long 65 CE 25000 23-Nov, price 348.40. Worst loss is 348.40 per unit (s10, s13, s14: the option goes
        # to zero) -> scan 22,646.00; long option value 348.40 x 65 = 22,646.00; span = max(22,646 - 22,646, 0) = 0.
        r = span_requirement(self.nifty, [(CE_25000, LOT)])
        self.assertEqual(r.scan, Decimal("22646.00"))
        self.assertEqual(r.long_option_value, Decimal("22646.00"))
        self.assertEqual(r.span, 0)
        # A long straddle: the credit exceeds the scan, floored at 0.
        r = span_requirement(self.nifty, [(CE_25000, LOT), (PE_25000, LOT)])
        self.assertEqual(r.long_option_value, Decimal("43875.00"))
        self.assertEqual(r.span, 0)
        self.assertEqual(span_paise(r), 0)

    def test_calendar_spread_charge(self):
        # Long 65 Nov future, short 65 Dec future. Per unit loss Nov[s] - Dec[s]: s1 -0.07, s3 5.38, s5 -5.51,
        # s7 10.81, s9 -10.95, s11 16.26, s13 -16.39, s15 11.40, s16 -11.45 -> worst 16.26 x 65 = 1,056.90.
        # Deltas: 23-Nov +65, 29-Dec -65. Spreads in priority order: 1 (17-Nov/23-Nov) and 4 (17-Nov/29-Dec)
        # find no 17-Nov delta; 7 (23-Nov A / 29-Dec B, weights 1) forms 65 spreads x 443.00 = 28,795.00.
        # span = 1,056.90 + 28,795.00 = 29,851.90.
        r = span_requirement(self.nifty, [(FUT_NOV, LOT), (FUT_DEC, -LOT)])
        self.assertEqual(r.scan, Decimal("1056.90"))
        self.assertEqual(r.intra_spread, Decimal("28795.000000"))
        self.assertEqual(r.span, Decimal("29851.900000"))
        self.assertEqual(span_paise(r), 2_985_200)

    def test_same_side_deltas_form_no_spread(self):
        r = span_requirement(self.nifty, [(FUT_NOV, LOT), (FUT_DEC, LOT)])
        self.assertEqual(r.intra_spread, 0)

    def test_option_delta_spreads_by_composite_delta(self):
        # Long 100 CE 25000 23-Nov (composite delta 0.5181) vs short 100 Dec future (delta 1.00000):
        # 23-Nov delta +51.81, 29-Dec delta -100 -> spread 7 forms min(51.81, 100) = 51.81 spreads x 443 = 22,951.83.
        r = span_requirement(self.nifty, [(CE_25000, 100), (FUT_DEC, -100)])
        self.assertEqual(r.intra_spread, Decimal("22951.83"))

    def test_missing_contract_listed_not_guessed(self):
        absent = ("CE", "2026-11-17", 2000000)  # strike 20000: not in the trimmed file
        r = span_requirement(self.nifty, [(FUT_NOV, LOT), (absent, -LOT)])
        self.assertEqual(r.missing, [absent])
        self.assertTrue(r.span.is_nan())  # never a number that could pass for a margin
        with self.assertRaises(SpanMissingError):
            span_paise(r)

    def test_empty_commodity_is_missing_never_zero(self):
        # Finding 2: a wanted underlying whose commodity has no contracts (e.g. a pfCode mismatch).
        r = span_requirement(CombinedCommodity(code="NIFTY"), [(FUT_NOV, LOT)])
        self.assertEqual(r.missing, [FUT_NOV])
        with self.assertRaises(SpanMissingError):
            span_paise(r)

    def test_legs_net_by_contract(self):
        r = span_requirement(self.nifty, [(FUT_NOV, 100), (FUT_NOV, -35)])
        self.assertEqual(r.scan, Decimal("152055.80"))
        self.assertEqual(span_requirement(self.nifty, []).span, 0)

    def test_unknown_som_method_refused(self):
        cc = dataclasses.replace(self.nifty, som_method="NET")
        with self.assertRaises(SpanCalcError):
            span_requirement(cc, [(CE_25000, -LOT)])

    def test_contract_outside_every_scan_tier_refused(self):
        cc = dataclasses.replace(self.nifty, scan_tiers=(Tier(1, "2027-01-01", "2027-12-31", {}),))
        with self.assertRaises(SpanCalcError):
            span_requirement(cc, [(FUT_NOV, LOT)])

    def test_span_paise_rounds_up_to_rupee(self):
        # 152,055.80 rupees -> 152,056 rupees -> 15,205,600 paise; a whole rupee stays as it is.
        self.assertEqual(span_paise(_result(span=Decimal("152055.80"))), 15_205_600)
        self.assertEqual(span_paise(_result(span=Decimal("152055.01"))), 15_205_600)
        self.assertEqual(span_paise(_result(span=Decimal("152055"))), 15_205_500)
        self.assertEqual(span_paise(_result(span=Decimal("0"))), 0)


if __name__ == "__main__":
    unittest.main()
