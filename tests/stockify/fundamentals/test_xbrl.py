"""FS 3: the XBRL parser, its checks and the ingest of downloaded filings, on real NSE filings (fixtures README)."""
from __future__ import annotations

import hashlib
import re
import shutil
from decimal import Decimal
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import paths
from stockify.fundamentals import store, xbrl

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "fundamentals"

# Task 2 owns the filings table (migration 0002). Its DDL as the plan gives it, so this file runs whether or not
# that migration is present yet.
FILINGS_DDL = """CREATE TABLE IF NOT EXISTS filings (
  filing_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, period_end TEXT NOT NULL, period_kind TEXT NOT NULL,
  basis TEXT NOT NULL, broadcast_ns INTEGER, xbrl_url TEXT, xbrl_sha256 TEXT, xbrl_file TEXT,
  status TEXT NOT NULL, reason TEXT)"""


def cr(amount: str) -> int:
    """₹ `amount` crore in paise, exactly (1 crore = 10^7 rupees = 10^9 paise)."""
    value = Decimal(amount) * 10**9
    assert value == value.to_integral_value()
    return int(value)


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


RELIANCE_Q3 = "xbrl_RELIANCE_2024-12-31_consolidated.xml"


class ParseIndAsTests(SandboxedDBTest):
    db_modules = (store,)

    def test_indas_quarter_fields(self):
        r = xbrl.parse(fixture(RELIANCE_Q3), period_kind="Q", basis="consolidated", period_end="2024-12-31")
        self.assertEqual(r.problems, [])
        self.assertEqual(r.taxonomy, "indas")
        self.assertEqual(r.fields, {
            "revenue_ops": cr("243865"), "other_income": cr("4214"), "total_income": cr("248079"),
            "total_expenses": cr("219436"), "finance_costs": cr("6179"), "depreciation": cr("13181"),
            "profit_before_tax": cr("28643"), "tax": cr("6839"), "net_profit": cr("21930"),
            "profit_owners": cr("18540"), "eps_basic": 1370, "eps_diluted": 1370,
            "paid_up_equity": cr("13532"), "face_value": 1000,
        })
        self.assertEqual(r.raw["revenue_ops"], "RevenueFromOperations 2438650000000.00")
        self.assertEqual(set(r.fields) - set(xbrl.FIELDS), set())
        # Q3 has no balance sheet or cash flow; the year to date is nine months.
        self.assertEqual((r.period_end, r.fy_start, r.ytd_months), ("2024-12-31", "2024-04-01", 9))
        self.assertEqual(r.ytd["revenue_ops"], cr("715563"))

    def test_indas_quarter_standalone(self):
        r = xbrl.parse(fixture("xbrl_RELIANCE_2024-12-31_standalone.xml"), period_kind="Q", basis="standalone")
        self.assertEqual(r.problems, [])
        self.assertEqual(r.period_end, "2024-12-31")
        self.assertEqual((r.fields["revenue_ops"], r.fields["profit_before_tax"], r.fields["net_profit"]),
                         (cr("128260"), cr("11597"), cr("8721")))
        # Standalone accounts have no minority interests: all the profit belongs to the owners.
        self.assertEqual(r.fields["profit_owners"], r.fields["net_profit"])
        self.assertEqual(r.raw["profit_owners"], "= net_profit (standalone)")
        self.assertEqual(r.fields["eps_basic"], 644)

    def test_half_year_has_balance_sheet_and_cash_flow(self):
        r = xbrl.parse(fixture("xbrl_RELIANCE_2024-09-30_consolidated.xml"), period_kind="Q", basis="consolidated",
                       period_end="2024-09-30")
        self.assertEqual(r.problems, [])
        self.assertEqual(r.ytd_months, 6)
        f = r.fields
        self.assertEqual((f["total_equity"], f["total_assets"], f["cash"]), (cr("819453"), cr("1815123"), cr("90353")))
        self.assertEqual((f["borrowings_current"], f["borrowings_noncurrent"]), (cr("132409"), cr("203928")))
        self.assertEqual(f["operating_cash_flow"], cr("91810"))           # the six months to September
        self.assertEqual(r.raw["total_equity"], "EquityAttributableToOwnersOfParent 8194530000000.00")
        self.assertEqual(f["profit_owners"], cr("16563"))                   # the quarter, not the half year

    def test_annual_kind_reads_the_year(self):
        data = fixture("xbrl_INFY_2024-03-31_consolidated.xml")
        q = xbrl.parse(data, period_kind="Q", basis="consolidated", period_end="2024-03-31")
        a = xbrl.parse(data, period_kind="A", basis="consolidated", period_end="2024-03-31")
        self.assertEqual((q.problems, a.problems), ([], []))
        self.assertEqual((q.fields["revenue_ops"], q.fields["profit_owners"]), (cr("37923"), cr("7969")))
        self.assertEqual((a.fields["revenue_ops"], a.fields["profit_owners"]), (cr("153670"), cr("26233")))
        self.assertEqual(a.fields["operating_cash_flow"], cr("25210"))
        self.assertEqual((a.fields["borrowings_current"], a.fields["borrowings_noncurrent"]), (0, 0))
        self.assertEqual(a.fields["total_equity"], cr("88116"))

    def test_scale_lakhs_to_paise_exact(self):
        data = fixture("xbrl_VSTIND_2024-12-31_standalone.xml")
        self.assertIn(b">Lakhs<", data)                         # declared rounding: lakhs, values in rupees
        r = xbrl.parse(data, period_kind="Q", basis="standalone")
        self.assertEqual(r.problems, [])
        self.assertEqual(r.fields["revenue_ops"], 470_550_000_000)  # 4705500000.00 rupees (₹47,055 lakh)
        # Odd values convert exactly (no float on the way): 1,23,45,678.91 rupees.
        odd = data.replace(b'decimals="-5">4705500000.00<', b'decimals="-5">12345678.91<', 1)
        self.assertNotEqual(odd, data)
        r = xbrl.parse(odd, period_kind="Q", basis="standalone")
        self.assertEqual(r.fields.get("revenue_ops"), 1_234_567_891)
        # A fraction of a paisa cannot be stored exactly: held, not rounded.
        frac = data.replace(b'decimals="-5">4705500000.00<', b'decimals="-5">4705500000.005<', 1)
        r = xbrl.parse(frac, period_kind="Q", basis="standalone")
        self.assertNotIn("revenue_ops", r.fields)
        self.assertTrue(any("fraction of a paisa" in p for p in r.problems), r.problems)
        self.assertEqual(xbrl.to_paise("0.29"), 29)
        self.assertEqual(xbrl.to_paise("-50140000000.00"), -5_014_000_000_000)

    def test_prior_period_columns_ignored(self):
        data = fixture(RELIANCE_Q3)
        # NSE's year-to-date context (FourD) carries the quarter's dates; its nine-month figures must not leak in.
        r = xbrl.parse(data, period_kind="Q", basis="consolidated")
        self.assertEqual(r.fields["revenue_ops"], cr("243865"))
        # A prior-year comparative column (quarter and balance sheet a year earlier) is ignored.
        extra = (b'<xbrli:context id="TwoD"><xbrli:entity><xbrli:identifier scheme="http://www.nseindia.com/NSESymbol">'
                 b'RELIANCE</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>2023-10-01</xbrli:startDate>'
                 b'<xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period></xbrli:context>\n'
                 b'<xbrli:context id="TwoI"><xbrli:entity><xbrli:identifier scheme="http://www.nseindia.com/NSESymbol">'
                 b'RELIANCE</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2023-12-31</xbrli:instant>'
                 b'</xbrli:period></xbrli:context>\n'
                 b'<in-bse-fin:RevenueFromOperations contextRef="TwoD" unitRef="INR" decimals="-7">2400000000000.00'
                 b'</in-bse-fin:RevenueFromOperations>\n'
                 b'<in-bse-fin:Assets contextRef="TwoI" unitRef="INR" decimals="-7">17500000000000.00</in-bse-fin:Assets>\n')
        mixed = data.replace(b"</xbrli:xbrl>", extra + b"</xbrli:xbrl>")
        r2 = xbrl.parse(mixed, period_kind="Q", basis="consolidated")
        self.assertEqual(r2.problems, [])
        self.assertEqual(r2.fields, r.fields)
        self.assertNotIn("total_assets", r2.fields)

    def test_old_filings_with_undefined_contexts(self):
        # 2021 files refer to OneD / OneI without defining them; NSE's own column names still say which is which.
        r = xbrl.parse(fixture("xbrl_RELIANCE_2021-06-30_consolidated.xml"), period_kind="Q", basis="consolidated",
                       period_end="2021-06-30")
        self.assertEqual(r.problems, [])
        self.assertEqual((r.fields["revenue_ops"], r.fields["profit_owners"]), (cr("144372"), cr("12273")))
        b = xbrl.parse(fixture("xbrl_HDFCBANK_2021-06-30_standalone.xml"), period_kind="Q", basis="standalone",
                       period_end="2021-06-30")
        self.assertEqual(b.problems, [])
        self.assertEqual(b.taxonomy, "banking")
        self.assertEqual(b.fields["net_profit"], cr("7729.64"))

    def test_basis_and_period_must_match_the_listing(self):
        r = xbrl.parse(fixture("xbrl_RELIANCE_2024-12-31_standalone.xml"), period_kind="Q", basis="consolidated")
        self.assertTrue(any("standalone" in p and "consolidated" in p for p in r.problems), r.problems)
        r = xbrl.parse(fixture(RELIANCE_Q3), period_kind="Q", basis="consolidated", period_end="2024-09-30")
        self.assertTrue(any("2024-09-30" in p for p in r.problems), r.problems)
        self.assertEqual(r.fields, {})
        r = xbrl.parse(fixture(RELIANCE_Q3), period_kind="H", basis="consolidated")
        self.assertTrue(any("half year" in p for p in r.problems), r.problems)


