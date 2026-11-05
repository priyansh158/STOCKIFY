"""stockify.data.instruments: identity with history (M1.1, A3 G12/G13).

Fixture dumps (tests/fixtures/kite_instruments/):
  2025-12-29  December NIFTY contracts (lot 75), a March future, a RELIANCE future,
              a fractional-strike call, and one unknown instrument type (skipped).
  2025-12-31  The December contracts have expired. Token 9001 now belongs to a
              January 24600 call, and the March future's lot size changed 75 -> 65.
"""
from __future__ import annotations

import gzip
import hashlib
import unittest
from datetime import date
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import paths
from stockify.data import instruments, migrate, refdata

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "kite_instruments"
DAY1, DAY2 = date(2025, 12, 29), date(2025, 12, 31)
UNDERLYINGS = {"NIFTY": "NSE:NIFTY 50"}


def dumps(day: date) -> dict[str, str]:
    folder = FIXTURES / day.isoformat()
    return {ex: (folder / f"{ex}.csv").read_text(encoding="utf-8") for ex in ("NSE", "NFO")}


def snapshot() -> dict:
    conn = refdata._connect()
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
            for t in ("instruments", "instrument_tokens", "contract_specs", "index_underlyings")}


class InstrumentsTest(SandboxedDBTest):
    db_modules = (refdata,)

    def sync(self, day: date):
        return instruments.sync(day, dumps(day), underlyings=UNDERLYINGS)

    def deriv(self, kind: str, expiry: str, strike_paise=None) -> int:
        row = refdata._connect().execute(
            "SELECT instrument_id FROM instruments WHERE kind=? AND expiry=? "
            "AND IFNULL(strike_paise,-1)=IFNULL(?,-1) AND underlying='NIFTY'",
            (kind, expiry, strike_paise)).fetchone()
        self.assertIsNotNone(row, (kind, expiry, strike_paise))
        return row["instrument_id"]


