"""LCM 6.2: the data coverage matrix by date and dataset, with gaps, bar ranges and storage."""
from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import research, store, timeutil
from stockify.daemon.landing import LandingDB
from stockify.data import calendar, history, refdata
from stockify.reporting import coverage

FROM, TO = date(2026, 10, 1), date(2026, 10, 7)   # Thu, Fri (Gandhi Jayanti), Sat, Sun, Mon, Tue, Wed
WED, THU = date(2026, 10, 7), date(2026, 10, 1)
T0 = 1_790_000_000_000_000_000
DAY_NS = 86_400 * 10**9
UNLOGGED = "instruments recorded before mode logging; not counted"


def put_day(day: date, status: str = "verified", landing_bytes: int = 100, archive_bytes: int = 40) -> None:
    refdata._connect().execute(
        "INSERT INTO tick_days (trade_date, status, packets, landing_bytes, archive_bytes) VALUES (?, ?, 10, ?, ?)",
        (day.isoformat(), status, landing_bytes, archive_bytes))


def put_q(day: date, **metrics: float) -> None:
    for k, v in metrics.items():
        refdata._connect().execute(
            "INSERT INTO data_quality (trade_date, instrument_id, metric, value) VALUES (?, 0, ?, ?)",
            (day.isoformat(), k, v))


def put_counts(day: date, full: float, quote: float, unlogged: float = 0, **pcts: float) -> None:
    put_q(day, coverage_full_n=full, coverage_quote_n=quote, coverage_unlogged_n=unlogged, **pcts)


def put_nse(dataset: str, day: date, outcome: str, parsed_ok: int = 1, size: int = 1000) -> None:
    refdata._connect().execute(
        "INSERT INTO nse_fetch_log (dataset, trade_date, http_status, outcome, bytes, parsed_ok) "
        "VALUES (?, ?, 200, ?, ?, ?)", (dataset, day.isoformat(), outcome, size, parsed_ok))


def cell(m, day: date, key: str):
    return next(d for d in m.days if d.date == day).cells[key]


