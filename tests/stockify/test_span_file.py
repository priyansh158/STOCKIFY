"""MEZ 4.6: the SPAN 4.00 reader, against a trimmed cut of NSE's real file of 2026-11-09 (issue 1)."""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify.fno.span_file import SpanFile, SpanFileError, parse_span

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "span" / "nsccl.20261109.i1.trim.spn"


class SpanFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.span = parse_span(FIXTURE)

    def test_parses_created_and_nifty(self):
        self.assertEqual(self.span.created, "202611082026")
        self.assertEqual(self.span.business_date, "2026-11-09")
        self.assertFalse(self.span.is_settlement)
        self.assertEqual(sorted(self.span.commodities), ["BANKNIFTY", "NIFTY"])
        nifty = self.span.commodities["NIFTY"]
        self.assertEqual(nifty.code, "NIFTY")
        self.assertEqual(nifty.spot, "24986.400000")
        self.assertEqual(nifty.som_method, "GROSS")
        self.assertEqual(len(nifty.contracts), 2 + 40)  # two futures, two series of 20 options

    def test_contract_key_for_option_and_future(self):
        nifty = self.span.commodities["NIFTY"]
        ce = nifty.contracts[("CE", "2026-11-17", 2495000)]
        self.assertEqual((ce.price, ce.delta, ce.vol, ce.composite_delta),
                         ("308.80", "0.54800846", "0.1666", "0.5249"))
        pe = nifty.contracts[("PE", "2026-11-17", 2495000)]
        self.assertEqual(pe.price, "261.45")
        fut = nifty.contracts[("FUT", "2026-11-23", None)]
        self.assertEqual((fut.price, fut.delta, fut.vol, fut.composite_delta),
                         ("25029.00", "1.00000000", "0.17", "1.00000"))
        self.assertIn(("FUT", "2026-12-29", None), nifty.contracts)
        self.assertIn(("CE", "2026-11-23", 2495000), nifty.contracts)

    def test_risk_array_has_16_values_in_file_order(self):
        ce = self.span.commodities["NIFTY"].contracts[("CE", "2026-11-17", 2495000)]
        self.assertEqual(ce.risk_array, (
            "-19.03", "90.38", "-586.18", "-547.42", "243.98", "293.09", "-1320.70", "-1317.13",
            "298.28", "302.27", "-2091.82", "-2091.69", "302.22", "302.30", "-1545.40", "105.81"))
        self.assertEqual(ce.composite_delta, "0.5249")
        fut = self.span.commodities["NIFTY"].contracts[("FUT", "2026-11-23", None)]
        self.assertEqual(fut.risk_array, (
            "9.72", "9.72", "-766.81", "-766.81", "786.25", "786.25", "-1543.35", "-1543.35",
            "1562.79", "1562.79", "-2319.88", "-2319.88", "2339.32", "2339.32", "-1627.32", "1634.12"))
        self.assertEqual(fut.composite_delta, "1.00000")

    def test_underlying_filter_skips_others(self):
        only = parse_span(FIXTURE, {"BANKNIFTY"})
        self.assertEqual(list(only.commodities), ["BANKNIFTY"])
        self.assertEqual(only.created, "202611082026")
        self.assertEqual(parse_span(FIXTURE, {"RELIANCE"}).commodities, {})
        bank = only.commodities["BANKNIFTY"]
        self.assertEqual({k[1] for k in bank.contracts if k[0] != "FUT"}, {"2026-11-23"})

    def test_reads_zip_and_plain(self):
        with tempfile.TemporaryDirectory() as tmp:
            z = Path(tmp) / "nsccl.20261109.i1.zip"
            with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(FIXTURE, "nsccl.20261109.i01.spn")
            zipped = parse_span(z)
            self.assertEqual(str(parse_span(str(FIXTURE)).commodities["NIFTY"].contracts.keys()),
                             str(zipped.commodities["NIFTY"].contracts.keys()))
            self.assertEqual(zipped, self.span)
            bad = Path(tmp) / "empty.zip"
            with zipfile.ZipFile(bad, "w") as zf:
                zf.writestr("readme.txt", "x")
            with self.assertRaises(SpanFileError):
                parse_span(bad)

    def test_parameters_are_read_as_strings(self):
        nifty = self.span.commodities["NIFTY"]
        self.assertEqual(nifty.price_scan[("OOP", "2026-11-17")], "2323.74")
        self.assertEqual(nifty.vol_scan[("FUT", "2026-11-23")], "0.04")
        self.assertEqual(nifty.series[("OOP", "2026-11-17")].time_to_expiry, "0.024658")
        self.assertEqual(nifty.series[("FUT", "2026-11-23")].interest_rate, "0.07")
        self.assertEqual([(t.number, t.start, t.end) for t in nifty.scan_tiers],
                         [(1, "2026-11-09", "2026-11-09"), (2, "2026-11-17", "2031-06-24")])
        self.assertEqual([t.number for t in nifty.intra_tiers], [0, 1])
        self.assertEqual([t.number for t in nifty.inter_tiers], [1])
        self.assertEqual(nifty.som_tiers[0].rates, {"1": "0"})
        spread = nifty.calendar_spreads[0]
        self.assertEqual((spread.charge_method, spread.rates["1"]), ("F", "443.000000"))
        self.assertEqual([(l.expiry, l.side) for l in spread.legs], [("2026-11-17", "A"), ("2026-11-23", "B")])
        self.assertEqual(len(self.span.scan_points), 16)
        self.assertEqual(self.span.scan_points[3].price_numerator, "1.000000")

    def test_rejects_other_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.spn"
            p.write_text("<spanFile><fileFormat>3.00</fileFormat></spanFile>", encoding="utf-8")
            with self.assertRaises(SpanFileError):
                parse_span(p)
            p.write_text("<spanFile><fileFormat>4.00", encoding="utf-8")
            with self.assertRaises(SpanFileError):
                parse_span(p)


