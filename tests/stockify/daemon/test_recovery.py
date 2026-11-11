"""GK 5.4 (M5.4; the brief's GK 5.2 name was taken): a restarted daemon replays today's journal and checks it against
the stored rows; a crash at any step of journal, engine or store leaves a day that recovery brings to the rows an
uninterrupted run gives. The owner's salvage tool for a bad-then-good journal record never runs by itself."""
from __future__ import annotations

import hashlib
import itertools
import json
import struct
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf
from tests.stockify.daemon.test_dispatcher import throughput_frame
from tests.stockify.daemon.test_engine_log import enable_logging
from tests.stockify.daemon.test_session_day import pack_records

from stockify import engine, paths, store
from stockify.config import loader
from stockify.daemon import exitcodes, finalise, journal, lifecycle, market_day, recovery, session_day
from stockify.daemon.clock import ManualClock
from stockify.daemon.dispatcher import Dispatcher
from stockify.daemon.encoder import FrameEncoder
from stockify.daemon.replay_ticker import ReplayTicker
from stockify.data import packs, refdata

SID = "s1"
DAY2 = date(2026, 11, 18)
SEC = pf.SEC
SHIFT = 86_400 * SEC
CHUNK = 13
TABLES = ("orders", "order_events", "fills", "charges")


def journal_records(sid: str, day: date = pf.DAY) -> list[dict]:
    _, body, _ = journal.read(paths.journal_file(sid, day))
    out, off = [], 0
    while off < len(body):
        rec, off = packs.decode(body, off)
        out.append(rec)
    return out


def record_ends(sid: str, day: date = pf.DAY) -> list[int]:
    """The file offset at which each whole journal record ends."""
    data = paths.journal_file(sid, day).read_bytes()
    out, off = [], journal.HEADER_SIZE
    while off < len(data):
        off += struct.unpack_from("<H", data, off + 2)[0]
        out.append(off)
    return out


def die_after(real):
    """An `_apply_outputs` that applies everything and then dies, before the transaction commits."""
    def apply(*a, **kw):
        real(*a, **kw)
        raise OSError("died inside the transaction")
    return apply


class DyingEngine:
    """The real engine, whose push fails."""

    def __init__(self, real):
        self._real = real

    def push(self, batch):
        raise RuntimeError("the engine died")

    def __getattr__(self, name):
        return getattr(self._real, name)


