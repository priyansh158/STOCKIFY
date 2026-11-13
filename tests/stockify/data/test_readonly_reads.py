"""LCM 6.2: the data reads (/api/data/quality, /api/data/recorder, store.latest_recorder_status) open their stores
read-only. A missing store is never created; an unreadable one is named in a plain sentence, not a 500."""
from __future__ import annotations

import sqlite3
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import paths, store, timeutil
from stockify.data import refdata

DAY = date(2026, 11, 9)
REFDATA_SENTENCE = "Couldn't read the recorded-day and download store."
NOT_A_DATABASE = b"this is not a sqlite database " * 40
ODD_FOLDERS = ("h#sh", "pct%41x", "sp ace", "a&mode=rw", "100%")      # characters a SQLite URI treats as syntax


def plant_table(path: Path, ddl: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.executescript(ddl)
    c.commit()
    c.close()


def plant_refdata_quality() -> None:
    """A healthy refdata store with one instrument and day-level plus instrument-level quality rows."""
    conn = refdata._connect()
    conn.execute("INSERT INTO instruments (instrument_id, exchange, kind, tradingsymbol, first_seen, last_seen, source) "
                 "VALUES (256265, 'NSE', 'INDEX', 'NIFTY 50', '2026-11-01', '2026-11-09', 'kite_dump')")
    for iid, metric, value in ((0, "silent_minutes", 1.0), (256265, "coverage_pct", 45.0)):
        conn.execute("INSERT INTO data_quality (trade_date, instrument_id, metric, value) VALUES (?, ?, ?, ?)",
                     (DAY.isoformat(), iid, metric, value))
    refdata._conn.close()
    refdata._conn = None


class ReadonlyUriTest(SandboxedDBTest):
    """One helper builds every read-only SQLite URI, and escapes what a URI would read as syntax."""

    def odd(self, name: str) -> Path:
        return paths.lab_dir() / name / "x.sqlite"

    def test_every_odd_folder_opens_the_file_it_names_read_only(self):
        for name in ODD_FOLDERS:
            with self.subTest(name):
                path = self.odd(name)
                plant_table(path, "CREATE TABLE t (v INTEGER); INSERT INTO t VALUES (7)")
                conn = sqlite3.connect(paths.readonly_uri(path), uri=True)
                try:
                    self.assertEqual(conn.execute("SELECT v FROM t").fetchone()[0], 7)
                    with self.assertRaises(sqlite3.OperationalError):
                        conn.execute("INSERT INTO t VALUES (8)")
                finally:
                    conn.close()

    def test_a_missing_file_in_an_odd_folder_is_not_created(self):
        path = self.odd("h#sh")
        path.parent.mkdir(parents=True)
        with self.assertRaises(sqlite3.OperationalError):
            sqlite3.connect(paths.readonly_uri(path), uri=True)
        self.assertFalse(path.exists())

    def test_immutable_adds_the_flag(self):
        self.assertTrue(paths.readonly_uri(self.odd("a b"), immutable=True).endswith("?mode=ro&immutable=1"))
        self.assertTrue(paths.readonly_uri(self.odd("a b")).endswith("?mode=ro"))

    def test_the_readers_open_a_store_in_an_odd_folder(self):
        from stockify.daemon.landing import LandingDB
        from stockify.data import compact
        from stockify.reporting import coverage
        for name in ODD_FOLDERS:
            with self.subTest(name):
                path = self.odd(name)
                plant_table(path, "CREATE TABLE recorder_status (wall_ns INTEGER, connected INTEGER);"
                                  "INSERT INTO recorder_status VALUES (5, 1);"
                                  "CREATE TABLE packets (n INTEGER); INSERT INTO packets VALUES (1)")
                with mock.patch.object(store, "_db_path", return_value=path):
                    row, bad = store.recorder_status_read()
                self.assertEqual((row["wall_ns"], bad), (5, False))
                self.assertEqual(compact._landing_packets(path), 1)
                self.assertEqual([r["wall_ns"] for r in coverage._rows(path, "SELECT wall_ns FROM recorder_status")], [5])
                landing = LandingDB(DAY, path, readonly=True)
                try:
                    self.assertEqual(landing.conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0], 1)
                finally:
                    landing.close()

    def test_quality_reads_refdata_in_an_odd_folder(self):
        path = self.odd("h#sh")
        plant_table(path, "CREATE TABLE data_quality (trade_date TEXT, instrument_id INTEGER, metric TEXT, value REAL);"
                          "INSERT INTO data_quality VALUES ('2026-11-09', 0, 'silent_minutes', 1.0);"
                          "CREATE TABLE instruments (instrument_id INTEGER, tradingsymbol TEXT)")
        from stockify.data import quality
        with mock.patch.object(paths, "refdata_db", return_value=path):
            rows, symbols = quality.stored_readonly(DAY)
        self.assertEqual((rows, symbols), ({0: {"silent_minutes": 1.0}}, {}))