class ParseIntegratedTests(SandboxedDBTest):
    db_modules = (store,)

    def test_integrated_filings(self):
        """2025 on: SEBI's in-capmkt taxonomy, one schemaRef for every format, told apart by the file's comment."""
        r = xbrl.parse(fixture("xbrl_RELIANCE_20260930_integrated_consolidated.xml"), period_kind="Q",
                       basis="consolidated")
        self.assertEqual((r.problems, r.taxonomy), ([], "indas"))
        self.assertIn("IFIndAs", r.format)
        f = r.fields
        self.assertEqual((f["revenue_ops"], f["net_profit"], f["profit_owners"], f["eps_basic"]),
                         (cr("332861"), cr("29550"), cr("25548"), 1888))
        # Its balance sheet is at 30-Sep-2026; the comparative column at 31-Mar-2026 (PY_I) is not read.
        self.assertEqual((f["total_assets"], f["total_equity"], f["cash"]), (cr("2275033"), cr("944404"), cr("181723")))
        self.assertEqual(f["operating_cash_flow"], cr("93055"))
        self.assertEqual(r.ytd_months, 6)

        b = xbrl.parse(fixture("xbrl_HDFCBANK_20260930_integrated_consolidated.xml"), period_kind="Q",
                       basis="consolidated")
        self.assertEqual((b.problems, b.taxonomy), ([], "banking"))
        self.assertEqual((b.fields["interest_earned"], b.fields["net_profit"]), (cr("95131.63"), cr("20700.03")))
        self.assertEqual(b.fields["total_equity"], cr("1541.86") + cr("601927.19"))

        n = xbrl.parse(fixture("xbrl_BAJFINANCE_20260930_integrated_consolidated.xml"), period_kind="Q",
                       basis="consolidated")
        self.assertEqual((n.problems, n.taxonomy), ([], "nbfc"))
        self.assertEqual((n.fields["interest_earned"], n.fields["profit_owners"]), (cr("21936.39"), cr("6266.20")))

    def test_integrated_without_marker_uses_the_file_name(self):
        data = fixture("xbrl_HDFCBANK_20260930_integrated_consolidated.xml")
        bare = re.sub(rb"<!--IF[^>]*-->", b"", data, count=1)
        self.assertNotEqual(bare, data)
        r = xbrl.parse(bare, period_kind="Q", basis="consolidated")
        self.assertEqual(r.taxonomy, "")
        self.assertIn("no map", r.problems[0])
        r = xbrl.parse(bare, period_kind="Q", basis="consolidated",
                       hint="INTEGRATED_FILING_BANKING_1740913_17102026054812_WEB.xml")
        self.assertEqual((r.problems, r.taxonomy), ([], "banking"))