class SchemaTests(InstrumentsTest):
    def test_migrations_create_every_a1_table(self):
        names = {r["name"] for r in refdata._connect().execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        for table in ("instruments", "instrument_tokens", "contract_specs", "index_underlyings",
                      "nse_fetch_log", "fo_eod", "cm_eod", "index_eod", "settlement_prices",
                      "freeze_limits", "daily_limits", "fo_universe", "tick_days", "data_quality"):
            self.assertIn(table, names)
        self.assertEqual(refdata.schema_version(), 1)

    def test_reopening_does_not_reapply(self):
        support.reset_sqlite_singleton(refdata)
        self.assertEqual(refdata.schema_version(), 1)

    def test_an_edited_applied_migration_is_refused(self):
        import shutil, tempfile
        base = Path(tempfile.mkdtemp(dir=str(support.SANDBOX_ROOT)))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        (base / "x").mkdir()
        (base / "x" / "0001_init.sql").write_text("CREATE TABLE a (b INTEGER);\n", encoding="utf-8")
        import sqlite3
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.assertEqual(migrate.apply(conn, "x", base), 1)
        (base / "x" / "0001_init.sql").write_text("CREATE TABLE a (c INTEGER);\n", encoding="utf-8")
        with self.assertRaises(migrate.MigrationError):
            migrate.apply(conn, "x", base)


class SyncTests(InstrumentsTest):
    def test_rows_are_classified_and_unknown_types_skipped(self):
        stats = self.sync(DAY1)
        self.assertEqual(stats.rows, 8)
        self.assertEqual(stats.skipped, 1)
        kinds = dict(refdata._connect().execute(
            "SELECT kind, COUNT(*) FROM instruments GROUP BY kind").fetchall())
        self.assertEqual(kinds, {"INDEX": 1, "EQ": 1, "CE": 2, "PE": 1, "FUT": 3})

    def test_strikes_and_ticks_are_exact_paise(self):
        self.sync(DAY1)
        self.deriv("CE", "2025-12-30", 2_450_000)
        self.deriv("CE", "2025-12-30", 105_250)          # "1052.5" -> 1,05,250 paise
        spec = instruments.spec_at(self.deriv("CE", "2025-12-30", 2_450_000), DAY1)
        self.assertEqual((spec["lot_size"], spec["tick_paise"]), (75, 5))

    def test_reused_token_resolves_by_date_g13(self):
        self.sync(DAY1)
        self.sync(DAY2)
        old = self.deriv("CE", "2025-12-30", 2_450_000)
        new = self.deriv("CE", "2026-01-06", 2_460_000)
        self.assertEqual(instruments.resolve_token(9001, DAY1), old)
        self.assertEqual(instruments.resolve_token(9001, DAY2), new)
        self.assertEqual(instruments.resolve_token(9001, date(2025, 12, 30)), old)
        self.assertIsNone(instruments.resolve_token(9001, date(2025, 1, 1)))

    def test_expired_contracts_lose_their_token_but_keep_history(self):
        self.sync(DAY1)
        self.sync(DAY2)
        put = self.deriv("PE", "2025-12-30", 2_450_000)
        self.assertEqual(instruments.token_at(put, DAY1), 9002)
        self.assertIsNone(instruments.token_at(put, DAY2))
        self.assertEqual(instruments.get(put)["last_seen"], "2025-12-29")

    def test_mid_life_lot_change_is_dated_g12(self):
        self.sync(DAY1)
        self.sync(DAY2)
        march = self.deriv("FUT", "2026-03-31")
        rows = refdata._connect().execute(
            "SELECT effective_from, lot_size FROM contract_specs WHERE instrument_id=? "
            "ORDER BY effective_from", (march,)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("2025-12-29", 75), ("2025-12-31", 65)])
        self.assertEqual(instruments.spec_at(march, date(2025, 12, 30))["lot_size"], 75)
        self.assertEqual(instruments.spec_at(march, DAY2)["lot_size"], 65)

    def test_syncing_the_same_dump_twice_changes_nothing(self):
        self.sync(DAY1)
        self.sync(DAY2)
        before = snapshot()
        stats = self.sync(DAY2)
        self.assertEqual(snapshot(), before)
        self.assertEqual((stats.new_instruments, stats.token_opened, stats.spec_rows), (0, 0, 0))

    def test_older_dump_after_newer_is_refused(self):
        self.sync(DAY2)
        with self.assertRaises(instruments.SyncError):
            self.sync(DAY1)

    def test_raw_dump_is_kept_exactly(self):
        self.sync(DAY1)
        raw = paths.nse_raw_dir("kite_instruments") / "20251229_NFO.csv.gz"
        text = dumps(DAY1)["NFO"].encode("utf-8")
        self.assertEqual(gzip.decompress(raw.read_bytes()), text)
        sidecar = raw.with_name(raw.name + ".sha256").read_text(encoding="utf-8").split()[0]
        self.assertEqual(sidecar, hashlib.sha256(text).hexdigest())

    def test_missing_column_is_an_error(self):
        with self.assertRaises(instruments.SyncError):
            instruments.parse_dump("instrument_token,tradingsymbol\n1,X\n")


class LookupTests(InstrumentsTest):
    def setUp(self):
        super().setUp()
        self.sync(DAY1)
        self.sync(DAY2)

    def test_find_by_symbol(self):
        self.assertEqual(instruments.find("NSE", "reliance"),
                         refdata._connect().execute(
                             "SELECT instrument_id FROM instruments WHERE kind='EQ'").fetchone()[0])
        self.assertIsNone(instruments.find("NSE", "NOPE"))

    def test_underlying_links(self):
        nifty = instruments.find("NSE", "NIFTY 50")
        reliance = instruments.find("NSE", "RELIANCE")
        self.assertEqual(instruments.underlying_of(self.deriv("FUT", "2026-03-31")), nifty)
        rel_fut = instruments.find("NFO", "RELIANCE26JANFUT")
        self.assertEqual(instruments.underlying_of(rel_fut), reliance)
        self.assertIsNone(instruments.underlying_of(nifty))

    def test_expiries_are_point_in_time(self):
        self.assertEqual(instruments.expiries("NIFTY", "OPT", DAY1), ["2025-12-30"])
        self.assertEqual(instruments.expiries("NIFTY", "OPT", DAY2), ["2026-01-06"])
        self.assertEqual(instruments.expiries("NIFTY", "FUT", DAY1), ["2025-12-30", "2026-03-31"])

    def test_option_chain_sorted_by_strike_then_kind(self):
        chain = instruments.option_chain("NIFTY", "2025-12-30", DAY1)
        self.assertEqual([(c["strike_paise"], c["kind"]) for c in chain],
                         [(105_250, "CE"), (2_450_000, "CE"), (2_450_000, "PE")])
        self.assertEqual(instruments.option_chain("NIFTY", "2026-01-06", DAY1), [])


if __name__ == "__main__":
    unittest.main()