class CoverageTest(SandboxedDBTest):
    db_modules = (store, refdata, history)

    def test_empty_lab_matrix_has_no_numbers(self):
        m = coverage.matrix(FROM, TO)
        self.assertEqual(len(m.days), 7)
        for d in m.days:
            for c in d.cells.values():
                self.assertIn(c.state, ("missing", "closed"))
                self.assertIsNone(c.value)
        self.assertIsNone(m.recorder)
        self.assertEqual(m.bars, [])

    def test_unreported_day_is_not_a_zero(self):
        put_day(WED, "landing")
        c = cell(coverage.matrix(FROM, TO), WED, "recorder_full")
        self.assertEqual((c.state, c.value), ("not_reported", None))

    def test_weekends_and_holidays_are_closed(self):
        m = coverage.matrix(FROM, TO)
        for d in (date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 4)):
            self.assertFalse(next(x for x in m.days if x.date == d).trading)
            for dataset in m.datasets:
                self.assertEqual(cell(m, d, dataset["key"]).state, "closed", (d, dataset["key"]))
        self.assertEqual(cell(m, THU, "recorder_full").state, "missing")

    def test_landing_only_day_shows_landing_and_a_partial_state(self):
        put_day(WED, "landing")
        c = cell(coverage.matrix(FROM, TO), WED, "archive")
        self.assertEqual((c.state, c.detail), ("partial", "landing"))

    def test_verified_archive_day(self):
        put_day(WED, "verified")
        put_day(THU, "landing_deleted")
        m = coverage.matrix(FROM, TO)
        self.assertEqual((cell(m, WED, "archive").state, cell(m, WED, "archive").detail), ("ok", "verified"))
        self.assertEqual((cell(m, THU, "archive").state, cell(m, THU, "archive").detail), ("ok", "verified"))

    def test_recorder_cells_use_the_two_day_metrics_and_the_configured_threshold(self):
        put_day(WED)
        put_counts(WED, 5, 3, coverage_full_pct=99.5, coverage_quote_pct=97.2)
        m = coverage.matrix(FROM, TO)
        self.assertEqual((cell(m, WED, "recorder_full").state, cell(m, WED, "recorder_full").value), ("ok", 99.5))
        self.assertEqual((cell(m, WED, "recorder_quote").state, cell(m, WED, "recorder_quote").value),
                         ("partial", 97.2))
        for pct, want in (("98", "partial"), ("97", "ok")):
            cfg = dict(research.settings(), coverage=dict(research.settings()["coverage"], ok_pct=pct))
            with mock.patch("stockify.research.settings", return_value=cfg):
                self.assertEqual(cell(coverage.matrix(FROM, TO), WED, "recorder_quote").state, want, pct)

    def test_zero_instruments_is_not_applicable_and_unlogged_is_said(self):
        put_day(WED)
        put_counts(WED, 4, 0, unlogged=2, coverage_full_pct=100.0)
        m = coverage.matrix(FROM, TO)
        q = cell(m, WED, "recorder_quote")
        self.assertEqual(q.state, "not_applicable")
        self.assertIn("no instruments recorded in this mode", q.detail)
        self.assertIn("2 " + UNLOGGED, q.detail)
        self.assertEqual(cell(m, WED, "recorder_full").detail, "2 " + UNLOGGED)
        self.assertIsNone(q.value)

    def test_no_unlogged_line_when_none(self):
        put_day(WED)
        put_counts(WED, 4, 1, coverage_full_pct=100.0, coverage_quote_pct=100.0)
        self.assertIsNone(cell(coverage.matrix(FROM, TO), WED, "recorder_full").detail)

    def test_no_tick_days_row_on_a_trading_day_is_missing_even_with_rows(self):
        put_counts(WED, 1, 1, coverage_full_pct=100.0, coverage_quote_pct=100.0)
        self.assertEqual(cell(coverage.matrix(FROM, TO), WED, "recorder_full").state, "missing")

    def test_nse_dataset_cells(self):
        put_nse("fo", WED, "ok")
        put_nse("cm", WED, "missing")
        put_nse("idx", WED, "error")
        put_nse("fo", THU, "ok", parsed_ok=0)
        m = coverage.matrix(FROM, TO)
        self.assertEqual(cell(m, WED, "nse_fo").state, "ok")
        self.assertEqual(cell(m, WED, "nse_cm").state, "missing")
        self.assertEqual(cell(m, WED, "nse_idx").state, "missing")
        self.assertIn("download failed", cell(m, WED, "nse_idx").detail)
        self.assertIn("http 200", cell(m, WED, "nse_idx").detail)
        self.assertEqual(cell(m, THU, "nse_fo").state, "partial")
        self.assertEqual(cell(m, THU, "nse_fo").detail, "downloaded, not parsed")
        self.assertEqual(cell(m, date(2026, 10, 6), "nse_fo").state, "missing")
        self.assertEqual([d["key"] for d in m.datasets if d["key"].startswith("nse_")],
                         ["nse_fo", "nse_cm", "nse_idx"])
        self.assertTrue(all(d["label"] for d in m.datasets))

    def test_bar_ranges_per_instrument(self):
        with history._Tx() as c:
            for iid, interval, a, b, rows in ((7, "minute", T0, T0 + 3 * DAY_NS, 100),
                                              (7, "minute", T0 + 3 * DAY_NS, T0 + 5 * DAY_NS, 50),
                                              (8, "day", T0, T0 + 9 * DAY_NS, 9)):
                c.execute("INSERT INTO bar_fetch_log VALUES (?, ?, ?, ?, ?, ?, 'ok')", (iid, interval, a, b, rows, T0))
            c.execute("INSERT INTO bar_fetch_log VALUES (9, 'day', ?, ?, 0, ?, 'error')", (T0, T0 + DAY_NS, T0))
        bars = coverage.matrix(FROM, TO).bars
        self.assertEqual([(b["instrument_id"], b["interval"]) for b in bars], [(8, "day"), (7, "minute")])
        seven = bars[1]
        self.assertEqual((seven["from_ns"], seven["to_ns"], seven["rows"]), (T0, T0 + 5 * DAY_NS, 150))
        self.assertEqual(seven["days"], 6)
        self.assertEqual(len(coverage.matrix(FROM, TO, bar_limit=1).bars), 1)

    def test_storage_numbers_and_warnings(self):
        put_day(WED, "verified", landing_bytes=500, archive_bytes=40)
        put_day(THU, "landing", landing_bytes=100, archive_bytes=0)
        put_day(date(2026, 10, 6), "landing_deleted", landing_bytes=900, archive_bytes=60)
        put_nse("fo", WED, "ok", size=7)
        usage = mock.Mock(free=5_000_000_000, total=100_000_000_000)
        with mock.patch.object(coverage.shutil, "disk_usage", return_value=usage):
            s = coverage.matrix(FROM, TO).storage
        self.assertEqual((s["landing_bytes"], s["archive_bytes"]), (600, 100))
        self.assertEqual((s["free_bytes"], s["total_bytes"]), (5_000_000_000, 100_000_000_000))
        self.assertGreater(s["refdata_bytes"], 0)
        self.assertEqual(s["nse_bytes"], 7)
        self.assertEqual(len(s["warnings"]), 2)

    def test_recorder_is_the_latest_status_row(self):
        self.assertIsNone(coverage.matrix(FROM, TO).recorder)
        store.write_recorder_status({"wall_ns": T0, "connected": 1, "subs_full": 1, "subs_quote": 2})
        self.assertEqual(coverage.matrix(FROM, TO).recorder["subs_quote"], 2)

    def test_gaps_for_a_recorded_day_and_404_without(self):
        with self.assertRaises(coverage.NoRecording):
            coverage.gaps(WED)
        db = LandingDB(WED)
        t = 1_791_000_000_000_000_000
        db.add_gap(t, t + 90 * 10**9, "disconnect", "all")
        db.add_dropped_frames([(5, 9, t + 100 * 10**9, t + 101 * 10**9)])
        db.open_gap(t + 200 * 10**9, "reconnecting", "tier:full")
        db.close()
        g = coverage.gaps(WED)
        self.assertEqual([x["reason"] for x in g], ["disconnect", "queue_full", "reconnecting"])
        self.assertEqual(list(g[0]), ["start_ns", "end_ns", "start_ist", "duration_s", "reason", "scope", "frames"])
        self.assertEqual((g[0]["duration_s"], g[1]["frames"]), (90.0, 5))
        self.assertIsNone(g[2]["end_ns"])
        self.assertIsNone(g[2]["duration_s"])
        self.assertTrue(g[0]["start_ist"].endswith("+05:30"))

    def test_matrix_is_finite_json(self):
        put_day(WED)
        put_counts(WED, 1, 1, coverage_full_pct=float("nan"), coverage_quote_pct=float("inf"))
        m = coverage.matrix(FROM, TO)
        self.assertEqual(cell(m, WED, "recorder_full").state, "not_reported")
        json.dumps(coverage.to_dict(m), allow_nan=False)

    def test_unknown_archive_status_and_a_day_still_recording(self):
        put_day(WED, "weird")
        self.assertEqual((cell(coverage.matrix(FROM, TO), WED, "archive").state,
                          cell(coverage.matrix(FROM, TO), WED, "archive").detail), ("not_reported", "unknown status weird"))
        today = timeutil.ist_today()
        if not calendar.is_trading_day("CM", today):
            self.skipTest("today is not a trading day")
        put_day(today, "landing")
        c = cell(coverage.matrix(today, today), today, "archive")
        self.assertEqual((c.state, c.detail), ("partial", "recording in progress"))

    def test_an_outdated_schema_is_read_without_touching_the_files(self):
        import sqlite3
        from stockify import paths
        for path, ddl in ((paths.refdata_db(), "CREATE TABLE tick_days (trade_date TEXT PRIMARY KEY, status TEXT);"
                           "INSERT INTO tick_days VALUES ('2026-10-07', 'verified')"),
                          (paths.bars_db(), "CREATE TABLE bars (instrument_id INTEGER)")):
            c = sqlite3.connect(str(path))
            c.executescript(ddl)
            c.commit()
            c.close()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (paths.refdata_db(), paths.bars_db())}
        m = coverage.matrix(FROM, TO)
        self.assertEqual(cell(m, WED, "archive").state, "ok")
        self.assertEqual(cell(m, WED, "recorder_full").state, "not_reported")
        self.assertEqual(cell(m, WED, "nse_fo").state, "missing")
        self.assertEqual((m.bars, m.storage["nse_bytes"]), ([], 0))
        for p, (data, mtime) in before.items():
            self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), (data, mtime), p.name)
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(p.with_name(p.name + suffix).exists(), p.name + suffix)

    def test_an_unreadable_store_is_an_error_not_an_empty_lab(self):
        from stockify import paths
        for make_bad, name in ((lambda p: p.mkdir(parents=True), "unopenable"),
                               (lambda p: (p.parent.mkdir(parents=True, exist_ok=True), p.write_bytes(b"not a database " * 50)),
                                "corrupt")):
            with self.subTest(name):
                for p in (paths.refdata_db(), paths.bars_db()):
                    if p.is_dir():
                        p.rmdir()
                    elif p.exists():
                        p.unlink()
                make_bad(paths.refdata_db())
                m = coverage.matrix(FROM, TO)
                self.assertEqual(len(m.errors), 1, m.errors)
                self.assertEqual(m.error_stores, ["refdata"])
                self.assertIn("Couldn't read the", m.errors[0])
                self.assertIn("recorded-day and download store", m.errors[0])
                for bad in ("\\", "/", ".sqlite", "sqlite3", "Error"):
                    self.assertNotIn(bad, m.errors[0])
        self.assertEqual(coverage.to_dict(m)["errors"], m.errors)
        self.assertEqual(coverage.to_dict(m)["error_stores"], ["refdata"])

    def test_a_locked_bars_store_names_the_bars_store(self):
        from stockify import paths
        history._connect().close()
        holder = sqlite3.connect(str(paths.bars_db()), isolation_level=None)
        holder.execute("PRAGMA journal_mode=DELETE")
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with mock.patch.object(coverage, "_TIMEOUT_S", 0.05):
                m = coverage.matrix(FROM, TO)
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertEqual(len(m.errors), 1, m.errors)
        self.assertIn("bar store", m.errors[0])
        self.assertEqual(m.error_stores, ["bars"])
        self.assertEqual(m.bars, [])

    def test_a_missing_store_or_an_old_schema_is_not_an_error(self):
        m = coverage.matrix(FROM, TO)
        self.assertEqual((m.errors, m.error_stores), ([], []))

    def test_an_unreadable_recorder_status_names_the_lab_store(self):
        from stockify import paths
        from stockify.data import quality
        for make_bad, name in ((lambda p: p.write_bytes(b"not a database " * 50), "corrupt"),
                               (lambda p: p.mkdir(), "unopenable")):
            with self.subTest(name):
                lab = paths.lab_db()
                if lab.is_dir():
                    lab.rmdir()
                elif lab.exists():
                    lab.unlink()
                make_bad(lab)
                m = coverage.matrix(FROM, TO)
                self.assertEqual(m.error_stores, ["lab"])
                self.assertEqual(m.errors, [quality.unreadable_sentence("lab")])
                self.assertEqual(m.errors, ["Couldn't read the recorder status."])
                self.assertIsNone(m.recorder)
                for bad in ("\\", "/", ".sqlite", "sqlite3", "Error"):
                    self.assertNotIn(bad, m.errors[0])

    def test_a_missing_or_old_lab_store_is_not_an_error(self):
        m = coverage.matrix(FROM, TO)
        self.assertEqual((m.errors, m.error_stores, m.recorder), ([], [], None))

    def test_coverage_shares_the_store_names_and_sentence_with_quality(self):
        from stockify.data import quality
        self.assertFalse(hasattr(coverage, "_STORE_NAMES"))
        self.assertFalse(hasattr(coverage, "_sentence"))
        self.assertIs(coverage.STORE_NAMES, quality.STORE_NAMES)

    def test_two_reads_of_one_bad_store_say_it_once(self):
        from stockify import paths
        paths.refdata_db().mkdir(parents=True)
        m = coverage.matrix(FROM, TO)
        self.assertEqual((len(m.errors), m.error_stores), (1, ["refdata"]))

    def test_reads_do_not_create_stores(self):
        from stockify import paths
        coverage.matrix(FROM, TO)
        self.assertFalse(paths.refdata_db().exists())
        self.assertFalse(paths.bars_db().exists())