class ParseFinancialsTests(SandboxedDBTest):
    db_modules = (store,)

    def test_bank_fields(self):
        r = xbrl.parse(fixture("xbrl_HDFCBANK_2024-12-31_standalone.xml"), period_kind="Q", basis="standalone")
        self.assertEqual(r.problems, [])
        self.assertEqual(r.taxonomy, "banking")
        f = r.fields
        self.assertNotIn("revenue_ops", f)
        self.assertEqual((f["interest_earned"], f["interest_expended"]), (cr("76006.88"), cr("45353.63")))
        self.assertEqual((f["other_income"], f["total_income"]), (cr("11453.56"), cr("87460.44")))
        self.assertEqual((f["profit_before_tax"], f["tax"], f["net_profit"]),
                         (cr("21846.55"), cr("5111.05"), cr("16735.50")))
        self.assertEqual((f["eps_basic"], f["eps_diluted"], f["face_value"]), (2190, 2180, 100))
        self.assertEqual(f["paid_up_equity"], cr("764.83"))

        # A bank's half year: the balance sheet in its own format. SBI files "Assets" without a unit, so the
        # total comes from the equal "capital and liabilities" line.
        s = xbrl.parse(fixture("xbrl_SBIN_2024-09-30_standalone.xml"), period_kind="Q", basis="standalone")
        self.assertEqual(s.problems, [])
        self.assertEqual(s.fields["total_assets"], cr("6341459.34"))
        self.assertEqual(s.raw["total_assets"], "CapitalAndLiabilities 63414593400000.00")
        self.assertEqual(s.fields["total_equity"], cr("892.46") + cr("418666.86"))
        self.assertEqual(s.fields["cash"], cr("264027.53") + cr("72073.93"))
        self.assertEqual(s.fields["operating_cash_flow"], cr("35960.16"))
        self.assertEqual(s.fields["net_profit"], cr("18331.44"))

    def test_nbfc_fields(self):
        r = xbrl.parse(fixture("xbrl_BAJFINANCE_2024-12-31_consolidated.xml"), period_kind="Q", basis="consolidated")
        self.assertEqual(r.problems, [])
        self.assertEqual(r.taxonomy, "nbfc")
        f = r.fields
        self.assertEqual((f["interest_earned"], f["revenue_ops"]), (cr("15768.21"), cr("18035.11")))
        self.assertEqual((f["interest_expended"], f["finance_costs"]), (cr("6385.63"), cr("6385.63")))
        self.assertEqual((f["net_profit"], f["profit_owners"]), (cr("4308.19"), cr("4246.54")))
        # December: the balance sheet lines are filed as zeros, which means "not filed", never zero assets.
        self.assertNotIn("total_assets", f)
        self.assertNotIn("total_equity", f)
        h = xbrl.parse(fixture("xbrl_BAJFINANCE_2024-09-30_consolidated.xml"), period_kind="Q", basis="consolidated")
        self.assertEqual(h.problems, [])
        self.assertEqual(h.fields["total_assets"], 4_206_557_600_000 * 100)
        self.assertIn("total_equity", h.fields)


