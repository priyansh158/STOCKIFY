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
from unittest.mock import patch

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


def write_in_progress(day: date) -> sqlite3.Connection:
    """bars.sqlite as it is while a recorder writes: a committed bar of `day` is in the `-wal`, not in the main
    file, and the writer is still connected."""
    writer = sqlite3.connect(str(paths.bars_db()))
    writer.execute("INSERT INTO bars VALUES (1, 'day', ?, 100)", (timeutil.ist_ns(day, "00:00"),))
    writer.commit()
    return writer


def killed_writer(day: date) -> None:
    """bars.sqlite as a recorder that was killed leaves it: a committed bar of `day` is in the `-wal` and there is
    no `-shm` (a plain read-only connection would make one)."""
    db = paths.bars_db()
    wal, shm = (db.with_name(db.name + suffix) for suffix in ("-wal", "-shm"))
    writer = write_in_progress(day)
    main, pending = db.read_bytes(), wal.read_bytes()
    writer.close()                                         # checkpoints and removes the -wal
    db.write_bytes(main)
    wal.write_bytes(pending)
    shm.unlink(missing_ok=True)


def opened_by(fn) -> list[str]:  # noqa: ANN001
    """The database names `fn` hands to sqlite3.connect inside the audit module."""
    seen: list[str] = []
    real = H.sqlite3.connect

    def connect(name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        seen.append(str(name))
        return real(name, *args, **kwargs)

    with patch.object(H.sqlite3, "connect", connect):
        fn()
    return seen


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

    def test_a_store_with_a_write_in_progress_is_not_opened(self):
        """Rows in a `-wal` can only be read by a connection that writes the `-shm`: such a store is left alone."""
        put_bar(date(2009, 1, 6), wal=True)
        put_eod("fo_eod", date(2009, 1, 9))
        writer = write_in_progress(date(2009, 1, 7))
        self.addCleanup(writer.close)
        paths.nse_margin_dir("span"), paths.nse_raw_dir()  # stockify.paths makes the folders it is asked for
        before = tree()
        self.assertGreater(dict((name, size) for name, size, _ in before)["bars.sqlite-wal"], 0)
        got: list = []
        opened = opened_by(lambda: got.append(H.audit(START, END)))
        self.assertEqual(tree(), before)                   # the file list, the sizes and the modified times
        self.assertEqual([name for name in opened if "bars.sqlite" in name], [])
        self.assertEqual(len(opened), 1)                   # refdata.sqlite is idle and is read
        self.assertEqual(got[0].not_checked, (("bars.sqlite", "busy, not checked (its -wal file is not empty)"),))
        self.assertIn(date(2009, 1, 6), got[0].silent_trading_days)    # its bars were not read
        self.assertNotIn(date(2009, 1, 9), got[0].silent_trading_days)
        self.assertIn("bars.sqlite: busy, not checked", H.report(got[0]))

    def test_rows_left_in_the_wal_by_a_killed_writer_are_not_read(self):
        put_bar(date(2009, 1, 6), wal=True)
        killed_writer(date(2009, 1, 7))
        paths.nse_margin_dir("span"), paths.nse_raw_dir()
        before = tree()
        self.assertEqual([name for name, _, _ in before if name.startswith("bars")],
                         ["bars.sqlite", "bars.sqlite-wal"])
        got = H.audit(START, END)
        self.assertEqual(tree(), before)                   # no -shm is made, the -wal is not replayed
        self.assertEqual([name for name, _ in got.not_checked], ["bars.sqlite"])

    def test_a_hot_rollback_journal_also_means_a_write_in_progress(self):
        put_eod("cm_eod", date(2009, 1, 6))
        paths.nse_margin_dir("span"), paths.nse_raw_dir()
        journal = paths.refdata_db().with_name(paths.refdata_db().name + "-journal")
        journal.write_bytes(b"x" * 512)
        before = tree()
        got = H.audit(START, END)
        self.assertEqual(tree(), before)
        self.assertEqual([name for name, _ in got.not_checked], ["refdata.sqlite"])
        self.assertIn("-journal", got.not_checked[0][1])

    def test_every_database_is_opened_immutable(self):
        put_bar(date(2009, 1, 6), wal=True)
        put_eod("fo_eod", date(2009, 1, 7))
        opened = opened_by(lambda: H.audit(START, END))
        self.assertEqual(len(opened), 2)
        for name in opened:
            with self.subTest(name=name):
                self.assertTrue(name.endswith("?mode=ro&immutable=1"))

    def test_a_write_that_starts_during_the_read_is_reported(self):
        """The `-wal` is looked at before and after the read: an immutable read takes no lock, so a writer can
        start under it, and what was read is then not used."""
        put_bar(date(2009, 1, 6))
        real = H.sqlite3.connect

        def connect(name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            conn = real(name, *args, **kwargs)
            paths.bars_db().with_name("bars.sqlite-wal").write_bytes(b"x" * 4096)      # the writer arrives
            return conn

        with patch.object(H.sqlite3, "connect", connect):
            got = H.audit(START, END)
        self.assertEqual([name for name, _ in got.not_checked], ["bars.sqlite"])
        self.assertIn(date(2009, 1, 6), got.silent_trading_days)

    def test_a_store_that_cannot_be_read_is_reported_not_raised(self):
        paths.refdata_db().write_bytes(b"not a database, or a page torn by a checkpoint" * 100)
        got = H.audit(START, END)
        self.assertEqual([name for name, _ in got.not_checked], ["refdata.sqlite"])
        self.assertIn("not readable, not checked", got.not_checked[0][1])

    def test_an_idle_lab_has_nothing_unchecked(self):
        put_bar(date(2009, 1, 6), wal=True)
        put_eod("fo_eod", date(2009, 1, 7))
        got = H.audit(START, END)
        self.assertEqual(got.not_checked, ())
        self.assertNotIn("not checked", H.report(got))

    def test_cli_exits_non_zero_when_a_store_is_busy(self):
        put_bar(date(2009, 1, 6), wal=True)
        killed_writer(date(2009, 1, 7))
        paths.nse_margin_dir("span"), paths.nse_raw_dir()
        before = tree()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["calendar", "audit", "--from", START.isoformat(), "--to", END.isoformat()])
        self.assertEqual(code, 1)
        self.assertEqual(tree(), before)
        self.assertIn("bars.sqlite: busy, not checked", out.getvalue())
        self.assertIn("no recorder or daemon is writing", err.getvalue())

    def test_cli_help_says_when_to_run_it(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["calendar", "audit", "--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("only while no recorder or daemon is writing", text)
        self.assertIn("can make the report wrong, never the stores", text)

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