class FlakyFile:
    """Stands in for a journal file object: the Nth write lands only `keep` bytes, then raises."""

    def __init__(self, real, keep: int):
        self._real, self._keep = real, keep

    def write(self, data):
        self._real.write(data[:self._keep])
        self._real.flush()
        raise OSError("disk went away")

    def __getattr__(self, name):
        return getattr(self._real, name)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class RecoveryBase(pf.PaperDayTest):
    FUT_FULL = False                       # the future has a book to trade against (a carried position needs one)

    def setUp(self):
        super().setUp()
        self.record(seconds=120, fut_full=self.FUT_FULL)
        sha = packs.build({"dataset": "ticks", "from": pf.EXPIRY, "to": pf.EXPIRY, "clock_cadence_ms": 5000})
        self.records = [r for r in pack_records(sha) if r[1] != packs.RANK[packs.SETTLE_K]]
        self.ticks = itertools.count(0, 10)
        self.cfg = self.session_cfg(strategy={"kind": "script", "actions": [
            {"on_event": 60, "place": {"instrument": self.ces[4], "side": "BUY", "qty": 195, "product": "NRML",
                                       "type": "LIMIT", "price": 11620}},
            {"on_event": 90, "place": {"instrument": self.ces[4], "side": "BUY", "qty": 65, "product": "NRML",
                                       "type": "MARKET"}}]})
        self._refs: dict[int, dict] = {}

    # ── helpers ──────────────────────────────────────────────────────────────────────────────────────────────────
    def make_session(self, sid=SID, cfg=None):
        store.create_session(sid, sid, cfg or self.cfg, 1)
        lifecycle.arm(sid, wall_ns=2)
        lifecycle.start_continuous(sid, wall_ns=3, engine_ns=3)

    def day(self, sid=SID, day=pf.DAY, cfg=None):
        sd = session_day.SessionDay(sid, day, cfg or self.cfg, mono=lambda: next(self.ticks))
        self.addCleanup(sd.close)
        return sd

    def live(self, sid=SID, records=None, upto=None, marks=False):
        """A session fed (in chunks) and left unfinalised and open. With `marks`, each chunk stores the frame mark
        (chunk number, t of its last record)."""
        self.make_session(sid)
        sd = self.day(sid)
        sd.open(None, [], self.t("09:14:00"))
        recs = self.records if records is None else records
        for i in range(0, len(recs) if upto is None else upto, CHUNK):
            chunk = recs[i:min(i + CHUNK, upto or len(recs))]
            sd.feed(chunk, (i // CHUNK + 1, chunk[-1][0]) if marks else None)
        return sd

    def landing_mark(self, t_ns: int):
        """The landed frame (frame_no, recv_ns) the journal's last record at `t_ns` belongs to, read from the day's
        landing file directly."""
        import sqlite3
        conn = sqlite3.connect(str(paths.ticks_landing_file(pf.DAY)))
        try:
            return conn.execute("SELECT frame_no, MAX(recv_ns) FROM packets WHERE recv_ns <= ? AND frame_no > 0",
                                (t_ns,)).fetchone()
        finally:
            conn.close()

    def rows(self, sid=SID, day=pf.EXPIRY) -> dict:
        return {t: store.session_rows(sid, t, trade_date=day) for t in TABLES}

    def ref(self, n: int) -> dict:
        """The rows an uninterrupted run gives after its first `n` journal records."""
        if n not in self._refs:
            sid = f"ref{n}"
            self.live(sid, self.records[:n], n)
            self._refs[n] = self.rows(sid)
        return self._refs[n]

    def recover(self, sid=SID, day=pf.DAY, at="09:40:00"):
        sd, res = recovery.recover(sid, day, clock=ManualClock(self.t(at)))
        self.addCleanup(sd.close)
        return sd, res

    def kinds(self, sid=SID) -> list[str]:
        return [r["kind"] for r in store.timeline(sid)]

    def timeline(self, sid, kind) -> list[dict]:
        return [r["detail"] for r in store.timeline(sid) if r["kind"] == kind]


class RecoveryTest(RecoveryBase):
    # ── the brief's tests ────────────────────────────────────────────────────────────────────────────────────────
    def test_recovery_writes_rows_missing_from_db(self):
        sd = self.live(upto=CHUNK * 5)
        n = sd.journal_records
        with mock.patch.object(store, "apply_session_outputs", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sd.feed(self.records[n:n + CHUNK], None)          # journal and engine are one batch ahead of the store
        sd.close()
        full = self.ref(len(self.records))
        stale = self.rows()
        self.assertNotEqual(stale, self.ref(n + CHUNK))
        sd2, res = self.recover()
        self.assertEqual((res.session_id, res.day, res.records, res.mismatch), (SID, pf.DAY, n + CHUNK, None))
        self.assertGreater(res.written, 0)
        self.assertEqual(res.tail_lost_rows, 0)
        self.assertEqual(self.rows(), self.ref(n + CHUNK))
        sd2.feed(self.records[n + CHUNK:], None)                   # and the day goes on
        self.assertEqual(self.rows(), full)
        self.assertEqual(self.timeline(SID, "recovered")[-1]["written"], res.written)

    def test_recovery_db_ahead_of_journal_is_tail_lost_not_parity(self):
        self.live()
        keep = 75                                                  # the order is placed by then; its fills are beyond
        self.assertNotEqual(self.ref(keep), self.rows())
        jp = paths.journal_file(SID, pf.DAY)
        with open(jp, "r+b") as f:
            f.truncate(record_ends(SID)[keep - 1] + 7)             # a record and a torn piece of the next: power loss
        sd, res = self.recover()
        self.assertIsNone(res.mismatch)
        self.assertEqual(res.records, keep)
        self.assertGreater(res.tail_lost_rows, 0)
        self.assertEqual(self.rows(), self.ref(keep))
        lost = self.timeline(SID, "journal_tail_lost")
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0]["rows"], res.tail_lost_rows)
        self.assertEqual(store.get_session(SID)["pause_reasons"], [])
        self.assertNotIn("determinism_mismatch", self.kinds())
        self.assertEqual(store.get_session(SID)["status"], "RUNNING")
        self.assertTrue(journal.torn_entries(jp))                  # the torn bytes are kept, never deleted

    def test_recovery_mismatch_pauses_with_determinism(self):
        self.live()
        before = self.rows()
        conn = store._connect()
        row = before["fills"][0]
        tampered = {**row, "price_paise": row["price_paise"] + 1}
        conn.execute("UPDATE session_fills SET row_json = ? WHERE session_id = ? AND fill_id = ?",
                     (json.dumps(tampered, sort_keys=True), SID, row["fill_id"]))
        conn.commit()
        enable_logging(self)
        with self.assertLogs("stockify.daemon.recovery", "ERROR") as cm:
            sd, res = self.recover()
        self.assertIn("determinism", "\n".join(cm.output))
        self.assertIsNotNone(res.mismatch)
        self.assertIn(f"fills[{row['fill_id']}]", res.mismatch)
        s = store.get_session(SID)
        self.assertEqual((s["status"], s["pause_reasons"]), ("PAUSED", ["determinism"]))
        detail = self.timeline(SID, "determinism_mismatch")
        self.assertEqual(len(detail), 1)
        self.assertEqual(detail[0]["table"], "fills")
        self.assertEqual(detail[0]["stored"], tampered)
        self.assertEqual(detail[0]["regenerated"], row)
        after = self.rows()
        self.assertEqual(after["fills"][0], tampered)              # the evidence is left as it was found
        self.assertEqual(after["order_events"], before["order_events"])
        last = journal_records(SID)[-1]                            # the journal says the strategy is off, first
        self.assertEqual((last["kind"], last["subtype"]), ("REFDATA", "strategy_control"))
        self.assertEqual(last["payload"], {"enabled": False, "reasons": ["determinism"]})

    def test_pending_stop_all_applies_before_first_push_after_restart(self):
        sd = self.live(upto=CHUNK * 4)
        n = sd.journal_records
        sd.close()
        store.add_command(SID, "stop_all", None, 5)
        sd2, res = self.recover()
        disp = Dispatcher([sd2], FrameEncoder({}), clock=ManualClock(self.t("09:41:00")),
                          cfg=loader.load_json("paper.json"), replay=True)
        pushes = []
        real = sd2.engine
        sd2.engine = mock.Mock(wraps=real, push=lambda b: (pushes.append(len(b)), real.push(b))[1])
        disp.start(self.t("09:41:00"))
        recs = journal_records(SID)
        self.assertEqual(len(recs), n + 1)
        self.assertEqual((recs[-1]["subtype"], recs[-1]["payload"]), ("strategy_control",
                                                                       {"enabled": False, "reasons": ["stop_all"]}))
        self.assertEqual(len(pushes), 1)                           # the control record only, nothing live before it
        self.assertEqual(store.pending_commands(), [])
        s = store.get_session(SID)
        self.assertEqual((s["status"], s["pause_reasons"]), ("PAUSED", ["stop_all"]))

    # ── crashes at every step ────────────────────────────────────────────────────────────────────────────────────
    def crash(self, sid: str, how: str) -> None:
        """Feed a live day to the 3rd chunk, then make the 4th die at `how`, leaving the day as a crash would."""
        sd = self.live(sid, upto=CHUNK * 3)
        batch = self.records[CHUNK * 3:CHUNK * 4]
        w = sd._journal
        patches = {
            "append refused": lambda: mock.patch.object(journal.JournalWriter, "append",
                                                        side_effect=journal.JournalError("refused")),
            "sidecar ahead of the records": lambda: mock.patch.object(w, "_f", FlakyFile(w._f, 0)),
            "half a batch of records": lambda: mock.patch.object(w, "_f", FlakyFile(w._f, 1500)),
            "one record and a piece": lambda: mock.patch.object(w, "_f", FlakyFile(w._f, 400 + 31)),
            "fsync fails": lambda: mock.patch.object(journal.os, "fsync", side_effect=OSError("sync")),
            "engine push dies": lambda: mock.patch.object(sd, "engine", DyingEngine(sd.engine)),
            "store refuses": lambda: mock.patch.object(store, "apply_session_outputs", side_effect=OSError("store")),
            "store dies mid-transaction": lambda: mock.patch.object(store, "_apply_outputs", die_after(
                store._apply_outputs)),
        }
        if how == "fsync fails":
            w._every = 0
        with patches[how]():
            with self.assertRaises(Exception):
                sd.feed(batch, None)
        sd.close()

    CRASHES = ("append refused", "sidecar ahead of the records", "half a batch of records", "one record and a piece",
               "fsync fails", "engine push dies", "store refuses", "store dies mid-transaction")

    def test_a_crash_at_any_journal_engine_or_store_step_recovers_to_the_uninterrupted_run(self):
        full = self.ref(len(self.records))
        for i, how in enumerate(self.CRASHES):
            with self.subTest(how):
                sid = f"c{i}"
                self.crash(sid, how)
                sd, res = self.recover(sid)
                self.assertIsNone(res.mismatch)
                self.assertEqual(res.tail_lost_rows, 0)
                self.assertEqual(res.records, len(journal_records(sid)))
                self.assertGreaterEqual(res.records, CHUNK * 3)
                self.assertEqual(self.rows(sid), self.ref(res.records))
                self.assertEqual(store.get_session(sid)["pause_reasons"], [])
                sd.feed(self.records[res.records:], None)           # the journal is one record per input: carry on
                self.assertEqual(self.rows(sid), full)
                self.assertEqual(len(journal_records(sid)), len(self.records))
                sd.close()
                self.assertEqual(journal.read(paths.journal_file(sid, pf.DAY))[2], 0)

    def test_a_torn_journal_tail_is_set_aside_and_recovery_goes_on(self):
        self.crash("t0", "half a batch of records")
        jp = paths.journal_file("t0", pf.DAY)
        self.assertGreater(journal.read(jp)[2], 0)                  # the crash left a torn tail
        sd, res = self.recover("t0")
        self.assertIsNone(res.mismatch)
        self.assertTrue(journal.torn_entries(jp))
        self.assertEqual(journal.read(jp)[2], 0)
        self.assertEqual(self.rows("t0"), self.ref(res.records))

    def test_a_crash_while_recovering_changes_nothing_and_the_next_recovery_finishes(self):
        sd = self.live(upto=CHUNK * 5)
        n = sd.journal_records
        with mock.patch.object(store, "apply_session_outputs", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sd.feed(self.records[n:n + CHUNK], None)
        sd.close()
        stale, size = self.rows(), paths.journal_file(SID, pf.DAY).stat().st_size
        with mock.patch.object(store, "replace_session_outputs", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                recovery.recover(SID, pf.DAY, clock=ManualClock(self.t("09:40:00")))
        self.assertEqual(self.rows(), stale)                        # the store is as it was
        self.assertEqual(paths.journal_file(SID, pf.DAY).stat().st_size, size)    # and the journal was never written
        with mock.patch.object(store, "_apply_outputs", die_after(store._apply_outputs)):
            with self.assertRaises(OSError):
                recovery.recover(SID, pf.DAY, clock=ManualClock(self.t("09:40:00")))
        self.assertEqual(self.rows(), stale)                        # a transaction that dies before it commits rolls back
        sd2, res = self.recover()
        self.assertEqual((res.mismatch, res.records), (None, n + CHUNK))
        self.assertEqual(self.rows(), self.ref(n + CHUNK))

    def test_a_crash_between_the_pause_record_and_the_pause_repeats_it_on_the_next_start(self):
        self.live()
        conn = store._connect()
        fill = self.rows()["fills"][0]
        conn.execute("UPDATE session_fills SET row_json = ? WHERE session_id = ? AND fill_id = ?",
                     (json.dumps({**fill, "qty": fill["qty"] + 1}, sort_keys=True), SID, fill["fill_id"]))
        conn.commit()
        with mock.patch.object(lifecycle, "pause", side_effect=OSError("died")):
            with self.assertRaises(OSError):
                recovery.recover(SID, pf.DAY, clock=ManualClock(self.t("09:40:00")))
        self.assertEqual(store.get_session(SID)["pause_reasons"], [])           # the store did not change ...
        self.assertEqual(journal_records(SID)[-1]["subtype"], "strategy_control")  # ... the journal's record did
        sd, res = self.recover()
        self.assertIsNotNone(res.mismatch)
        self.assertEqual(store.get_session(SID)["pause_reasons"], ["determinism"])

    def test_recovery_refuses_a_journal_with_a_bad_record_before_good_ones_and_touches_nothing(self):
        self.live()
        jp = paths.journal_file(SID, pf.DAY)
        ends = record_ends(SID)
        data = bytearray(jp.read_bytes())
        data[ends[20] + 30] ^= 0xFF
        jp.write_bytes(bytes(data))
        before = (jp.read_bytes(), self.rows())
        with self.assertRaises(journal.JournalError):
            recovery.recover(SID, pf.DAY, clock=ManualClock(self.t("09:40:00")))
        self.assertEqual((jp.read_bytes(), self.rows()), before)

    def test_a_finalised_day_recovers_replay_only(self):
        sd = self.live()
        sd.close(finalise=True)
        sd2, res = self.recover()
        self.assertEqual((res.records, res.mismatch, res.written, res.tail_lost_rows), (len(self.records), None, 0, 0))
        with self.assertRaises(session_day.SessionDayError):
            sd2.feed(self.records[:1], None)

    def test_a_clean_restart_writes_nothing_and_says_so_on_the_timeline(self):
        self.live().close()
        before = self.rows()
        sd, res = self.recover()
        self.assertEqual((res.written, res.tail_lost_rows, res.mismatch), (0, 0, None))
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.timeline(SID, "recovered"), [{"day": pf.EXPIRY, "records": len(self.records),
                                                             "written": 0, "tail_lost_rows": 0, "mismatch": None}])

    def test_the_log_says_how_many_records_and_mismatches(self):
        self.live().close()
        enable_logging(self)
        with self.assertLogs("stockify.daemon.recovery", "INFO") as cm:
            self.recover()
        self.assertIn(f"recovered {SID}: {len(self.records)} records, 0 mismatches", "\n".join(cm.output))

    def test_a_stopped_session_with_a_mismatch_is_reported_not_paused(self):
        self.live()
        lifecycle.stop(SID, wall_ns=9)
        conn = store._connect()
        fill = self.rows()["fills"][0]
        conn.execute("UPDATE session_fills SET row_json = ? WHERE session_id = ? AND fill_id = ?",
                     (json.dumps({**fill, "qty": fill["qty"] + 1}, sort_keys=True), SID, fill["fill_id"]))
        conn.commit()
        sd, res = self.recover()
        self.assertIsNotNone(res.mismatch)
        self.assertEqual(store.get_session(SID)["status"], "STOPPED")
        self.assertEqual(len(self.timeline(SID, "determinism_mismatch")), 1)


    # ── the frame mark (GK 5.5) ──────────────────────────────────────────────────────────────────────────────────
    def test_a_repair_keeps_the_frame_mark_when_the_store_was_behind_and_finalise_reads_it(self):
        sd = self.live(upto=CHUNK * 5, marks=True)
        n = sd.journal_records
        mark = store.session_last_frame(SID, pf.EXPIRY)
        self.assertEqual(mark[0], 5)
        with mock.patch.object(store, "apply_session_outputs", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sd.feed(self.records[n:n + CHUNK], (6, self.records[n + CHUNK - 1][0]))
        sd.close()
        sd2, res = self.recover()
        self.assertGreater(res.written, 0)
        self.assertEqual(store.session_last_frame(SID, pf.EXPIRY), mark)        # never NULL after a repair
        sd2.feed(self.records[n + CHUNK:], None)
        sd2.close()
        self.assertEqual(finalise.finalise_day(SID, pf.DAY, files_complete=True, cutoff_passed=False).status,
                         "finalised")
        self.assertEqual(store.session_last_frame(SID, pf.EXPIRY), mark)        # finalise carried it over its replay

    def test_a_repair_sets_the_frame_mark_to_the_journals_last_frame_when_the_store_was_ahead(self):
        self.live(marks=True)
        stale = store.session_last_frame(SID, pf.EXPIRY)
        keep = 75
        with open(paths.journal_file(SID, pf.DAY), "r+b") as f:
            f.truncate(record_ends(SID)[keep - 1] + 7)
        sd, res = self.recover()
        self.assertGreater(res.tail_lost_rows, 0)
        mark = store.session_last_frame(SID, pf.EXPIRY)
        self.assertIsNotNone(mark)
        last_t = journal_records(SID)[-1]["t_event"]
        self.assertLessEqual(mark[1], last_t)
        self.assertLess(mark[1], stale[1])
        self.assertEqual(mark, tuple(self.landing_mark(last_t)))

    # ── a determinism pause that stays open (GK 5.5) ─────────────────────────────────────────────────────────────
    def tamper(self, field="price_paise"):
        conn = store._connect()
        row = self.rows()["fills"][0]
        conn.execute("UPDATE session_fills SET row_json = ? WHERE session_id = ? AND fill_id = ?",
                     (json.dumps({**row, field: row[field] + 1}, sort_keys=True), SID, row["fill_id"]))
        conn.commit()

    def test_the_mismatch_names_the_repair_path_the_session_and_the_day(self):
        self.live()
        self.tamper()
        enable_logging(self)
        with self.assertLogs("stockify.daemon.recovery", "ERROR") as cm:
            sd, res = self.recover()
        text = "\n".join(cm.output)
        for word in ("SessionDay.replay()", SID, pf.EXPIRY, "journal is the truth"):
            self.assertIn(word, text)
        detail = self.timeline(SID, "determinism_mismatch")[0]
        self.assertIn("SessionDay.replay()", detail["repair"])
        self.assertEqual((detail["session_id"], detail["day"]), (SID, pf.EXPIRY))
        self.assertRegex(detail["digest"], "^[0-9a-f]{64}$")

    def test_an_identical_mismatch_on_a_later_restart_writes_one_still_open_row_and_nothing_else(self):
        self.live()
        self.tamper()
        sd, res = self.recover()
        sd.close()
        digest = self.timeline(SID, "determinism_mismatch")[0]["digest"]
        for restart in (1, 2):
            sd, res2 = self.recover()
            sd.close()
            self.assertEqual(res2.mismatch, res.mismatch)
            self.assertEqual(len(self.timeline(SID, "determinism_mismatch")), 1)
            self.assertEqual(len(self.timeline(SID, "recovered")), 1)
            still = self.timeline(SID, "determinism_still_open")
            self.assertEqual(len(still), restart)
            self.assertEqual(still[-1]["digest"], digest)
        pauses = [r for r in journal_records(SID) if r["kind"] == "REFDATA" and r["subtype"] == "strategy_control"]
        self.assertEqual(len(pauses), 1)                            # the pause is journaled once
        self.tamper("qty")                                          # a different disagreement is new news
        sd, res3 = self.recover()
        self.assertEqual(len(self.timeline(SID, "determinism_mismatch")), 2)
        self.assertNotEqual(self.timeline(SID, "determinism_mismatch")[1]["digest"], digest)


class PauseReconcileTest(RecoveryBase):
    """LCB 5.24 (M-3): the journal is the truth for the pause reasons too (D-68: journal first, then store)."""

    def reasons_journaled(self):
        return [r["payload"]["reasons"] for r in journal_records(SID)
                if r["kind"] == "REFDATA" and r["subtype"] == "strategy_control"]

    def test_journal_paused_store_running_is_repaired_to_paused(self):
        sd = self.live(upto=CHUNK * 2)
        sd.control(False, ["owner", "token_expired"], self.t("09:30:00"))     # journaled; the store write never happened
        sd.close()
        self.assertEqual(store.get_session(SID)["pause_reasons"], [])
        sd2, _ = self.recover()
        got = store.get_session(SID)
        self.assertEqual((got["status"], got["pause_reasons"]), ("PAUSED", ["owner", "token_expired"]))
        self.assertTrue([r for r in store.timeline(SID) if r["kind"] == "pause_reconciled"])

    def test_journal_resumed_store_paused_is_repaired_to_running(self):
        sd = self.live(upto=CHUNK * 2)
        sd.control(False, ["token_expired"], self.t("09:30:00"))
        lifecycle.pause(SID, "token_expired", wall_ns=5)
        sd.control(True, [], self.t("09:31:00"))                               # journaled; the store write never happened
        sd.close()
        sd2, _ = self.recover()
        got = store.get_session(SID)
        self.assertEqual((got["status"], got["pause_reasons"]), ("RUNNING", []))

    def test_a_journal_with_no_control_leaves_the_store_alone(self):
        sd = self.live(upto=CHUNK * 2)
        lifecycle.pause(SID, "owner", wall_ns=5)
        sd.close()
        self.recover()
        self.assertEqual(store.get_session(SID)["pause_reasons"], ["owner"])


class CarriedPositionTest(RecoveryBase):
    FUT_FULL = True

    def two_days(self, sid):
        """Day one: a BUY of the future, finalised. Day two: a SELL of the same 65, which closes the carried position
        with real fills. Returns (cfg, day2 records); day two is not yet fed."""
        clock = packs.RANK[packs.CLOCK]
        day1 = [r for r in self.records
                if packs.decode(r[2], 0)[0]["instrument_id"] in (self.idx, self.fut) or r[1] == clock]
        act = [{"on_event": 60, "place": {"instrument": self.fut, "side": "BUY", "qty": 65, "product": "NRML",
                                          "type": "MARKET"}},
               {"on_event": len(day1) + 60, "place": {"instrument": self.fut, "side": "SELL", "qty": 65,
                                                       "product": "NRML", "type": "MARKET"}}]
        cfg = self.session_cfg(strategy={"kind": "script", "actions": act}, capital_paise=100_000_000,
                               universe=["NSE:NIFTY 50", "NFO:NIFTY26NOVFUT"])
        self.make_session(sid, cfg)
        one = self.day(sid, cfg=cfg)
        one.open(None, [], self.t("09:14:00"))
        one.feed(day1, None)
        one.close()
        self.assertEqual(finalise.finalise_day(sid, pf.DAY, files_complete=True, cutoff_passed=False).status,
                         "finalised")
        lifecycle.start_continuous(sid, wall_ns=9, engine_ns=9)
        return cfg, [(t + SHIFT, rank, _shift(rec, SHIFT)) for t, rank, rec in day1]

    def test_day2_recovery_uses_day1_state(self):
        with refdata.transaction() as conn:
            conn.execute("INSERT INTO fo_eod (trade_date, instrument_id, settle_paise) VALUES (?, ?, 2411000)",
                         (DAY2.isoformat(), self.fut))
        cfg, day2 = self.two_days(SID)
        ref_cfg, _ = self.two_days("clean")
        d2 = DAY2.isoformat()
        for sid, c in ((SID, cfg), ("clean", ref_cfg)):             # the same day two, once uninterrupted, once crashed
            two = self.day(sid, day=DAY2, cfg=c)
            two.open(finalise.next_sod_state(sid, DAY2), [], self.t("09:14:00") + SHIFT)
            for i in range(0, len(day2), CHUNK):
                two.feed(day2[i:i + CHUNK], None)
            if sid == SID:
                live_rows, live_book = self.rows(sid, d2), two.engine.book()
            two.close()
        self.assertEqual([o["order_id"] for o in live_rows["orders"]], [2], "day one's order counter carried over")
        self.assertEqual([f["side"] for f in live_rows["fills"]], ["SELL"], "a real fill")
        self.assertEqual(live_book, [], "the carried 65 is closed")
        bundle = json.loads(paths.day_bundle_file(SID, DAY2).read_text(encoding="utf-8"))
        self.assertTrue(bundle["sod_state"]["ledger"], "day one's position is in day two's bundle")
        sd, res = self.recover(day=DAY2, at="09:40:00")
        self.assertEqual((res.records, res.mismatch, res.written, res.tail_lost_rows), (len(day2), None, 0, 0))
        self.assertEqual(self.rows(SID, d2), live_rows)
        self.assertEqual(sd.engine.book(), live_book)
        for sid, day_ in ((SID, sd), ("clean", None)):             # end the day on each: the same rows and state
            self.assertEqual(finalise.finalise_day(sid, DAY2, files_complete=True, cutoff_passed=False,
                                                   live=day_).status, "finalised")
        self.assertEqual(self.rows(SID, d2), self.rows("clean", d2))
        self.assertEqual(store.session_day_end(SID, d2), store.session_day_end("clean", d2))
        self.assertEqual(store.get_session_day(SID, d2)["eod_state_json"],
                         store.get_session_day("clean", d2)["eod_state_json"])


def _shift(rec: bytes, shift: int) -> bytes:
    t = struct.unpack_from("<q", rec, 8)[0]
    return rec[:8] + struct.pack("<q", t + shift) + rec[16:]


class RestartGapTest(RecoveryBase):
    """GK 5.4: a live restart inside market hours journals the GAP(process_stop) end at the first new frame."""

    def dispatcher(self, sd, *, replay=False):
        tokens = {pf.pf.NIFTY_INDEX: self.idx}
        return Dispatcher([sd], FrameEncoder(tokens), clock=ManualClock(self.t("10:00:00")),
                          cfg=loader.load_json("paper.json"), replay=replay)

    def gaps(self, sid=SID):
        return [(r["t_event"], r["phase"]) for r in journal_records(sid)
                if r["kind"] == "GAP" and r["reason"] == "process_stop"]

    def frame(self, disp, t_ns, no=1):
        disp.put_frame((no, t_ns, 0, throughput_frame(), True))
        disp.drain()

    def test_the_end_of_a_process_stop_gap_is_journaled_at_the_first_new_frame(self):
        sd = self.live(upto=CHUNK * 2)
        disp = self.dispatcher(sd)
        disp.journal_process_stop(self.t("09:30:00"))
        sd.close()
        n = len(journal_records(SID))
        sd2, res = self.recover()
        disp2 = self.dispatcher(sd2)
        disp2.start(self.t("10:00:00"))
        self.assertEqual(len(journal_records(SID)), n)             # nothing is journaled before a frame arrives
        t_frame = self.t("10:00:05")
        self.frame(disp2, t_frame)
        recs = journal_records(SID)
        ends = [i for i, r in enumerate(recs) if r["kind"] == "GAP" and r["phase"] == 1]
        self.assertEqual(len(ends), 1)
        self.assertIn(recs[ends[0]]["t_event"] - t_frame, (0, 1))   # at the frame's stamp (+1 ns after its CLOCK record)
        self.assertEqual([p for _, p in self.gaps()], [0, 1])
        self.assertTrue(all(r["kind"] != "INDEX" for r in recs[n:ends[0]]))     # before the frame's own records
        self.frame(disp2, t_frame + SEC, no=2)
        self.assertEqual([p for _, p in self.gaps()], [0, 1])       # once
        self.assertEqual([r["detail"]["cause"] for r in store.timeline(SID) if r["kind"] == "gap"][-1:],
                         ["process_stop_resumed"])

    def test_a_crashed_run_inside_market_hours_gets_its_gap_start_at_the_last_journaled_time(self):
        sd = self.live(upto=CHUNK * 2)
        last = journal_records(SID)[-1]["t_event"]
        sd.close()                                                  # no process stop was journaled: a crash
        sd2, res = self.recover()
        disp = self.dispatcher(sd2)
        disp.start(self.t("10:00:00"))
        starts = [t for t, p in self.gaps() if p == 0]
        self.assertEqual(len(starts), 1)
        self.assertIn(starts[0] - last, (0, 1))                     # at the last record's t (+1 ns when its rank sorts lower)
        row = [r["detail"] for r in store.timeline(SID) if r["kind"] == "gap"][-1]
        self.assertEqual(row["t_ns"], starts[0])                    # the timeline names the t that was journaled
        self.assertEqual(disp.stats.gaps, 1)
        self.frame(disp, self.t("10:00:05"))
        self.assertEqual([p for _, p in self.gaps()], [0, 1])
        self.assertEqual(disp.stats.gaps, 2)

    def test_a_restart_outside_market_hours_journals_no_gap_start(self):
        sd = self.live(upto=CHUNK * 2)
        sd.close()
        sd2, res = self.recover()
        disp = self.dispatcher(sd2)
        disp.start(self.t("16:30:00"))
        self.assertEqual(self.gaps(), [])

    def test_a_replay_journals_no_process_stop_gaps_on_restart(self):
        sd = self.live(upto=CHUNK * 2)
        sd.close()
        sd2, res = self.recover()
        disp = self.dispatcher(sd2, replay=True)
        disp.start(self.t("10:00:00"))
        self.frame(disp, self.t("10:00:05"))
        self.assertEqual(self.gaps(), [])

    def test_a_session_opened_fresh_gets_no_gap(self):
        self.make_session()
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        disp = self.dispatcher(sd)
        disp.start(self.t("10:00:00"))
        self.frame(disp, self.t("10:00:05"))
        self.assertEqual(self.gaps(), [])

    # ── LCB 5.2 (Task 18 review): a live restart whose journal ends inside an open token_expired gap ────────────
    def outage(self, reason="token_expired"):
        return [(r["phase"], r["t_event"]) for r in journal_records(SID) if r["kind"] == "GAP" and r["reason"] == reason]

    def controls(self):
        return [(r["payload"]["enabled"], r["payload"]["reasons"]) for r in journal_records(SID)
                if r["kind"] == "REFDATA" and r["subtype"] == "strategy_control"]

    def expired_journal(self):
        """A live day that ended inside an open scope-all token_expired gap, its session paused for it."""
        sd = self.live(upto=CHUNK * 2)
        disp = self.dispatcher(sd)
        disp.put_gap("start", "token_expired", self.t("09:30:00"), None)
        disp.drain()
        disp.pause_for_token_expiry(self.t("09:30:01"))
        sd.close()

    def test_a_live_restart_inside_an_open_token_expired_gap_nests_no_process_stop(self):
        self.expired_journal()
        sd2, _ = self.recover()
        self.assertEqual(sd2.open_outage, "token_expired")
        disp = self.dispatcher(sd2)
        disp.start(self.t("10:00:00"))                               # inside market hours, live
        self.assertEqual(self.gaps(), [])                            # no process_stop start inside the outage
        self.frame(disp, self.t("10:00:05"))
        self.assertEqual(self.gaps(), [])
        self.assertEqual([p for p, _ in self.outage()], [0, 1])      # the token_expired gap ends at the first frame
        self.assertEqual([r["detail"]["cause"] for r in store.timeline(SID) if r["kind"] == "gap"][-1:],
                         ["token_expired_resumed"])

    def test_the_resume_control_lands_before_the_first_frames_own_records(self):
        self.expired_journal()
        sd2, _ = self.recover()
        disp = self.dispatcher(sd2)
        disp.start(self.t("10:00:00"))
        n = len(journal_records(SID))
        self.frame(disp, self.t("10:00:05"))
        kinds = [(r["kind"], r.get("phase"), r.get("subtype")) for r in journal_records(SID)[n:]]
        end = kinds.index(("GAP", 1, None))
        ctl = kinds.index(("REFDATA", None, "strategy_control"))
        idx = [i for i, k in enumerate(kinds) if k[0] == "INDEX"]
        self.assertLess(end, ctl)
        self.assertTrue(idx and ctl < idx[0], kinds)
        self.assertEqual(self.controls()[-2:], [(False, ["token_expired"]), (True, [])])
        self.assertEqual(store.get_session(SID)["pause_reasons"], [])

    def test_a_live_restart_after_an_open_disconnect_gap_is_continued_the_same_way(self):
        sd = self.live(upto=CHUNK * 2)
        disp = self.dispatcher(sd)
        disp.put_gap("start", "disconnect", self.t("09:30:00"), None)
        disp.drain()
        sd.close()
        sd2, _ = self.recover()
        disp2 = self.dispatcher(sd2)
        disp2.start(self.t("10:00:00"))
        self.frame(disp2, self.t("10:00:05"))
        self.assertEqual(self.gaps(), [])
        self.assertEqual([p for p, _ in self.outage("disconnect")], [0, 1])

    # ── the CLOCK restore (dispatcher `_last_clock`) applies to a live restart too ────────────────────────────
    def test_a_live_restart_inside_the_same_clock_boundary_repeats_no_clock_record(self):
        """`Dispatcher._last_clock` is restored from the journal on a live restart as well as a replay's: the CLOCK
        record of a boundary the journal holds is not written again (it is a periodic marker, never data)."""
        sd = self.live(upto=CHUNK * 2)
        first = self.dispatcher(sd)
        boundary = self.t("09:45:00")
        every = int(loader.load_json("paper.json")["clock_every_s"]) * SEC
        self.frame(first, boundary + every // 4)
        sd.close()
        clocks = [r["t_event"] for r in journal_records(SID) if r["kind"] == "CLOCK"]
        self.assertEqual(clocks[-1], boundary)                       # the restart's boundary is the journal's last CLOCK
        sd2, _ = self.recover()
        self.assertEqual(sd2.last_clock, boundary)
        disp = self.dispatcher(sd2)
        disp.start(boundary + every // 2)
        self.frame(disp, boundary + every // 2)                      # a frame inside the same boundary
        self.assertEqual([r["t_event"] for r in journal_records(SID) if r["kind"] == "CLOCK"], clocks)
        self.frame(disp, boundary + every + every // 4, no=2)        # the next boundary adds exactly one
        self.assertEqual([r["t_event"] for r in journal_records(SID) if r["kind"] == "CLOCK"],
                         clocks + [boundary + every])


class MarketDayRecoveryTest(pf.PaperDayTest):
    """The same restart through the real daemon code (`replay:<day>:max`)."""

    def setUp(self):
        super().setUp()
        self.record(seconds=120)
        real = loader.load_json
        patcher = mock.patch.object(
            loader, "load_json", lambda name, *a, **kw: pf.UNIVERSE if name == "universe.json" else real(name, *a, **kw))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = self.session_cfg(strategy={"kind": "script", "actions": []})
        store.create_session("s1", "s1", self.cfg, 1)
        lifecycle.arm("s1", wall_ns=2)

    def run_day(self, **kw):
        kw.setdefault("ticker_spec", f"replay:{pf.EXPIRY}:max")
        kw.setdefault("session_ids", ["s1"])
        return market_day.run_market_day(**kw)

    def stop_after(self, events: int, stop: threading.Event):
        real_wait, seen = ReplayTicker._wait, []

        def wait(ticker, prev, t_ns):
            real_wait(ticker, prev, t_ns)
            seen.append(t_ns)
            if len(seen) == events:
                stop.set()
                time.sleep(0.4)

        patcher = mock.patch.object(ReplayTicker, "_wait", wait)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def test_a_restart_applies_a_pending_stop_all_before_the_first_new_input(self):
        stop = threading.Event()
        patcher = self.stop_after(40, stop)
        self.assertEqual(self.run_day(stop=stop), exitcodes.OK)
        patcher.stop()
        before = len(journal_records("s1"))
        paths.ticks_replay_file(pf.DAY).unlink()
        store.add_command("s1", "stop_all", None, 5)
        self.assertEqual(self.run_day(), exitcodes.OK)
        recs = journal_records("s1")
        self.assertGreater(len(recs), before + 1)
        first = recs[before]
        self.assertEqual((first["kind"], first["subtype"], first["payload"]),
                         ("REFDATA", "strategy_control", {"enabled": False, "reasons": ["stop_all"]}))
        self.assertEqual(store.pending_commands(), [])
        self.assertEqual(store.get_session("s1")["pause_reasons"], ["stop_all"])

    def test_a_restart_replays_the_journal_and_the_timeline_says_so(self):
        stop = threading.Event()
        patcher = self.stop_after(40, stop)
        self.run_day(stop=stop)
        patcher.stop()
        paths.ticks_replay_file(pf.DAY).unlink()
        self.run_day()
        self.assertIn("recovered", [r["kind"] for r in store.timeline("s1")])


# ── the owner's salvage tool ─────────────────────────────────────────────────────────────────────────────────────


class SalvageTest(RecoveryBase):
    def damaged(self, sid=SID, at=20):
        """A day whose record `at` has a flipped byte, with good records after it."""
        self.live(sid).close()
        jp = paths.journal_file(sid, pf.DAY)
        data = bytearray(jp.read_bytes())
        data[record_ends(sid)[at] + 30] ^= 0xFF
        jp.write_bytes(bytes(data))
        return jp

    def digest(self, p) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def test_salvage_writes_a_copy_beside_the_journal_and_a_report_and_never_edits_the_original(self):
        jp = self.damaged(at=20)
        crc = journal._crc_path(jp)
        before = (self.digest(jp), self.digest(crc))
        rep = recovery.salvage(jp)
        self.assertEqual((self.digest(jp), self.digest(crc)), before)           # the original is untouched
        self.assertEqual(rep.kept, 21)
        self.assertEqual(rep.dropped, len(self.records) - 21)
        self.assertEqual(rep.first_bad_offset, record_ends(SID)[20])
        self.assertTrue(rep.copy.exists() and rep.report.exists())
        self.assertEqual(rep.copy.parent, jp.parent)
        copy_records = journal.read(rep.copy)[1]
        self.assertEqual(copy_records, jp.read_bytes()[journal.HEADER_SIZE:record_ends(SID)[20]])   # byte for byte
        text = json.loads(rep.report.read_text(encoding="utf-8"))
        self.assertEqual((text["kept_records"], text["dropped_records"]), (21, len(self.records) - 21))
        self.assertEqual(text["original_sha256"], before[0])
        self.assertEqual(text["salvaged_sha256"], self.digest(rep.copy))
        self.assertIn("checksum", text["first_bad"]["why"])
        self.assertEqual(text["first_bad"]["seq"], 22)
        self.assertTrue(text["dropped_after"])
        self.assertIn("never deleted", text["next_step"])

    def test_the_salvaged_copy_is_a_journal_the_writer_can_open_and_the_original_still_is_not(self):
        jp = self.damaged()
        rep = recovery.salvage(jp)
        w = journal.JournalWriter(rep.copy, fsync_every_mono_ns=1, mono=time.monotonic_ns)
        self.assertEqual(w.records, 21)
        w.close(finalise=False)
        with self.assertRaises(journal.JournalError):
            journal.read(jp)

    def test_nothing_takes_effect_until_the_owner_adopts_it(self):
        jp = self.damaged()
        recovery.salvage(jp)
        with self.assertRaises(journal.JournalError):                          # recovery still refuses the day
            recovery.recover(SID, pf.DAY, clock=ManualClock(self.t("09:40:00")))
        self.assertTrue(any("salvage" in n.name for n in jp.parent.iterdir()))

    def test_adopt_keeps_the_damaged_journal_and_recovery_then_deletes_nothing_it_cannot_regenerate(self):
        jp = self.damaged()
        orig = jp.read_bytes()
        orig_crc = journal._crc_path(jp).read_bytes()
        rep = recovery.salvage(jp)
        recovery.adopt_salvage(jp)
        kept = list(jp.parent.glob(jp.name + ".damaged*"))
        self.assertIn(orig, [p.read_bytes() for p in kept])                    # the damaged journal is still on disk
        self.assertIn(orig_crc, [p.read_bytes() for p in kept])
        self.assertFalse(rep.copy.exists())                                    # the copy is now the journal
        self.assertEqual(len(journal_records(SID)), 21)
        sd, res = self.recover()
        self.assertIsNone(res.mismatch)
        self.assertEqual(res.records, 21)
        self.assertGreater(res.tail_lost_rows, 0)                              # rows of the dropped records go
        self.assertEqual(self.rows(), self.ref(21))
        self.assertEqual(len(self.timeline(SID, "journal_tail_lost")), 1)

    def test_adopt_needs_a_salvage_to_adopt_and_a_stopped_daemon(self):
        jp = self.damaged()
        with self.assertRaises(recovery.SalvageError):
            recovery.adopt_salvage(jp)                                          # nothing was salvaged
        recovery.salvage(jp)
        from stockify.daemon import singleton
        lock = singleton.acquire()
        self.addCleanup(lock.release)
        with self.assertRaises(singleton.AlreadyRunning):
            recovery.adopt_salvage(jp)                                          # a running daemon holds the journal
        lock.release()
        recovery.adopt_salvage(jp)

    def test_adopt_that_dies_between_its_steps_is_finished_by_running_it_again(self):
        jp = self.damaged()
        recovery.salvage(jp)
        real, crc = recovery.os.replace, journal._crc_path(jp)

        def replace(src, dst):
            if Path(dst) == crc:                    # the journal is replaced, its checksum file not yet
                raise OSError("power cut")
            return real(src, dst)

        with mock.patch.object(recovery.os, "replace", replace):
            with self.assertRaises(OSError):
                recovery.adopt_salvage(jp)
        recovery.adopt_salvage(jp)
        self.assertEqual(len(journal_records(SID)), 21)
        self.assertTrue(list(jp.parent.glob(jp.name + ".damaged*")))

    def test_a_healthy_journal_has_nothing_to_salvage(self):
        self.live().close()
        with self.assertRaises(recovery.SalvageError):
            recovery.salvage(paths.journal_file(SID, pf.DAY))

    def test_a_damaged_finalised_journal_is_salvaged_as_an_open_one(self):
        self.live().close(finalise=True)
        jp = paths.journal_file(SID, pf.DAY)
        data = bytearray(jp.read_bytes())
        data[record_ends(SID)[30] + 30] ^= 0xFF
        jp.write_bytes(bytes(data))
        rep = recovery.salvage(jp)
        self.assertEqual(rep.kept, 31)
        self.assertFalse(journal.is_finalised(rep.copy))                         # the day is open again, to be finalised

    def test_nothing_in_the_daemon_calls_the_salvage_tool(self):
        import inspect
        import pkgutil
        import stockify.daemon
        from stockify import __main__ as cli
        mods = [__import__(f"stockify.daemon.{m.name}", fromlist=["x"])
                for m in pkgutil.iter_modules(stockify.daemon.__path__) if m.name != "recovery"]
        for mod in (stockify.daemon, cli, *mods):
            self.assertNotIn("salvage", inspect.getsource(mod), mod.__name__)
        self.assertNotIn("salvage(", inspect.getsource(recovery.recover))
        self.assertNotIn("adopt_salvage(", inspect.getsource(recovery.recover))

    def test_adopt_refuses_a_changed_checksum_file_before_it_changes_anything(self):
        jp = self.damaged()
        recovery.salvage(jp)
        crc = journal._crc_path(jp)
        crc.write_bytes(crc.read_bytes() + b"\x00\x00\x00\x00")
        before = jp.read_bytes()
        with self.assertRaises(recovery.SalvageError):
            recovery.adopt_salvage(jp)
        self.assertEqual(jp.read_bytes(), before)
        self.assertFalse(list(jp.parent.glob(jp.name + ".damaged*")))
        with self.assertRaises(recovery.SalvageError):             # and _preserve itself checks what it copies
            recovery._preserve(jp, "0" * 64, crc=True)


if __name__ == "__main__":
    unittest.main()