class ChecksTests(SandboxedDBTest):
    db_modules = (store,)

    def base(self) -> dict:
        return {"revenue_ops": cr("1000"), "other_income": cr("10"), "total_income": cr("1010"),
                "profit_before_tax": cr("200"), "tax": cr("50"), "net_profit": cr("150"), "profit_owners": cr("150"),
                "eps_basic": 1500, "paid_up_equity": cr("100"), "face_value": 1000}

    def test_clean_fields_pass(self):
        self.assertEqual(xbrl.check(self.base(), period_kind="Q"), [])

    def test_check_messages(self):
        f = self.base() | {"net_profit": cr("170")}
        self.assertEqual(xbrl.check(f, period_kind="Q"),
                         ["profit after tax (₹170.00 crore) differs from profit before tax minus tax "
                          "(₹150.00 crore) by more than 1 %"])
        f = self.base() | {"eps_basic": 1700}
        self.assertEqual(xbrl.check(f, period_kind="Q"),
                         ["EPS times shares (₹170.00 crore) differs from profit attributable to owners "
                          "(₹150.00 crore) by more than 2 %"])
        f = self.base()
        del f["revenue_ops"]
        problems = xbrl.check(f, period_kind="Q")
        self.assertIn("no revenue from operations (or interest earned) for the filing's own period", problems)
        f = self.base()
        del f["profit_before_tax"], f["net_profit"]
        problems = xbrl.check(f, period_kind="Q")
        self.assertIn("no profit before tax for the filing's own period", problems)
        self.assertIn("no net profit for the filing's own period", problems)
        self.assertEqual(xbrl.check(self.base(), period_kind="H"),
                         ["a half-year filing without a balance sheet (total equity and total assets)"])
        f = self.base() | {"total_income": cr("1100")}
        self.assertEqual(xbrl.check(f, period_kind="Q"),
                         ["total income (₹1,100.00 crore) differs from revenue plus other income "
                          "(₹1,010.00 crore) by more than 1 %"])

    def test_rounding_slack(self):
        # Within one rounding step of the filing (₹1 crore here) a small total still passes.
        f = self.base() | {"profit_before_tax": cr("2"), "tax": cr("1"), "net_profit": cr("2"), "profit_owners": cr("2"),
                           "eps_basic": 20}
        self.assertEqual(xbrl.check(f, period_kind="Q", aux={"step_paise": cr("1")}), [])
        self.assertNotEqual(xbrl.check(f, period_kind="Q", aux={"step_paise": cr("0.01")}), [])


