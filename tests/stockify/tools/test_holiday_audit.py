"""The calendar audit (D-75): weekdays with no NSE data against the holiday list, on a small lab built here.

The shipped holiday list is the calendar under test: 2009-01-08 (a Thursday, Moharram) is in it, the other
weekdays of 2009-01-05 .. 2009-01-16 are trading days.
"""
from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import __main__ as cli
from stockify import paths, timeutil
from stockify.data import calendar as cal
from stockify.data import nse_margin_files as M
from stockify.tools import holiday_audit as H

START, END = date(2009, 1, 5), date(2009, 1, 16)          # Monday .. Friday of the next week
HOLIDAY = date(2009, 1, 8)
SATURDAY = date(2009, 1, 10)
WEEKDAYS = [START + timedelta(days=i) for i in range(12) if (START + timedelta(days=i)).weekday() < 5]
TRADING = [d for d in WEEKDAYS if d != HOLIDAY]


def put_span(day: date, suffix: str = "s") -> None:
    """An archived SPAN file of `day`: the audit only asks whether it is there."""
    folder = paths.nse_margin_dir("span") / f"{day:%Y}"
    folder.mkdir(exist_ok=True)
    (folder / M.span_name(day, suffix)).write_bytes(b"PK")


def put_bar(day: date, *, wal: bool = False) -> None:
    conn = sqlite3.connect(str(paths.bars_db()))
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS bars (instrument_id INTEGER, interval TEXT, ts_ns INTEGER, "
                 "close_paise INTEGER, PRIMARY KEY (instrument_id, interval, ts_ns))")
    conn.execute("INSERT INTO bars VALUES (1, 'day', ?, 100)", (timeutil.ist_ns(day, "00:00"),))
    conn.commit()
    conn.close()


def put_eod(table: str, day: date) -> None:
    conn = sqlite3.connect(str(paths.refdata_db()))
    for name in ("fo_eod", "cm_eod", "index_eod"):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {name} (trade_date TEXT, instrument_id INTEGER)")
    conn.execute(f"INSERT INTO {table} VALUES (?, 1)", (day.isoformat(),))
    conn.commit()
    conn.close()


def put_raw(dataset: str, day: date) -> None:
    (paths.nse_raw_dir(dataset) / f"{day:%Y%m%d}.csv").write_bytes(b"x")


def tree() -> list[tuple[str, int, int]]:
    """Every file and folder under the lab with its size and modification time."""
    lab = paths.lab_dir()
    return sorted((str(p.relative_to(lab)), p.stat().st_size if p.is_file() else -1,
                   p.stat().st_mtime_ns if p.is_file() else -1) for p in lab.rglob("*"))


class HolidayAuditTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        support.assert_sandboxed(paths.lab_dir())
        cal.reload()

    def test_a_trading_weekday_without_data_is_reported(self):
        silent = date(2009, 1, 14)
        for d in TRADING:
            if d != silent:
                put_span(d)
        got = H.audit(START, END)
        self.assertEqual(got.silent_trading_days, (silent,))
        self.assertEqual(got.holidays_with_data, ())
        self.assertEqual((got.weekdays, got.with_data, got.holidays), (10, 8, 1))

    def test_a_holiday_with_data_is_reported_with_its_name_and_store(self):
        for d in WEEKDAYS:
            put_span(d)
        got = H.audit(START, END)
        self.assertEqual(got.silent_trading_days, ())
        self.assertEqual(got.holidays_with_data, ((HOLIDAY, "Moharram", ("span",)),))

    def test_weekends_are_ignored(self):
        for d in TRADING:
            put_span(d)
        put_span(SATURDAY)                                 # a weekend file is not the audit's business
        got = H.audit(START, END)
        self.assertEqual((got.silent_trading_days, got.holidays_with_data), ((), ()))
        self.assertEqual(got.weekdays, 10)

    def test_only_the_end_of_day_span_file_counts(self):
        put_span(date(2009, 1, 5), "i1")
        self.assertIn(date(2009, 1, 5), H.audit(START, END).silent_trading_days)

    def test_a_data_point_in_any_other_store_counts(self):
        put_bar(date(2009, 1, 5))
        put_eod("fo_eod", date(2009, 1, 6))
        put_eod("cm_eod", date(2009, 1, 7))
        put_eod("index_eod", date(2009, 1, 9))
        put_raw("fo", date(2009, 1, 12))
        put_raw("idx", HOLIDAY)
        got = H.audit(START, END)
        self.assertEqual(got.silent_trading_days,
                         (date(2009, 1, 13), date(2009, 1, 14), date(2009, 1, 15), date(2009, 1, 16)))
        self.assertEqual(got.holidays_with_data, ((HOLIDAY, "Moharram", ("raw:idx",)),))

    def test_a_snapshot_named_by_its_download_day_is_not_trading_data(self):
        """Only NSE's per-trade-date datasets (sources.json, not `current_only`) say the exchange was open."""
        for dataset in ("qtyfreeze", "kite_instruments", "nse_holidays"):
            put_raw(dataset, START)
        self.assertIn(START, H.audit(START, END).silent_trading_days)
        put_raw("cm", START)
        self.assertNotIn(START, H.audit(START, END).silent_trading_days)

    def test_counts_per_year(self):
        for d in (date(2009, 12, 31), date(2010, 1, 4)):
            put_span(d)
        got = H.audit(date(2009, 12, 28), date(2010, 1, 5))     # 28 Dec 2009 and 1 Jan 2010 are holidays
        self.assertEqual(got.by_year, ((2009, 4, 1, 1), (2010, 3, 1, 1)))    # year, weekdays, with data, holidays
        self.assertEqual(got.silent_trading_days, (date(2009, 12, 29), date(2009, 12, 30), date(2010, 1, 5)))

    def test_the_audit_changes_nothing_in_the_lab(self):
        put_span(date(2009, 1, 5))
        put_bar(date(2009, 1, 6), wal=True)                # the real stores are WAL databases
        put_eod("fo_eod", date(2009, 1, 7))
        put_raw("cm", date(2009, 1, 9))
        paths.nse_margin_dir("manifest")
        before = tree()
        H.report(H.audit(START, END))
        self.assertEqual(tree(), before)

    def test_idle_wal_files_left_by_a_writer_are_not_touched(self):
        """The lab's stores keep an empty `-wal` and a `-shm` beside them between writes."""
        put_bar(date(2009, 1, 6), wal=True)
        wal, shm = (paths.bars_db().with_name(paths.bars_db().name + s) for s in ("-wal", "-shm"))
        wal.write_bytes(b"")
        shm.write_bytes(bytes(32768))
        paths.nse_margin_dir("span"), paths.nse_raw_dir()  # stockify.paths makes the folders it is asked for
        before = tree()
        got = H.audit(START, END)
        self.assertEqual(tree(), before)
        self.assertNotIn(date(2009, 1, 6), got.silent_trading_days)

    def test_rows_still_in_the_wal_are_read(self):
        put_bar(date(2009, 1, 6), wal=True)
        writer = sqlite3.connect(str(paths.bars_db()))
        self.addCleanup(writer.close)
        writer.execute("INSERT INTO bars VALUES (1, 'day', ?, 100)", (timeutil.ist_ns(date(2009, 1, 7), "00:00"),))
        writer.commit()                                    # in the -wal, not yet in the main file
        silent = H.audit(START, END).silent_trading_days
        self.assertNotIn(date(2009, 1, 6), silent)
        self.assertNotIn(date(2009, 1, 7), silent)

    def test_an_empty_lab_gets_no_files(self):
        got = H.audit(START, END)
        self.assertEqual(got.silent_trading_days, tuple(TRADING))
        self.assertEqual([p for p in paths.lab_dir().rglob("*") if p.is_file()], [])

    def test_the_report_names_both_lists(self):
        for d in WEEKDAYS:
            if d != date(2009, 1, 14):
                put_span(d)
        text = H.report(H.audit(START, END))
        self.assertIn("2009-01-14 Wed", text)
        self.assertIn("2009-01-08 Thu  Moharram  [span]", text)
        self.assertIn("2009-01-05 .. 2009-01-16", text)

    def test_cli_prints_the_report(self):
        for d in TRADING[1:]:
            put_span(d)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["calendar", "audit", "--from", START.isoformat(), "--to", END.isoformat()])
        self.assertEqual(code, 0)
        self.assertIn("2009-01-05 Mon", out.getvalue())
        self.assertNotIn("2009-01-06", out.getvalue())

    def test_cli_refuses_a_reversed_range(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["calendar", "audit", "--from", END.isoformat(), "--to", START.isoformat()])
        self.assertEqual(code, 1)
        self.assertIn("--to", err.getvalue())

    def test_plain_calendar_command_still_lists_sessions(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["calendar", "--start", "2026-11-09", "--days", "2"])
        self.assertEqual(code, 0)
        self.assertIn("closed: Diwali-Balipratipada", out.getvalue())


if __name__ == "__main__":
    unittest.main()
