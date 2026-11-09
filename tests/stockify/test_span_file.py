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
        self.assertEqual(len(ce.risk_array), 16)
        self.assertTrue(all(isinstance(a, str) for a in ce.risk_array))
        self.assertEqual(ce.risk_array[:4], ("-19.03", "90.38", "-586.18", "-547.42"))
        self.assertEqual(ce.risk_array[-2:], ("-1545.40", "105.81"))
        fut = self.span.commodities["NIFTY"].contracts[("FUT", "2026-11-23", None)]
        self.assertEqual(fut.risk_array[0], "9.72")
        self.assertEqual(fut.risk_array[14:], ("-1627.32", "1634.12"))

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


if __name__ == "__main__":
    unittest.main()