def _put_filing(filing_id: str, fixture_name: str | None, *, symbol: str, period_end: str, kind: str = "Q",
                basis: str = "consolidated", status: str = "downloaded", broadcast_ns: int = 1,
                data: bytes | None = None) -> None:
    conn = store._connect()
    rel = None
    digest = None
    if fixture_name is not None or data is not None:
        body = data if data is not None else fixture(fixture_name)
        rel = f"{period_end[:4]}/{symbol}_{period_end}_{basis}_{filing_id[:8]}.xml"
        target = paths.fundamentals_raw_dir("xbrl") / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
    conn.execute("INSERT INTO filings (filing_id, symbol, period_end, period_kind, basis, broadcast_ns, xbrl_url,"
                 " xbrl_sha256, xbrl_file, status, reason) VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                 (filing_id, symbol, period_end, kind, basis, broadcast_ns, "https://nsearchives.nseindia.in/x.xml",
                  digest, rel, status))


class IngestTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        store._connect().execute(FILINGS_DDL)

    def status(self, filing_id: str) -> tuple:
        row = store._connect().execute("SELECT status, reason FROM filings WHERE filing_id = ?", (filing_id,)).fetchone()
        return row["status"], row["reason"]

    def figures(self, filing_id: str) -> dict:
        rows = store._connect().execute("SELECT field, value_paise, value_raw FROM figures WHERE filing_id = ?",
                                        (filing_id,)).fetchall()
        return {r["field"]: (r["value_paise"], r["value_raw"]) for r in rows}

    def test_figures_table(self):
        cols = [r[1] for r in store._connect().execute("PRAGMA table_info(figures)")]
        self.assertEqual(cols, ["filing_id", "field", "value_paise", "value_raw"])

    def test_clean_filing_is_parsed(self):
        _put_filing("a" * 64, RELIANCE_Q3, symbol="RELIANCE", period_end="2024-12-31")
        _put_filing("b" * 64, "xbrl_HDFCBANK_2024-12-31_standalone.xml", symbol="HDFCBANK", period_end="2024-12-31",
                    basis="standalone")
        _put_filing("c" * 64, None, symbol="INFY", period_end="2024-12-31", status="listed")
        self.assertEqual(xbrl.ingest_pending(), {"parsed": 2, "held": 0})
        self.assertEqual(self.status("a" * 64), ("parsed", None))
        figs = self.figures("a" * 64)
        self.assertEqual(figs["revenue_ops"], (cr("243865"), "RevenueFromOperations 2438650000000.00"))
        self.assertEqual(figs["eps_basic"], (1370, "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations 13.70"))
        self.assertEqual(self.figures("b" * 64)["interest_earned"][0], cr("76006.88"))
        self.assertEqual(self.status("c" * 64), ("listed", None))
        self.assertEqual(xbrl.ingest_pending(), {"parsed": 0, "held": 0})

    def test_broken_filing_is_held_with_reason(self):
        broken = "xbrl_RELIANCE_2024-12-31_standalone_broken.xml"
        r = xbrl.parse(fixture(broken), period_kind="Q", basis="standalone")
        self.assertEqual(r.problems, ["profit after tax (₹8,721.00 crore) differs from profit before tax minus tax "
                                      "(₹9,721.00 crore) by more than 1 %"])
        _put_filing("1" * 64, broken, symbol="RELIANCE", period_end="2024-12-31", basis="standalone")
        _put_filing("2" * 64, None, symbol="RELIANCE", period_end="2024-09-30", basis="standalone",
                    data=fixture(RELIANCE_Q3)[:5000])
        _put_filing("3" * 64, None, symbol="INFY", period_end="2024-12-31")          # no file on disk
        conn = store._connect()
        conn.execute("UPDATE filings SET xbrl_file = '2024/missing.xml' WHERE filing_id = ?", ("3" * 64,))
        _put_filing("4" * 64, RELIANCE_Q3, symbol="RELIANCE", period_end="2024-12-31")
        conn.execute("UPDATE filings SET xbrl_sha256 = ? WHERE filing_id = ?", ("0" * 64, "4" * 64))
        _put_filing("5" * 64, None, symbol="X", period_end="2024-12-31", data=b"")
        conn.execute("UPDATE filings SET xbrl_file = '../../escape.xml' WHERE filing_id = ?", ("5" * 64,))
        self.assertEqual(xbrl.ingest_pending(), {"parsed": 0, "held": 5})
        status, reason = self.status("1" * 64)
        self.assertEqual(status, "held")
        self.assertIn("profit after tax", reason)
        self.assertEqual(self.figures("1" * 64), {})
        self.assertIn("not readable XML", self.status("2" * 64)[1])
        self.assertIn("XBRL file is missing", self.status("3" * 64)[1])
        self.assertIn("does not match its recorded sha256", self.status("4" * 64)[1])
        self.assertIn("outside the raw XBRL folder", self.status("5" * 64)[1])

    def test_unknown_taxonomy_is_held(self):
        data = fixture(RELIANCE_Q3).replace(b"Ind-AS_entry_point_2020-03-31.xsd", b"Insurance_entry_point_2021-03-31.xsd")
        r = xbrl.parse(data, period_kind="Q", basis="consolidated")
        self.assertEqual(r.taxonomy, "")
        self.assertEqual(r.fields, {})
        self.assertEqual(r.problems, ["the filing is in a format this parser has no map for "
                                      "(Insurance_entry_point_2021-03-31.xsd)"])
        _put_filing("e" * 64, None, symbol="SBILIFE", period_end="2024-12-31", data=data)
        self.assertEqual(xbrl.ingest_pending(), {"parsed": 0, "held": 1})
        self.assertEqual(self.status("e" * 64),
                         ("held", "the filing is in a format this parser has no map for "
                                  "(Insurance_entry_point_2021-03-31.xsd)"))

    def test_doctype_is_refused(self):
        r = xbrl.parse(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>', period_kind="Q",
                       basis="consolidated")
        self.assertEqual(r.problems, ["the XBRL file declares a DOCTYPE, which NSE filings never do"])

    def test_quarters_must_add_up_to_the_year_to_date(self):
        data = fixture("xbrl_RELIANCE_2024-09-30_consolidated.xml")
        h = xbrl.parse(data, period_kind="Q", basis="consolidated")
        q1 = {k: h.ytd[k] - h.fields[k] for k in h.ytd}                       # what Q1 must have been
        conn = store._connect()

        def put_q1(filing_id: str, values: dict) -> None:
            _put_filing(filing_id, None, symbol="RELIANCE", period_end="2024-06-30", status="parsed", broadcast_ns=1)
            conn.executemany("INSERT INTO figures (filing_id, field, value_paise, value_raw) VALUES (?,?,?,?)",
                             [(filing_id, k, v, "test") for k, v in values.items()])

        put_q1("q" * 64, q1)
        _put_filing("s" * 64, None, symbol="RELIANCE", period_end="2024-09-30", broadcast_ns=2, data=data)
        self.assertEqual(xbrl.ingest_pending(), {"parsed": 1, "held": 0})

        # Same filing for another company whose Q1 on record is ₹5,000 crore short of the half year.
        conn.execute("UPDATE filings SET symbol = 'OTHER' WHERE filing_id = ?", ("q" * 64,))
        conn.execute("UPDATE figures SET value_paise = value_paise - ? WHERE filing_id = ? AND field = 'revenue_ops'",
                     (cr("5000"), "q" * 64))
        _put_filing("t" * 64, None, symbol="OTHER", period_end="2024-09-30", broadcast_ns=3, data=data)
        self.assertEqual(xbrl.ingest_pending(), {"parsed": 0, "held": 1})
        status, reason = self.status("t" * 64)
        self.assertEqual(status, "held")
        self.assertTrue(re.search(r"quarters .* do not add up: revenue from operations ₹4,66,698.00 crore", reason), reason)

        # Without the earlier quarter on record the check cannot run, and does not hold the filing.
        _put_filing("u" * 64, None, symbol="THIRD", period_end="2024-09-30", broadcast_ns=4, data=data)
        self.assertEqual(xbrl.ingest_pending(), {"parsed": 1, "held": 0})