class StoreReadsTest(SandboxedDBTest):
    db_modules = (store, refdata)

    def test_recorder_status_read_reports_unreadable_only_for_a_file_that_exists(self):
        self.assertEqual(store.recorder_status_read(), (None, False))             # missing file
        self.assertFalse(paths.lab_db().exists())
        plant_table(paths.lab_db(), "CREATE TABLE other (x INTEGER)")
        self.assertEqual(store.recorder_status_read(), (None, False))             # missing table: an older schema
        paths.lab_db().write_bytes(NOT_A_DATABASE)
        self.assertEqual(store.recorder_status_read(), (None, True))              # exists, cannot be read

    def test_recorder_status_read_returns_the_row_and_not_unreadable(self):
        store.write_recorder_status({"wall_ns": 5, "connected": 1, "subs_full": 0, "subs_quote": 0,
                                     "packets_per_s": 0.0, "queue_depth": 0, "writer_lag_ms": None,
                                     "last_packet_age_ms_json": "{}", "bytes_today": 0})
        row, bad = store.recorder_status_read()
        self.assertEqual((row["wall_ns"], bad), (5, False))
        self.assertEqual(store.latest_recorder_status(), row)

    def test_a_missing_lab_store_reads_as_no_status_and_stays_missing(self):
        self.assertFalse(paths.lab_db().exists())
        self.assertIsNone(store.latest_recorder_status())
        self.assertFalse(paths.lab_db().exists())

    def test_an_unreadable_lab_store_reads_as_no_status(self):
        paths.lab_db().parent.mkdir(parents=True, exist_ok=True)
        paths.lab_db().write_bytes(NOT_A_DATABASE)
        self.assertIsNone(store.latest_recorder_status())
        self.assertEqual(paths.lab_db().read_bytes(), NOT_A_DATABASE)

    def test_a_lab_store_without_the_table_reads_as_no_status(self):
        paths.lab_db().parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(paths.lab_db()))
        c.execute("CREATE TABLE other (x INTEGER)")
        c.commit()
        c.close()
        self.assertIsNone(store.latest_recorder_status())

    def test_a_healthy_lab_store_returns_the_latest_row(self):
        for ns in (10, 30, 20):
            store.write_recorder_status({"wall_ns": ns, "connected": 1, "subs_full": 3, "subs_quote": 4,
                                         "packets_per_s": 2.5, "queue_depth": 0, "writer_lag_ms": 1.5,
                                         "last_packet_age_ms_json": "{}", "bytes_today": 99})
        row = store.latest_recorder_status()
        self.assertEqual((row["wall_ns"], row["subs_full"], row["bytes_today"]), (30, 3, 99))


