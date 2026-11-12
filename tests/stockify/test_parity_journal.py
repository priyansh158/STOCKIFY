"""RD 5.1 (M5.10; E-01): every finalised session-day is re-run from its journal through a fresh engine, and every
stored row must equal the replay's. A difference is a bug: it is recorded, the session is paused, nothing is repaired."""
from __future__ import annotations

import contextlib
import io
import itertools
import json
import sqlite3
import unittest
from datetime import date
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf
from tests.stockify.daemon.test_engine_log import enable_logging
from tests.stockify.daemon.test_finalise import DAY2, SHIFT, shifted
from tests.stockify.daemon.test_session_day import pack_records

from stockify import __main__ as cli, engine, paths, parity, store
from stockify.daemon import finalise, journal, lifecycle, session_day
from stockify.data import packs, refdata

SID = "p1"


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class _Base(pf.PaperDayTest):
    fut_full = False

    def setUp(self):
        super().setUp()
        self.record(seconds=120, fut_full=self.fut_full)
        sha = packs.build({"dataset": "ticks", "from": pf.EXPIRY, "to": pf.EXPIRY, "clock_cadence_ms": 5000})
        self.records = [r for r in pack_records(sha) if r[1] != packs.RANK[packs.SETTLE_K]]
        self.ticks = itertools.count(0, 10)

    def cfg(self, **over):
        act = [{"on_event": 60, "place": {"instrument": self.ces[4], "side": "BUY", "qty": 65, "product": "NRML",
                                          "type": "MARKET"}}]
        return self.session_cfg(strategy={"kind": "script", "actions": act}, **over)

    def finalised_day(self, sid=SID, cfg=None):
        cfg = cfg or self.cfg()
        store.create_session(sid, "s", cfg, 1)
        lifecycle.arm(sid, wall_ns=2)
        lifecycle.start_continuous(sid, wall_ns=3, engine_ns=3)
        sd = session_day.SessionDay(sid, pf.DAY, cfg, mono=lambda: next(self.ticks))
        self.addCleanup(sd.close)
        sd.open(None, [], self.t("09:14:00"))
        sd.feed(self.records, None)
        sd.close()
        res = finalise.finalise_day(sid, pf.DAY, files_complete=True, cutoff_passed=False)
        self.assertEqual((res.status, res.error), ("finalised", None))
        return cfg

    def tamper(self, sql: str, *args) -> None:
        conn = store._connect()
        with store._lock:
            conn.execute(sql, args)
            conn.commit()

    def tamper_row(self, table: str, key: str, field: str, value, sid=SID) -> None:
        """Change one field of the first stored row of `table` (a row_json table) for the fixture day."""
        row = store._one(f"SELECT {key} AS k, row_json FROM {table} WHERE session_id = ? AND trade_date = ? "
                         f"ORDER BY {key} LIMIT 1", (sid, pf.EXPIRY))
        data = json.loads(row["row_json"])
        data[field] = value
        self.tamper(f"UPDATE {table} SET row_json = ? WHERE session_id = ? AND trade_date = ? AND {key} = ?",
                    json.dumps(data, sort_keys=True), sid, pf.EXPIRY, row["k"])