class MapSourcesTests(SandboxedDBTest):
    db_modules = (store,)

    def test_every_fixture_parses_clean(self):
        cases = {
            "xbrl_RELIANCE_2024-12-31_consolidated.xml": ("consolidated", "indas"),
            "xbrl_RELIANCE_2024-12-31_standalone.xml": ("standalone", "indas"),
            "xbrl_RELIANCE_2024-09-30_consolidated.xml": ("consolidated", "indas"),
            "xbrl_RELIANCE_2021-06-30_consolidated.xml": ("consolidated", "indas"),
            "xbrl_INFY_2024-03-31_consolidated.xml": ("consolidated", "indas"),
            "xbrl_VSTIND_2024-12-31_standalone.xml": ("standalone", "indas"),
            "xbrl_HDFCBANK_2024-12-31_standalone.xml": ("standalone", "banking"),
            "xbrl_HDFCBANK_2021-06-30_standalone.xml": ("standalone", "banking"),
            "xbrl_SBIN_2024-09-30_standalone.xml": ("standalone", "banking"),
            "xbrl_BAJFINANCE_2024-12-31_consolidated.xml": ("consolidated", "nbfc"),
            "xbrl_BAJFINANCE_2024-09-30_consolidated.xml": ("consolidated", "nbfc"),
            "xbrl_RELIANCE_20260930_integrated_consolidated.xml": ("consolidated", "indas"),
            "xbrl_HDFCBANK_20260930_integrated_consolidated.xml": ("consolidated", "banking"),
            "xbrl_BAJFINANCE_20260930_integrated_consolidated.xml": ("consolidated", "nbfc"),
        }
        names = {p.name for p in FIX.glob("xbrl_*.xml")} - {"xbrl_RELIANCE_2024-12-31_standalone_broken.xml"}
        self.assertEqual(names, set(cases))
        for name, (basis, family) in cases.items():
            with self.subTest(name=name):
                day = name.split("_")[2]
                day = day if "-" in day else f"{day[:4]}-{day[4:6]}-{day[6:]}"
                r = xbrl.parse(fixture(name), period_kind="Q", basis=basis, period_end=day)
                self.assertEqual((r.problems, r.taxonomy), ([], family))
                self.assertLessEqual(set(r.fields), set(xbrl.FIELDS))

    def test_fixture_hashes_match_readme(self):
        readme = (FIX / "README.md").read_text(encoding="utf-8")
        missing = [p.name for p in FIX.glob("xbrl_*.xml") if hashlib.sha256(p.read_bytes()).hexdigest() not in readme]
        self.assertEqual(missing, [])

    def test_taxonomy_map_names_only_known_fields(self):
        tax = xbrl.taxonomy()
        self.assertEqual(set(tax["families"]), {"indas", "banking", "nbfc"})
        for family, spec in tax["families"].items():
            with self.subTest(family=family):
                self.assertLessEqual(set(spec["fields"]), set(xbrl.FIELDS))
                for name in ("profit_before_tax", "net_profit", "eps_basic", "paid_up_equity", "face_value"):
                    self.assertIn(name, spec["fields"])
        self.assertIn("revenue_ops", tax["families"]["indas"]["fields"])
        self.assertIn("interest_earned", tax["families"]["banking"]["fields"])


if __name__ == "__main__":  # pragma: no cover
    import unittest
    unittest.main()