@support.requires_fastapi
class ApiReadsTest(SandboxedDBTest):
    db_modules = (store, refdata)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        # raise_server_exceptions=False: an unhandled error must show as the HTTP 500 a browser would see
        self.client = TestClient(app, base_url=support.API_BASE_URL, raise_server_exceptions=False)

    def quality(self):
        return self.client.get("/api/data/quality", params={"date": DAY.isoformat()})

    def test_quality_on_a_missing_refdata_store_is_200_with_a_sentence_and_creates_nothing(self):
        self.assertFalse(paths.refdata_db().exists())
        r = self.quality()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["errors"], body["error_stores"]), ([REFDATA_SENTENCE], ["refdata"]))
        self.assertEqual((body["date"], body["day"], body["instruments"]), (DAY.isoformat(), {}, []))
        self.assertFalse(paths.refdata_db().exists())

    def test_quality_on_an_unreadable_refdata_store_is_200_with_a_sentence(self):
        paths.refdata_db().parent.mkdir(parents=True, exist_ok=True)
        paths.refdata_db().write_bytes(NOT_A_DATABASE)
        r = self.quality()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["errors"], r.json()["error_stores"]), ([REFDATA_SENTENCE], ["refdata"]))
        self.assertEqual(paths.refdata_db().read_bytes(), NOT_A_DATABASE)

    def test_the_sentence_names_no_path_and_no_exception_text(self):
        paths.refdata_db().parent.mkdir(parents=True, exist_ok=True)
        paths.refdata_db().write_bytes(NOT_A_DATABASE)
        text = self.quality().text
        for leak in ("sqlite", "refdata.sqlite", "not a database", str(paths.lab_dir())):
            self.assertNotIn(leak, text)

    def test_quality_on_a_healthy_store_has_no_errors_and_the_same_values(self):
        plant_refdata_quality()
        r = self.quality()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["errors"], body["error_stores"]), ([], []))
        self.assertEqual(body["day"], {"silent_minutes": 1.0})
        self.assertEqual([(i["instrument_id"], i["symbol"], i["metrics"]) for i in body["instruments"]],
                         [(256265, "NIFTY 50", {"coverage_pct": 45.0})])

    def test_a_refdata_store_without_the_quality_table_reads_as_no_report_not_unreadable(self):
        plant_table(paths.refdata_db(), "CREATE TABLE unrelated (x INTEGER)")
        self.assertEqual(self.quality().status_code, 404)

    def test_symbols_are_read_in_chunks_under_the_variable_limit(self):
        plant_refdata_quality()
        conn = refdata._connect()
        for iid in (7, 8, 9, 10, 11):
            conn.execute("INSERT INTO instruments (instrument_id, exchange, kind, tradingsymbol, first_seen, "
                         "last_seen, source) VALUES (?, 'NSE', 'EQ', ?, '2026-11-01', '2026-11-09', 'kite_dump')",
                         (iid, f"S{iid}"))
            conn.execute("INSERT INTO data_quality (trade_date, instrument_id, metric, value) VALUES (?, ?, 'coverage_pct', 1.0)",
                         (DAY.isoformat(), iid))
        refdata._conn.close()
        refdata._conn = None
        from stockify.data import quality
        with mock.patch.object(quality, "_ID_CHUNK", 2):
            _rows, symbols = quality.stored_readonly(DAY)
        self.assertEqual(symbols, {256265: "NIFTY 50", 7: "S7", 8: "S8", 9: "S9", 10: "S10", 11: "S11"})

    def test_the_unreadable_store_is_named_by_the_error_itself(self):
        from stockify.data import quality
        self.assertEqual(quality.StoreUnreadable("refdata").store_key, "refdata")
        self.assertEqual(str(quality.StoreUnreadable("lab")), quality.unreadable_sentence("lab"))

    def test_quality_for_a_day_with_no_report_is_still_404_on_a_healthy_store(self):
        plant_refdata_quality()
        self.assertEqual(self.client.get("/api/data/quality", params={"date": "2026-11-11"}).status_code, 404)

    def test_the_read_does_not_hold_the_store_open(self):
        plant_refdata_quality()
        self.assertEqual(self.quality().status_code, 200)
        self.assertIsNone(refdata._conn)          # nothing was opened through the read-write singleton

    def test_recorder_status_on_a_missing_lab_store_is_null_and_creates_nothing(self):
        r = self.client.get("/api/data/recorder")
        self.assertEqual((r.status_code, r.json()["status"]), (200, None))
        self.assertFalse(paths.lab_db().exists())

    def test_recorder_status_on_an_unreadable_lab_store_is_null(self):
        paths.lab_db().parent.mkdir(parents=True, exist_ok=True)
        paths.lab_db().write_bytes(NOT_A_DATABASE)
        r = self.client.get("/api/data/recorder")
        self.assertEqual((r.status_code, r.json()["status"]), (200, None))

    def test_recorder_status_on_a_healthy_store_is_unchanged(self):
        store.write_recorder_status({"wall_ns": timeutil.now_ns(), "connected": 1, "subs_full": 3, "subs_quote": 4,
                                     "packets_per_s": 2.5, "queue_depth": 0, "writer_lag_ms": 1.5,
                                     "last_packet_age_ms_json": "{}", "bytes_today": 99})
        s = self.client.get("/api/data/recorder").json()["status"]
        self.assertEqual((s["connected"], s["subs_full"], s["bytes_today"]), (True, 3, 99))


if __name__ == "__main__":
    unittest.main()