class ParityJournalTest(_Base):
    def test_fixture_session_day_passes(self):
        """E-01."""
        self.finalised_day()
        jpath = paths.journal_file(SID, pf.DAY)
        before = jpath.read_bytes()
        res = parity.journal_parity(SID, pf.DAY)
        self.assertEqual((res.outcome, res.kind, res.session_id, res.day), ("pass", "journal", SID, pf.DAY))
        self.assertIsNone(res.summary["first_difference"])
        for table in ("orders", "order_events", "fills", "charges", "equity"):   # the call expired: no positions
            with self.subTest(table=table):
                self.assertGreater(res.summary["rows"][table], 0)
        row = store.get_session_day(SID, pf.EXPIRY)
        self.assertEqual(row["parity"], "pass")
        stored = store.get_parity_result(SID, pf.EXPIRY, "journal")
        self.assertEqual((stored["outcome"], stored["summary"]), ("pass", res.summary))
        self.assertEqual([r["detail"]["outcome"] for r in store.timeline(SID) if r["kind"] == "parity"], ["pass"])
        self.assertEqual(store.get_session(SID)["pause_reasons"], [])
        self.assertEqual(jpath.read_bytes(), before, "parity never writes a journal")

    def test_tampered_fill_fails_and_pauses(self):
        enable_logging(self)
        self.finalised_day()
        self.tamper_row("session_fills", "fill_id", "price_paise", 1)
        with self.assertLogs("stockify.parity", "ERROR") as logged:
            res = parity.journal_parity(SID, pf.DAY)
        self.assertEqual(res.outcome, "fail")
        first = res.summary["first_difference"]
        self.assertEqual((first["table"], first["stored"]["price_paise"]), ("fills", 1))
        self.assertNotEqual(first["regenerated"]["price_paise"], 1)
        self.assertTrue(any("fills" in m and SID in m for m in logged.output))
        self.assertEqual(store.get_session(SID)["pause_reasons"], ["parity_failure"])
        self.assertEqual(store.get_session_day(SID, pf.EXPIRY)["parity"], "fail")
        self.assertEqual(store.get_parity_result(SID, pf.EXPIRY, "journal")["outcome"], "fail")
        rows = [r for r in store.timeline(SID) if r["kind"] == "parity"]
        self.assertEqual([r["detail"]["outcome"] for r in rows], ["fail"])
        self.assertEqual(rows[0]["detail"]["first_difference"]["table"], "fills")
        # nothing is repaired: the stored row is as found, and a second run fails the same way
        self.assertEqual(store.session_rows(SID, "fills")[0]["price_paise"], 1)
        again = parity.journal_parity(SID, pf.DAY)
        self.assertEqual(again.summary["first_difference"], first)
        self.assertEqual(store.get_session(SID)["pause_reasons"], ["parity_failure"])

    def test_a_stopped_session_is_not_paused_but_the_failure_is_recorded(self):
        self.finalised_day()
        lifecycle.stop(SID, wall_ns=9)
        self.tamper_row("session_fills", "fill_id", "price_paise", 1)
        res = parity.journal_parity(SID, pf.DAY)
        self.assertEqual(res.outcome, "fail")
        self.assertEqual(store.get_session(SID)["status"], "STOPPED")
        self.assertEqual(store.get_session_day(SID, pf.EXPIRY)["parity"], "fail")
        self.assertEqual([r["detail"]["outcome"] for r in store.timeline(SID) if r["kind"] == "parity"], ["fail"])

    def test_unfinalised_day_is_skipped(self):
        cfg = self.cfg()
        store.create_session(SID, "s", cfg, 1)
        lifecycle.arm(SID, wall_ns=2)
        lifecycle.start_continuous(SID, wall_ns=3, engine_ns=3)
        sd = session_day.SessionDay(SID, pf.DAY, cfg, mono=lambda: next(self.ticks))
        self.addCleanup(sd.close)
        sd.open(None, [], self.t("09:14:00"))
        sd.feed(self.records, None)
        sd.close()                                              # a journal that is not finalised, a day that is open
        res = parity.journal_parity(SID, pf.DAY)
        self.assertEqual(res.outcome, "skipped")
        self.assertIn("not finalised", res.summary["reason"])
        self.assertIsNone(store.get_parity_result(SID, pf.EXPIRY, "journal"))
        self.assertIsNone(store.get_session_day(SID, pf.EXPIRY)["parity"])
        self.assertEqual(store.get_session(SID)["pause_reasons"], [])
        self.assertEqual([r for r in store.timeline(SID) if r["kind"] == "parity"], [])
        self.assertEqual(parity.journal_parity("nosuch", pf.DAY).outcome, "skipped")

    def test_cli_exit_codes(self):
        self.finalised_day()
        argv = ["parity", "journal", "--session", SID, "--date", pf.EXPIRY]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(argv), 0)
        self.assertNotIn("differs:", out.getvalue())
        self.tamper_row("session_fills", "fill_id", "price_paise", 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(argv), 1)
        lines = [ln for ln in out.getvalue().splitlines() if ln.startswith("differs:")]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("differs: fills "), lines[0])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["parity", "journal", "--session", SID, "--date", "2026-13-40"]), 2)
            self.assertEqual(cli.main(["parity", "journal", "--session", "nosuch", "--date", pf.EXPIRY]), 2)

    # ── every table is compared, and a missing or an extra row is a difference ───────────────────────────────────

    def test_every_table_is_compared(self):
        self.finalised_day()
        cases = [("orders", "session_orders", "order_id", "status", "REJECTED"),
                 ("order_events", "session_order_events", "event_no", "event", "CANCEL"),
                 ("fills", "session_fills", "fill_id", "qty", 1),
                 ("charges", "session_charges", "row_no", "total", "1")]
        for name, table, key, field, value in cases:
            with self.subTest(table=name):
                row = store._one(f"SELECT {key} AS k, row_json FROM {table} WHERE session_id = ? AND trade_date = ? "
                                 f"ORDER BY {key} LIMIT 1", (SID, pf.EXPIRY))
                data = json.loads(row["row_json"])
                self.assertIn(field, data)
                self.tamper_row(table, key, field, value)
                res = parity.journal_parity(SID, pf.DAY)
                self.assertEqual((res.outcome, res.summary["first_difference"]["table"]), ("fail", name))
                self.tamper(f"UPDATE {table} SET row_json = ? WHERE session_id = ? AND trade_date = ? AND {key} = ?",
                            row["row_json"], SID, pf.EXPIRY, row["k"])
        with self.subTest(table="positions"):          # the day ends flat (its call expired): a stored row is extra
            self.tamper("INSERT INTO session_positions_eod (session_id, trade_date, row_no, row_json) "
                        "VALUES (?, ?, 1, '{\"qty\": \"65\"}')", SID, pf.EXPIRY)
            res = parity.journal_parity(SID, pf.DAY)
            self.assertEqual((res.outcome, res.summary["first_difference"]["table"]), ("fail", "positions"))
            self.tamper("DELETE FROM session_positions_eod WHERE session_id = ?", SID)
        with self.subTest(table="equity"):
            self.tamper("UPDATE session_equity SET nav_paise = nav_paise + 1 WHERE session_id = ?", SID)
            res = parity.journal_parity(SID, pf.DAY)
            self.assertEqual((res.outcome, res.summary["first_difference"]["table"]), ("fail", "equity"))
            self.tamper("UPDATE session_equity SET nav_paise = nav_paise - 1 WHERE session_id = ?", SID)
        self.assertEqual(parity.journal_parity(SID, pf.DAY).outcome, "pass")

    def test_a_missing_and_an_extra_stored_row_are_differences(self):
        self.finalised_day()
        row = store._one("SELECT fill_id AS k, row_json FROM session_fills WHERE session_id = ? LIMIT 1", (SID,))
        self.tamper("DELETE FROM session_fills WHERE session_id = ? AND fill_id = ?", SID, row["k"])
        res = parity.journal_parity(SID, pf.DAY)
        first = res.summary["first_difference"]
        self.assertEqual((res.outcome, first["table"], first["stored"]), ("fail", "fills", None))
        self.tamper("INSERT INTO session_fills (session_id, trade_date, fill_id, row_json) VALUES (?, ?, ?, ?)",
                    SID, pf.EXPIRY, row["k"], row["row_json"])
        self.tamper("INSERT INTO session_fills (session_id, trade_date, fill_id, row_json) VALUES (?, ?, 99, ?)",
                    SID, pf.EXPIRY, row["row_json"])
        res = parity.journal_parity(SID, pf.DAY)
        first = res.summary["first_difference"]
        self.assertEqual((res.outcome, first["table"], first["regenerated"]), ("fail", "fills", None))

    def test_a_journal_the_engine_will_not_replay_is_a_failure_not_a_crash(self):
        self.finalised_day()
        with mock.patch.object(finalise, "replay_into", side_effect=journal.JournalError("bad crc")):
            res = parity.journal_parity(SID, pf.DAY)
        self.assertEqual(res.outcome, "fail")
        self.assertIn("bad crc", res.summary["error"])
        self.assertEqual(store.get_session(SID)["pause_reasons"], ["parity_failure"])
        self.assertEqual(store.get_session_day(SID, pf.EXPIRY)["parity"], "fail")

    def test_the_failure_is_pause_first_and_the_result_row_last(self):
        """A crash between the steps must leave a rerun that still pauses: the result row is written last."""
        self.finalised_day()
        self.tamper_row("session_fills", "fill_id", "price_paise", 1)
        with mock.patch.object(store, "put_parity_result", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                parity.journal_parity(SID, pf.DAY)
        self.assertEqual(store.get_session(SID)["pause_reasons"], ["parity_failure"])
        self.assertIsNone(store.get_parity_result(SID, pf.EXPIRY, "journal"))

    def test_parity_takes_no_daemon_lock_and_no_day_claim(self):
        self.finalised_day()
        claim = finalise.try_claim(finalise.claim_path(SID, pf.DAY), 0)
        self.assertIsNotNone(claim)
        try:
            self.assertEqual(parity.journal_parity(SID, pf.DAY).outcome, "pass")
        finally:
            claim.release()


class ParityTwoDayTest(_Base):
    def test_day2_parity_passes(self):
        """SOD from day 1: the day's bundle carries it."""
        act = [{"on_event": 60, "place": {"instrument": self.fut, "side": "BUY", "qty": 65, "product": "NRML",
                                          "type": "MARKET"}}]
        cfg = self.session_cfg(strategy={"kind": "script", "actions": act},
                               universe=["NSE:NIFTY 50", "NFO:NIFTY26NOVFUT"])
        store.create_session(SID, "s", cfg, 1)
        lifecycle.arm(SID, wall_ns=2)
        lifecycle.start_continuous(SID, wall_ns=3, engine_ns=3)
        clock = packs.RANK[packs.CLOCK]
        day1 = [r for r in self.records
                if packs.decode(r[2], 0)[0]["instrument_id"] in (self.idx, self.fut) or r[1] == clock]
        day2 = [(t + SHIFT, rank, shifted(rec, SHIFT)) for t, rank, rec in day1]
        with refdata.transaction() as conn:
            conn.execute("INSERT INTO fo_eod (trade_date, instrument_id, settle_paise) VALUES (?, ?, 2411000)",
                         (DAY2.isoformat(), self.fut))
        for day, recs, state, start in ((pf.DAY, day1, None, self.t("09:14:00")), (DAY2, day2, "next", None)):
            if state:
                state = finalise.next_sod_state(SID, day)
                start = self.t("09:14:00") + SHIFT
                lifecycle.start_continuous(SID, wall_ns=9, engine_ns=9)
            sd = session_day.SessionDay(SID, day, cfg, mono=lambda: next(self.ticks))
            self.addCleanup(sd.close)
            sd.open(state, [], start)
            sd.feed(recs, None)
            sd.close()
            self.assertEqual(finalise.finalise_day(SID, day, files_complete=True, cutoff_passed=False).status,
                             "finalised")
        self.assertIn("sod_state", json.loads(paths.day_bundle_file(SID, DAY2).read_text("utf-8")))
        for day in (pf.DAY, DAY2):
            with self.subTest(day=day):
                res = parity.journal_parity(SID, day)
                self.assertEqual((res.outcome, res.summary["first_difference"]), ("pass", None))
                self.assertEqual(store.get_session_day(SID, day.isoformat())["parity"], "pass")


if __name__ == "__main__":
    unittest.main()
