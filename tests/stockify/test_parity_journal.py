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
from stockify.config import loader
from stockify.daemon import finalise, journal, lifecycle, recovery, session_day
from stockify.daemon.clock import ManualClock
from stockify.daemon.dispatcher import Dispatcher
from stockify.daemon.encoder import FrameEncoder
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

    def test_parity_ignores_a_missing_note_on_an_old_stored_day(self):
        """CH 6.3: a stored day written before the note column has no `note` in its order rows; the regenerated twin has."""
        self.finalised_day()
        conn = store._connect()
        rows = conn.execute("SELECT session_id, trade_date, order_id, row_json FROM session_orders "
                            "WHERE session_id = ?", (SID,)).fetchall()
        self.assertTrue(rows)
        for r in rows:
            data = json.loads(r["row_json"])
            self.assertIn("note", data)
            data.pop("note")
            self.tamper("UPDATE session_orders SET row_json = ? WHERE session_id = ? AND trade_date = ? AND "
                        "order_id = ?", json.dumps(data, sort_keys=True), r["session_id"], r["trade_date"],
                        r["order_id"])
        res = parity.journal_parity(SID, pf.DAY)
        self.assertEqual(res.outcome, "pass", res.summary["first_difference"])
        self.tamper_row("session_orders", "order_id", "status", "REJECTED")     # a real difference still fails
        self.assertEqual(parity.journal_parity(SID, pf.DAY).outcome, "fail")

    def test_the_note_rule_for_stored_orders(self):
        """D-93: a stored order without the `note` key (written before CH 6.2) is compared on every other field; one
        with the key compares exactly. The twin of an AUTO_SQOFF order carries the bundle's system note."""
        sq = "Closed by the broker's 15:20 auto square-off"
        twin = {7: {"order_id": "7", "tag": "AUTO_SQOFF", "qty": "50", "note": sq}}
        old = {7: {"order_id": "7", "tag": "AUTO_SQOFF", "qty": "50"}}
        self.assertIsNone(parity._first_difference("orders", old, twin))
        wrong_qty = {7: {"order_id": "7", "tag": "AUTO_SQOFF", "qty": "51"}}
        self.assertEqual(parity._first_difference("orders", wrong_qty, twin)["key"], 7)
        self.assertIsNone(parity._first_difference("orders", {7: {**old[7], "note": sq}}, twin))
        wrong_note = parity._first_difference("orders", {7: {**old[7], "note": "something else"}}, twin)
        self.assertEqual((wrong_note["table"], wrong_note["key"]), ("orders", 7))
        blank = parity._first_difference("orders", {7: {**old[7], "note": ""}}, twin)
        self.assertIsNotNone(blank, "a stored note of '' is a stored note")
        self.assertIsNone(parity._first_difference("orders", {7: old[7]}, {7: {**twin[7], "note": ""}}))
        self.assertIsNotNone(parity._first_difference("fills", {1: {"a": 1}}, {1: {"a": 1, "note": "x"}}))

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

    # -- fix round 1 -----------------------------------------------------------------------------------------------

    def test_a_tampered_eod_state_fails_parity(self):
        """It seeds the next day, so it is compared byte for byte."""
        self.finalised_day()
        self.tamper("UPDATE session_days SET eod_state_json = eod_state_json || ' ' WHERE session_id = ?", SID)
        res = parity.journal_parity(SID, pf.DAY)
        first = res.summary["first_difference"]
        self.assertEqual((res.outcome, first["table"]), ("fail", "eod_state"))
        self.assertEqual(store.get_session(SID)["pause_reasons"], ["parity_failure"])

    def test_a_repeat_of_the_same_mismatch_is_one_still_open_row(self):
        enable_logging(self)
        self.finalised_day()
        self.tamper("UPDATE session_days SET eod_state_json = eod_state_json || ' ' WHERE session_id = ?", SID)
        with self.assertLogs("stockify.parity", "ERROR"):
            parity.journal_parity(SID, pf.DAY)
        with self.assertNoLogs("stockify.parity", "ERROR"):
            again = parity.journal_parity(SID, pf.DAY)
        self.assertEqual(again.outcome, "fail")
        kinds = [r["kind"] for r in store.timeline(SID)]
        self.assertEqual((kinds.count("parity"), kinds.count("parity_still_open")), (1, 1))
        self.assertEqual(store.get_session(SID)["pause_reasons"], ["parity_failure"])
        self.tamper_row("session_fills", "fill_id", "price_paise", 1)                # a different mismatch
        with self.assertLogs("stockify.parity", "ERROR"):
            other = parity.journal_parity(SID, pf.DAY)
        self.assertEqual(other.summary["first_difference"]["table"], "fills")
        kinds = [r["kind"] for r in store.timeline(SID)]
        self.assertEqual((kinds.count("parity"), kinds.count("parity_still_open")), (2, 1))

    def test_cli_exit_codes_not_finalised_and_unexpected_error(self):
        store.create_session(SID, "s", self.cfg(), 1)
        argv = ["parity", "journal", "--session", SID, "--date", pf.EXPIRY]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(argv), 3)
        self.assertIn("not finalised", err.getvalue())
        err = io.StringIO()
        with mock.patch.object(parity, "journal_parity", side_effect=sqlite3.OperationalError("database is locked")):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(argv), 4)
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertIn("database is locked", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_a_changed_strategy_fails_parity_naming_both_hashes_and_is_never_replayed(self):
        """I-1: a day is not replayed with other code than it was made with."""
        from stockify import bundle as bundle_mod
        self.finalised_day()
        stored = json.loads(paths.day_bundle_file(SID, pf.DAY).read_text(encoding="utf-8"))["strategy"]["source_sha256"]
        real = bundle_mod.strategy_ref
        with mock.patch.object(bundle_mod, "strategy_ref",
                               side_effect=lambda cfg: {**real(cfg), "source_sha256": "e" * 64}):
            res = parity.journal_parity(SID, pf.DAY)
        self.assertEqual(res.outcome, "fail")
        self.assertIn(stored, res.summary["error"])
        self.assertIn("e" * 64, res.summary["error"])
        rows = [r for r in store.timeline(SID, limit=100) if r["kind"] == "strategy_changed"]
        self.assertEqual([(r["detail"]["stored"], r["detail"]["current"]) for r in rows], [(stored, "e" * 64)])

    def test_an_unloadable_engine_does_not_mask_the_days_own_error(self):
        self.finalised_day()
        with mock.patch.object(engine, "load", side_effect=ImportError("not built")), \
                mock.patch.object(finalise, "fresh_engine", side_effect=finalise.FinaliseError("real")):
            res = parity.journal_parity(SID, pf.DAY)
        self.assertEqual(res.outcome, "fail")
        self.assertIn("real", res.summary["error"])


class ParityTwoDayTest(_Base):
    fut_full = True

    def test_day2_parity_passes(self):
        """SOD from day 1: the day's bundle carries it."""
        act = [{"on_event": 60, "place": {"instrument": self.fut, "side": "BUY", "qty": 65, "product": "NRML",
                                          "type": "MARKET"}}]
        cfg = self.session_cfg(strategy={"kind": "script", "actions": act}, capital_paise=100_000_000,
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
        sod = json.loads(paths.day_bundle_file(SID, DAY2).read_text("utf-8"))["sod_state"]
        self.assertTrue(sod["ledger"]["positions"], "a real position was carried into day two")
        for day in (pf.DAY, DAY2):
            with self.subTest(day=day):
                res = parity.journal_parity(SID, day)
                self.assertEqual((res.outcome, res.summary["first_difference"]), ("pass", None))
                self.assertEqual(store.get_session_day(SID, day.isoformat())["parity"], "pass")
        self.assertGreaterEqual(parity.journal_parity(SID, pf.DAY).summary["rows"]["positions"], 1)
        row = store._one("SELECT row_json FROM session_positions_eod WHERE session_id = ? AND trade_date = ?",
                         (SID, pf.EXPIRY))
        data = json.loads(row["row_json"])
        data["qty"] = str(int(data["qty"]) + 1)
        self.tamper("UPDATE session_positions_eod SET row_json = ? WHERE session_id = ? AND trade_date = ?",
                    json.dumps(data, sort_keys=True), SID, pf.EXPIRY)
        res = parity.journal_parity(SID, pf.DAY)
        self.assertEqual((res.outcome, res.summary["first_difference"]["table"]), ("fail", "positions"))


class ParityPauseTest(_Base):
    """Where the parity pause goes: the store alone when no later day is open, the command queue (journal first)
    when one is."""

    def setUp(self):
        super().setUp()
        self.cfg2 = self.session_cfg(strategy={"kind": "script", "actions": []},
                                     universe=["NSE:NIFTY 50", "NFO:NIFTY26NOVFUT"])
        store.create_session(SID, "s", self.cfg2, 1)
        lifecycle.arm(SID, wall_ns=2)
        lifecycle.start_continuous(SID, wall_ns=3, engine_ns=3)
        clock = packs.RANK[packs.CLOCK]
        day1 = [r for r in self.records
                if packs.decode(r[2], 0)[0]["instrument_id"] in (self.idx, self.fut) or r[1] == clock]
        with refdata.transaction() as conn:
            conn.execute("INSERT INTO fo_eod (trade_date, instrument_id, settle_paise) VALUES (?, ?, 2411000)",
                         (DAY2.isoformat(), self.fut))
        sd = session_day.SessionDay(SID, pf.DAY, self.cfg2, mono=lambda: next(self.ticks))
        self.addCleanup(sd.close)
        sd.open(None, [], self.t("09:14:00"))
        sd.feed(day1, None)
        sd.close()
        self.assertEqual(finalise.finalise_day(SID, pf.DAY, files_complete=True, cutoff_passed=False).status,
                         "finalised")
        self.tamper("UPDATE session_days SET eod_state_json = eod_state_json || ' ' WHERE session_id = ?", SID)

    def tamper(self, sql, *args):
        conn = store._connect()
        with store._lock:
            conn.execute(sql, args)
            conn.commit()

    def open_day2(self, reasons=()):
        sd = session_day.SessionDay(SID, DAY2, self.cfg2, mono=lambda: next(self.ticks))
        self.addCleanup(sd.close)
        sd.open(finalise.next_sod_state(SID, DAY2), list(reasons), self.t("09:14:00") + SHIFT)
        return sd

    def controls(self, day):
        _, body, _ = journal.read(paths.journal_file(SID, day))
        out, off = [], 0
        while off < len(body):
            rec, off = packs.decode(body, off)
            if rec["kind"] == "REFDATA" and rec["subtype"] == "strategy_control":
                out.append(rec["payload"])
        return out

    def test_the_next_day_opens_paused(self):
        self.assertEqual(parity.journal_parity(SID, pf.DAY).outcome, "fail")
        self.assertEqual(store.get_session(SID)["pause_reasons"], ["parity_failure"])
        self.assertEqual(store.pending_commands(), [])                      # no later day: the store carries it
        lifecycle.start_continuous(SID, wall_ns=9, engine_ns=9)
        self.assertEqual(store.get_session(SID)["status"], "PAUSED")
        sd = self.open_day2(store.get_session(SID)["pause_reasons"])
        sd.close()
        self.assertIn("parity_failure", json.dumps(self.controls(DAY2)))

    def test_a_later_open_day_is_paused_through_the_journal_and_a_restart_keeps_it(self):
        lifecycle.start_continuous(SID, wall_ns=9, engine_ns=9)
        sd2 = self.open_day2()
        res = parity.journal_parity(SID, pf.DAY)
        self.assertEqual(res.outcome, "fail")
        self.assertEqual(store.get_session(SID)["pause_reasons"], [], "not a store-only pause")
        cmds = store.pending_commands()
        self.assertEqual([(c["command"], c["args"]) for c in cmds], [("pause", {"reason": "parity_failure"})])
        parity.journal_parity(SID, pf.DAY)                                   # a repeat queues nothing more
        self.assertEqual(len(store.pending_commands()), 1)
        t = self.t("09:30:00") + SHIFT
        disp = Dispatcher([sd2], FrameEncoder({}), clock=ManualClock(t), cfg=dict(loader.load_json("paper.json")),
                          replay=True)
        disp.apply_pending(t)
        self.assertEqual(store.pending_commands(), [])
        self.assertIn("parity_failure", json.dumps(self.controls(DAY2)[-1]))
        self.assertEqual(store.get_session(SID)["pause_reasons"], ["parity_failure"])
        sd2.close()
        sd3, _ = recovery.recover(SID, DAY2, clock=ManualClock(self.t("09:40:00") + SHIFT))
        self.addCleanup(sd3.close)
        got = store.get_session(SID)
        self.assertEqual((got["status"], got["pause_reasons"]), ("PAUSED", ["parity_failure"]))


if __name__ == "__main__":
    unittest.main()
