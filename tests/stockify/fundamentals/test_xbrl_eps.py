"""FS 13.5: the EPS × shares ≈ profit-to-owners check on real NSE filings (fixtures README, "EPS check filings")."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify.fundamentals import store, xbrl

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "fundamentals"


def cr(amount: str) -> int:
    """₹ `amount` crore in paise, exactly."""
    value = Decimal(amount) * 10**9
    assert value == value.to_integral_value()
    return int(value)


def parse(name: str, period_end: str, kind: str = "Q") -> xbrl.ParseResult:
    return xbrl.parse((FIX / name).read_bytes(), period_kind=kind, basis="consolidated", period_end=period_end)


def eps_problems(problems: list[str]) -> list[str]:
    return [p for p in problems if p.startswith("EPS times shares")]


class OwnersFiledAsZeroTests(SandboxedDBTest):
    db_modules = (store,)

    def test_owners_and_minority_both_zero_is_not_a_split(self):
        # KPRMILL files profit for the period ₹201.84 crore with owners and minority both 0.00: no split was filed,
        # so the whole profit is the owners' (it was read as ₹0.00 crore and the filing held).
        r = parse("xbrl_KPRMILL_2023-09-30_consolidated.xml", "2023-09-30")
        self.assertEqual(r.problems, [])
        self.assertEqual(r.fields["net_profit"], cr("201.84"))
        self.assertEqual(r.fields["profit_owners"], cr("201.84"))
        self.assertIn("filed as 0", r.raw["profit_owners"])

    def test_owners_zero_with_minority_takes_the_difference(self):
        # Owners 0 while minority holds part of a non-zero profit: owners = profit - minority, never 0 by mistake.
        data = (FIX / "xbrl_KPRMILL_2023-09-30_consolidated.xml").read_bytes()
        nci = b'<in-bse-fin:ProfitOrLossAttributableToNonControllingInterests contextRef="OneD" unitRef="INR" decimals="-5">0.00<'
        self.assertIn(nci, data)
        data = data.replace(nci, nci.replace(b">0.00<", b">18400000.00<"))
        r = xbrl.parse(data, period_kind="Q", basis="consolidated", period_end="2023-09-30")
        self.assertEqual(r.fields["profit_owners"], cr("200"))


class WeightedSharesGapTests(SandboxedDBTest):
    db_modules = (store,)

    def test_genuine_gaps_pass(self):
        # EPS uses weighted-average shares, paid-up / face value is period-end shares: a few per cent apart.
        for name, day, owners in (("xbrl_RKFORGE_2023-09-30_consolidated.xml", "2023-09-30", "82.1956"),
                                  ("xbrl_INDHOTEL_2022-03-31_consolidated.xml", "2022-03-31", "74.19")):
            with self.subTest(name=name):
                r = parse(name, day)
                self.assertEqual(eps_problems(r.problems), [])
                self.assertEqual(r.fields["profit_owners"], cr(owners))

    def test_scale_errors_still_hold(self):
        r = parse("xbrl_RKFORGE_2023-09-30_consolidated.xml", "2023-09-30")
        f = dict(r.fields)
        step = {"step_paise": r.step_paise}
        self.assertEqual(eps_problems(xbrl.check(f, period_kind="Q", aux=step)), [])
        for label, change in (("EPS 100x", {"eps_basic": f["eps_basic"] * 100}),
                              ("profit in lakhs read as crores", {"profit_owners": f["profit_owners"] * 100}),
                              ("profit 100x smaller", {"profit_owners": f["profit_owners"] // 100}),
                              ("EPS 1.5x", {"eps_basic": f["eps_basic"] * 3 // 2}),
                              ("profit 1.5x", {"profit_owners": f["profit_owners"] * 3 // 2})):
            with self.subTest(label):
                problems = eps_problems(xbrl.check(f | change, period_kind="Q", aux=step))
                self.assertEqual(len(problems), 1)
                self.assertTrue(problems[0].endswith("by more than 10 %"), problems[0])


if __name__ == "__main__":  # pragma: no cover
    import unittest
    unittest.main()