_A16 = "<a>1.00</a>" * 16


def _ra(values: str = _A16) -> str:
    return f"<ra><r>1</r>{values}<d>0.5</d></ra>"


def _fut(pe: str = "<pe>20261123</pe>", ra: str | None = None) -> str:
    return f"<fut><cId>1</cId>{pe}<p>25029.00</p><d>1.0</d><v>0.17</v>{ra or _ra()}</fut>"


def _opt(side: str = "C", ra: str | None = None, k: str = "24950.00") -> str:
    return f"<opt><cId>1</cId><o>{side}</o><k>{k}</k><p>1.00</p><d>0.5</d><v>0.16</v>{ra or _ra()}</opt>"


def _doc(portfolios: str, fmt: str = "<fileFormat>4.00</fileFormat>") -> str:
    return (f"<spanFile>{fmt}<created>202611082026</created><pointInTime><date>20261109</date>"
            f"<isSetl>0</isSetl><clearingOrg><exchange>{portfolios}</exchange>"
            "<ccDef><cc>NIFTY</cc><somMeth>GROSS</somMeth><pfLink><pfId>9</pfId><pfType>FUT</pfType></pfLink>"
            "</ccDef></clearingOrg></pointInTime></spanFile>")


def _fut_pf(*futs: str) -> str:
    return f"<futPf><pfId>9</pfId><pfCode>NIFTY</pfCode><cvf>1.00</cvf>{''.join(futs)}</futPf>"


def _oop_pf(*opts: str) -> str:
    return ("<oopPf><pfId>9</pfId><pfCode>NIFTY</pfCode><cvf>1.00</cvf><series><pe>20261117</pe>"
            f"{''.join(opts)}</series></oopPf>")


class SpanFileErrorPathTests(unittest.TestCase):
    def _fails(self, xml: str, *words: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.spn"
            p.write_text(xml, encoding="utf-8")
            with self.assertRaises(SpanFileError) as ctx:
                parse_span(p)
        for word in words:
            self.assertIn(word, str(ctx.exception))

    def _parses(self, xml: str) -> SpanFile:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.spn"
            p.write_text(xml, encoding="utf-8")
            return parse_span(p)

    def test_helper_documents_are_valid(self):
        """The error cases below differ from these by exactly one defect."""
        span = self._parses(_doc(_fut_pf(_fut())))
        self.assertIn(("FUT", "2026-11-23", None), span.commodities["NIFTY"].contracts)
        span = self._parses(_doc(_oop_pf(_opt("C"), _opt("P"))))
        self.assertEqual(len(span.commodities["NIFTY"].contracts), 2)

    def test_risk_array_with_fewer_than_16_values(self):
        self._fails(_doc(_fut_pf(_fut(ra=_ra("<a>1.00</a>" * 15)))), "risk array", "15", "16")

    def test_future_without_expiry(self):
        self._fails(_doc(_fut_pf(_fut(pe=""))), "lacks <pe>")

    def test_series_without_expiry(self):
        xml = _doc(_oop_pf(_opt()).replace("<pe>20261117</pe>", ""))
        self._fails(xml, "lacks <pe>")

    def test_duplicate_contract(self):
        self._fails(_doc(_fut_pf(_fut(), _fut())), "duplicate contract", "FUT")
        self._fails(_doc(_oop_pf(_opt("C"), _opt("C"))), "duplicate contract", "CE")

    def test_option_side_other_than_c_or_p(self):
        self._fails(_doc(_oop_pf(_opt("X"))), "neither C nor P")

    def test_missing_file_format(self):
        self._fails(_doc(_fut_pf(_fut()), fmt=""), "fileFormat")


if __name__ == "__main__":
    unittest.main()