@support.requires_fastapi
class CoverageApiTests(SandboxedDBTest):
    db_modules = (store, refdata, history)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)

    def test_matrix_endpoint_shape(self):
        put_day(WED)
        put_counts(WED, 2, 0, coverage_full_pct=99.9)
        r = self.client.get("/api/data/coverage?from=2026-10-01&to=2026-10-07")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(list(j), ["from_date", "to_date", "datasets", "days", "bars", "storage", "recorder", "errors", "error_stores"])
        self.assertEqual((j["errors"], j["error_stores"]), ([], []))
        self.assertEqual(len(j["days"]), 7)
        self.assertEqual(list(j["days"][0]), ["date", "trading", "cells"])
        self.assertEqual(j["days"][6]["cells"]["recorder_full"], {"state": "ok", "value": 99.9, "detail": None})

    def test_range_limits_come_from_the_config_and_bad_dates_422(self):
        cfg = dict(research.settings(), coverage={"ok_pct": "99", "default_days": 3, "max_days": 10})
        with mock.patch("stockify.research.settings", return_value=cfg):
            r = self.client.get("/api/data/coverage?to=2026-10-07")
            self.assertEqual(len(r.json()["days"]), 3)
            self.assertEqual(self.client.get("/api/data/coverage?from=2026-10-01&to=2026-10-10").status_code, 200)
            big = self.client.get("/api/data/coverage?from=2026-10-01&to=2026-10-11")
            self.assertEqual(big.status_code, 422)
            self.assertIn("10", big.text)
        for q in ("from=nope", "from=20261001&to=2026-10-07", "from=2026-10-07&to=2026-10-01", "to=2026-W41-1",
                  "from=2026-10-01%20&to=2026-10-02"):
            r = self.client.get("/api/data/coverage?" + q)
            self.assertEqual(r.status_code, 422, q)
            self.assertNotIn("\\", r.text)
        self.assertEqual(self.client.get("/api/data/gaps?date=bad").status_code, 422)
        self.assertEqual(self.client.get("/api/data/gaps").status_code, 422)

    def test_gaps_endpoint_and_404(self):
        r = self.client.get("/api/data/gaps?date=2026-10-07")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("sqlite", r.text)
        self.assertNotIn("\\", r.text)
        db = LandingDB(WED)
        db.add_gap(T0, T0 + 10**9, "disconnect")
        db.close()
        r = self.client.get("/api/data/gaps?date=2026-10-07")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual((j["total"], j["offset"], j["limit"]), (1, 0, 200))
        self.assertEqual(j["gaps"][0]["reason"], "disconnect")

    def test_gaps_pages(self):
        db = LandingDB(WED)
        for i in range(5):
            db.add_gap(T0 + i * 10**10, T0 + i * 10**10 + 10**9, f"g{i}")
        db.close()
        r = self.client.get("/api/data/gaps?date=2026-10-07&offset=1&limit=2").json()
        self.assertEqual((r["total"], r["offset"], r["limit"]), (5, 1, 2))
        self.assertEqual([g["reason"] for g in r["gaps"]], ["g1", "g2"])
        self.assertEqual(self.client.get("/api/data/gaps?date=2026-10-07&offset=9").json()["gaps"], [])
        for q in ("offset=-1", "limit=0", "limit=1001", "limit=x"):
            self.assertEqual(self.client.get(f"/api/data/gaps?date=2026-10-07&{q}").status_code, 422, q)

    def test_matrix_is_finite_json(self):
        put_day(WED)
        put_counts(WED, 1, 1, coverage_full_pct=float("nan"))
        text = self.client.get("/api/data/coverage?from=2026-10-01&to=2026-10-07").text
        for token in ("NaN", "Infinity"):
            self.assertNotIn(token, text)
        json.loads(text, parse_constant=lambda c: self.fail(c))

    def test_non_loopback_host_refused(self):
        for url in ("/api/data/coverage", "/api/data/gaps?date=2026-10-07"):
            r = self.client.get(url, headers={"Host": "evil.example"})
            self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
