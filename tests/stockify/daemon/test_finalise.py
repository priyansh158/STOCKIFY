"""DM 5.5 (M5.3): each session-day is finalised after NSE's settlement prices; a crash anywhere in it is repaired by
running it again."""
from __future__ import annotations

import csv
import itertools
import json
import sqlite3
import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf
from tests.stockify.daemon.test_engine_log import enable_logging
from tests.stockify.daemon.test_session_day import pack_records

from stockify import bundle as bundle_mod, engine, paths, store
from stockify.daemon import finalise, journal, lifecycle, session_day
from stockify.data import packs, refdata
from stockify.oracle import sod_state

SID = "s1"
DAY2 = date(2026, 11, 18)
SEC = pf.SEC
SHIFT = 86_400 * SEC


def journal_records(day: date, sid: str = SID) -> list[dict]:
    _, body, _ = journal.read(paths.journal_file(sid, day))
    out, off = [], 0
    while off < len(body):
        rec, off = packs.decode(body, off)
        out.append(rec)
    return out


def shifted(rec: bytes, shift: int) -> bytes:
    t = struct.unpack_from("<q", rec, 8)[0]
    return rec[:8] + struct.pack("<q", t + shift) + rec[16:]


def stamped(records, settles) -> bytes:
    """Records as a journal would stamp them: `t` made monotone by `journal.ordered_t`, seq numbered from 1."""
    out, last_t, last_rank, seq = bytearray(), None, None, 1
    for t, rank, rec in [*records, *settles]:
        t = journal.ordered_t(t, rank, last_t, last_rank)
        out += rec[:8] + struct.pack("<qQ", t, seq) + rec[24:]
        last_t, last_rank, seq = t, rank, seq + 1
    return bytes(out)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class FinaliseTest(pf.PaperDayTest):
    def setUp(self):
        super().setUp()
        self.record(seconds=120)
        sha = packs.build({"dataset": "ticks", "from": pf.EXPIRY, "to": pf.EXPIRY, "clock_cadence_ms": 5000})
        # the live feed never carries SETTLE records (a pack built after the day does): finalising adds them
        self.records = [r for r in pack_records(sha) if r[1] != packs.RANK[packs.SETTLE_K]]
        self.ticks = itertools.count(0, 10)

    def buy_cfg(self, **over):
        act = [{"on_event": 60, "place": {"instrument": self.ces[4], "side": "BUY", "qty": 65, "product": "NRML",
                                          "type": "MARKET"}}]
        return self.session_cfg(strategy={"kind": "script", "actions": act}, **over)

    def make_session(self, cfg=None, sid=SID):
        cfg = cfg or self.buy_cfg()
        store.create_session(sid, "s", cfg, 1)
        lifecycle.arm(sid, wall_ns=2)
        lifecycle.start_continuous(sid, wall_ns=3, engine_ns=3)
        return cfg

    def day(self, cfg, day=pf.DAY, sid=SID):
        sd = session_day.SessionDay(sid, day, cfg, mono=lambda: next(self.ticks))
        self.addCleanup(sd.close)
        return sd

    def traded_day(self, cfg=None, sid=SID):
        """Day one, fed and left unfinalised; returns the still open SessionDay."""
        cfg = self.make_session(cfg, sid)
        sd = self.day(cfg, sid=sid)
        sd.open(None, [], self.t("09:14:00"))
        sd.feed(self.records, None)
        return sd

    def fin(self, day=pf.DAY, sid=SID, **kw):
        kw.setdefault("files_complete", True)
        kw.setdefault("cutoff_passed", False)
        return finalise.finalise_day(sid, day, **kw)

    def settles(self, day=pf.DAY, sid=SID):
        return [r for r in journal_records(day, sid) if r["kind"] == "SETTLE"]

    def expected_settles(self, day=pf.DAY, sid=SID) -> int:
        b = json.loads(paths.day_bundle_file(sid, day).read_text(encoding="utf-8"))
        return len(packs.settle_records({int(i["id"]) for i in b["instruments"]}, day))

    # ── the brief's tests ─────────────────────────────────────────────────────────────────────────────────────────

    def test_finalise_settles_expiry_and_stores_eod(self):
        self.traded_day().close()                                             # a fresh process finalises
        res = self.fin()
        self.assertEqual((res.status, res.error, res.day, res.session_id), ("finalised", None, pf.DAY, SID))
        jpath = paths.journal_file(SID, pf.DAY)
        self.assertTrue(journal.is_finalised(jpath))
        want = self.expected_settles()
        self.assertGreater(want, 0)
        self.assertEqual((len(self.settles()), res.settle_count), (want, want))
        row = store.get_session_day(SID, pf.EXPIRY)
        self.assertEqual(row["status"], "finalised")
        self.assertIsInstance(row["finalised_ns"], int)
        self.assertEqual(json.loads(row["eod_state_json"])["trade_date"], pf.EXPIRY)
        self.assertGreater(store.session_day_end(SID, pf.EXPIRY)["equity"]["nav_paise"], 0)
        self.assertEqual(store.get_session(SID)["status"], "ARMED")
        with tempfile.TemporaryDirectory() as tmp:                            # the orders are the engine's book
            e = finalise.fresh_engine(SID, pf.DAY)
            finalise.replay_into(e, jpath)
            e.finish()
            e.write_artifacts(tmp)
            with open(Path(tmp) / "orders.csv", newline="", encoding="utf-8") as f:
                book = list(csv.DictReader(f))
            with open(Path(tmp) / "positions_eod.csv", newline="", encoding="utf-8") as f:
                eod = list(csv.DictReader(f))
            del e
        self.assertEqual(store.session_rows(SID, "orders"), [dict(r) for r in book])
        self.assertEqual(store.session_day_end(SID, pf.EXPIRY)["positions"], [dict(r) for r in eod])
        self.assertEqual([(r["status"], r["filled_qty"]) for r in book], [("COMPLETE", "65")])

    def test_a_live_engine_finalises_without_a_replay(self):
        sd = self.traded_day()
        self.assertEqual(self.fin(live=sd).status, "finalised")
        self.assertTrue(journal.is_finalised(paths.journal_file(SID, pf.DAY)))
        e = finalise.fresh_engine(SID, pf.DAY)
        finalise.replay_into(e, paths.journal_file(SID, pf.DAY))
        e.finish()
        self.assertEqual(store.get_session_day(SID, pf.EXPIRY)["eod_state_json"], e.eod_state())
        self.assertEqual(len(self.settles()), self.expected_settles())

    def test_waits_for_files_until_cutoff(self):
        self.traded_day().close()
        res = self.fin(files_complete=False, cutoff_passed=False)
        self.assertEqual(res.status, "waiting")
        self.assertFalse(journal.is_finalised(paths.journal_file(SID, pf.DAY)))
        self.assertEqual(self.settles(), [])
        self.assertEqual(store.get_session_day(SID, pf.EXPIRY)["status"], "open")
        self.assertEqual(store.get_session(SID)["status"], "RUNNING")
        self.assertEqual(self.fin().status, "finalised")                      # the files came

    def test_finalise_after_cutoff_uses_fallback_label(self):
        self.traded_day().close()
        with refdata.transaction() as conn:                                   # the evening files never came
            conn.execute("DELETE FROM settlement_prices")
            conn.execute("DELETE FROM fo_eod")
        res = self.fin(files_complete=False, cutoff_passed=True)
        self.assertEqual(res.status, "finalised")
        self.assertIn("settlement_fallback", res.labels)
        self.assertIn("settlement_fallback", json.loads(store.get_session_day(SID, pf.EXPIRY)["labels_json"]))
        self.assertEqual(self.settles(), [])

    def test_complete_files_carry_no_fallback_label(self):
        self.traded_day().close()
        self.assertNotIn("settlement_fallback", self.fin().labels)

    def test_settle_t_is_never_before_last_journal_t(self):
        sd = self.traded_day()
        late = self.t("20:00:00")
        rank = packs.RANK[packs.GAP_K]
        sd.feed([(late, rank, packs.enc_gap(0, late, 0, "disconnect")),
                 (late, rank, packs.enc_gap(0, late, 1, "disconnect"))], None)
        sd.close()
        self.fin()
        recs = journal_records(pf.DAY)
        settles = [r for r in recs if r["kind"] == "SETTLE"]
        self.assertTrue(settles)
        self.assertTrue(all(r["t_event"] >= late for r in settles))
        ts = [r["t_event"] for r in recs]
        self.assertEqual(ts, sorted(ts))

    def test_next_day_refuses_unfinalised_previous_day(self):
        self.assertIsNone(finalise.next_sod_state(SID, pf.DAY))               # no earlier day at all
        self.traded_day().close()
        with self.assertRaises(finalise.FinaliseError):
            finalise.next_sod_state(SID, DAY2)
        self.fin()
        self.assertEqual(finalise.next_sod_state(SID, DAY2)["trade_date"], pf.EXPIRY)
        self.assertIsNone(finalise.next_sod_state(SID, pf.DAY))               # the first day still has none

    def test_finalise_in_a_fresh_process_writes_no_process_stop_gap(self):
        self.traded_day().close(finalise=False)
        self.fin()
        gaps = [r for r in journal_records(pf.DAY) if r["kind"] == "GAP"]
        self.assertEqual([g for g in gaps if g["reason"] == "process_stop"], [])

    # ── finalising is idempotent: a crash anywhere, then a re-run ─────────────────────────────────────────────────

    def test_crash_before_the_journal_header_is_repaired_by_a_rerun(self):
        """The settle records are in the journal and the store has the day, but the header does not say finalised."""
        real, calls = journal.JournalWriter.close, []

        def crash(w, *, finalise):
            if finalise and not calls:
                calls.append(1)
                real(w, finalise=False)
                raise OSError("power cut before the header")
            return real(w, finalise=finalise)

        self.traded_day().close()
        with mock.patch.object(journal.JournalWriter, "close", crash):
            with self.assertRaises(OSError):
                self.fin()
        self.assertFalse(journal.is_finalised(paths.journal_file(SID, pf.DAY)))
        self.assertEqual(len(self.settles()), self.expected_settles())
        self.assertNotEqual(store.get_session_day(SID, pf.EXPIRY)["status"], "finalised")
        with self.assertRaises(finalise.FinaliseError):                       # the state is not handed on yet
            finalise.next_sod_state(SID, DAY2)
        self.assertEqual(self.fin().status, "finalised")
        self.assertTrue(journal.is_finalised(paths.journal_file(SID, pf.DAY)))
        self.assertEqual(len(self.settles()), self.expected_settles())        # never appended twice
        self.assertEqual(store.get_session(SID)["status"], "ARMED")

    def test_crash_before_arming_is_repaired_by_a_rerun(self):
        self.traded_day().close()
        with mock.patch.object(lifecycle, "arm", side_effect=OSError("crash before arming")):
            with self.assertRaises(OSError):
                self.fin()
        self.assertTrue(journal.is_finalised(paths.journal_file(SID, pf.DAY)))
        self.assertNotEqual(store.get_session_day(SID, pf.EXPIRY)["status"], "finalised")
        self.assertEqual(store.get_session(SID)["status"], "RUNNING")
        self.assertEqual(self.fin().status, "finalised")
        self.assertEqual(store.get_session(SID)["status"], "ARMED")
        self.assertEqual(len(self.settles()), self.expected_settles())

    def test_a_crash_while_storing_gives_the_same_day_as_a_clean_run(self):
        self.traded_day(sid="clean").close()
        self.fin(sid="clean")
        self.traded_day().close()
        with mock.patch.object(store, "save_session_day_end", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                self.fin()
        self.assertEqual(self.fin().status, "finalised")

        def view(sid):
            row = store.get_session_day(sid, pf.EXPIRY)
            return {"state": row["eod_state_json"], "labels": row["labels_json"].replace(sid, "<sid>"), "status": row["status"],
                    "end": store.session_day_end(sid, pf.EXPIRY),
                    "tables": [store.session_rows(sid, t) for t in ("orders", "order_events", "fills", "charges")],
                    "journal": paths.journal_file(sid, pf.DAY).read_bytes()}

        self.assertEqual(view(SID), view("clean"))

    def test_a_rerun_of_a_finalised_day_changes_nothing_and_never_arms_again(self):
        self.traded_day().close()
        self.fin()
        before = (paths.journal_file(SID, pf.DAY).read_bytes(), store.get_session_day(SID, pf.EXPIRY))
        lifecycle.start_continuous(SID, wall_ns=9, engine_ns=9)                # the next day began
        again = self.fin()
        self.assertEqual(again.status, "finalised")
        self.assertEqual(again.labels, json.loads(before[1]["labels_json"]))
        self.assertEqual((paths.journal_file(SID, pf.DAY).read_bytes(), store.get_session_day(SID, pf.EXPIRY)), before)
        self.assertEqual(store.get_session(SID)["status"], "RUNNING")

    def test_finalising_only_ever_appends_to_the_journal(self):
        self.traded_day().close()
        jpath = paths.journal_file(SID, pf.DAY)
        before = jpath.read_bytes()
        self.fin()
        after = jpath.read_bytes()
        self.assertEqual(after[64:len(before)], before[64:])                  # the header alone is rewritten

    # ── stop, reasons ────────────────────────────────────────────────────────────────────────────────────────────

    def test_a_stopped_session_stays_stopped(self):
        self.traded_day().close()
        lifecycle.stop(SID, wall_ns=50)
        self.assertEqual(self.fin().status, "finalised")
        self.assertEqual(store.get_session(SID)["status"], "STOPPED")

    def test_reasons_are_kept_when_the_day_ends(self):
        self.traded_day().close()
        lifecycle.pause(SID, "owner", wall_ns=50)
        self.fin()
        s = store.get_session(SID)
        self.assertEqual((s["status"], s["pause_reasons"]), ("ARMED", ["owner"]))

    # ── two days ──────────────────────────────────────────────────────────────────────────────────────────────────

    def test_two_day_session_equals_a_two_day_per_day_run(self):
        """The session carries day one's state to day two through `next_sod_state`; a per-day run that feeds the
        same records, and the same settle records, to a fresh engine per day, does too."""
        act = [{"on_event": 60, "place": {"instrument": self.fut, "side": "BUY", "qty": 65, "product": "NRML",
                                          "type": "MARKET"}}]
        cfg = self.session_cfg(strategy={"kind": "script", "actions": act},
                               universe=["NSE:NIFTY 50", "NFO:NIFTY26NOVFUT"])
        self.make_session(cfg)
        clock = packs.RANK[packs.CLOCK]
        day1 = [r for r in self.records
                if packs.decode(r[2], 0)[0]["instrument_id"] in (self.idx, self.fut) or r[1] == clock]
        day2 = [(t + SHIFT, rank, shifted(rec, SHIFT)) for t, rank, rec in day1]
        with refdata.transaction() as conn:
            conn.execute("INSERT INTO fo_eod (trade_date, instrument_id, settle_paise) VALUES (?, ?, 2411000)",
                         (DAY2.isoformat(), self.fut))
        one = self.day(cfg)
        one.open(None, [], self.t("09:14:00"))
        one.feed(day1, None)
        one.close()
        self.assertEqual(self.fin().status, "finalised")
        lifecycle.start_continuous(SID, wall_ns=9, engine_ns=9)
        two = self.day(cfg, day=DAY2)
        two.open(finalise.next_sod_state(SID, DAY2), [], self.t("09:14:00") + SHIFT)
        two.feed(day2, None)
        two.close()
        self.assertEqual(self.fin(DAY2).status, "finalised")
        refs, state = [], None
        for day, recs in ((pf.DAY, day1), (DAY2, day2)):
            b = bundle_mod.resolve_session_day("ref", cfg, day, state)
            with tempfile.TemporaryDirectory() as tmp:
                bp = Path(tmp) / "bundle.json"
                bp.write_text(bundle_mod.dumps(b) + "\n", encoding="utf-8", newline="\n")
                e = session_day.session_engine(bp, cfg)
                e.push(stamped(recs, packs.settle_records({int(i["id"]) for i in b["instruments"]}, day)))
                e.finish()
                refs.append(e.eod_state())
                state = json.loads(refs[-1])
                del e
        self.assertEqual(store.get_session_day(SID, pf.EXPIRY)["eod_state_json"], refs[0])
        self.assertEqual(store.get_session_day(SID, DAY2.isoformat())["eod_state_json"], refs[1])
        self.assertNotEqual(refs[0], refs[1])
        self.assertTrue(state["ledger"], "a position was carried over")

    # ── fix round 1 ───────────────────────────────────────────────────────────────────────────────────────────────

    def test_a_day_that_is_being_finalised_is_busy_for_a_second_caller(self):
        """C1: one claim per (session, day), an OS file lock beside the journal."""
        self.traded_day().close()
        jpath = paths.journal_file(SID, pf.DAY)
        before = jpath.read_bytes()
        with mock.patch.object(finalise, "CLAIM_WAIT_S", 0.1):
            held = finalise.try_claim(finalise.claim_path(SID, pf.DAY), 0)
            self.assertIsNotNone(held)
            try:
                res = self.fin()
            finally:
                held.release()
            self.assertEqual(res.status, "busy")
            self.assertEqual(jpath.read_bytes(), before)                       # nothing touched
            self.assertEqual(store.get_session_day(SID, pf.EXPIRY)["status"], "open")
            self.assertEqual(self.fin().status, "finalised")                   # released: it goes ahead

    def test_two_threads_finalising_one_day_append_one_settle_set(self):
        self.traded_day().close()
        out, gate = [], threading.Barrier(2)

        def work():
            gate.wait()
            out.append(self.fin().status)

        threads = [threading.Thread(target=work) for _ in range(2)]
        with mock.patch.object(finalise, "CLAIM_WAIT_S", 60):
            for t in threads:
                t.start()
            for t in threads:
                t.join(120)
        self.assertEqual(sorted(out), ["finalised", "finalised"])             # the second waited, then saw it done
        self.assertEqual(len(self.settles()), self.expected_settles())
        self.assertTrue(journal.is_finalised(paths.journal_file(SID, pf.DAY)))
        self.assertEqual(store.get_session(SID)["status"], "ARMED")

    def test_another_process_holding_the_claim_makes_the_day_busy_until_it_dies(self):
        self.traded_day().close()
        code = ("import sys; from pathlib import Path; from stockify.daemon import finalise; "
                "c = finalise.try_claim(Path(sys.argv[1]), 0); print('held' if c else 'no', flush=True); sys.stdin.read()")
        root = Path(__file__).resolve().parents[3]
        env = {**os.environ, "PYTHONPATH": str(root)}
        proc = subprocess.Popen([sys.executable, "-c", code, str(finalise.claim_path(SID, pf.DAY))], cwd=str(root),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "held")
            with mock.patch.object(finalise, "CLAIM_WAIT_S", 0.2):
                self.assertEqual(self.fin().status, "busy")
            proc.kill()                                                       # a crashed holder never blocks
            proc.wait(30)
            with mock.patch.object(finalise, "CLAIM_WAIT_S", 5):
                self.assertEqual(self.fin().status, "finalised")
        finally:
            proc.kill()
            proc.stdout.close()
            proc.stdin.close()
        self.assertEqual(len(self.settles()), self.expected_settles())

    def tables(self, sid):
        row = store.get_session_day(sid, pf.EXPIRY)
        return {"orders": store.session_rows(sid, "orders"), "events": store.session_rows(sid, "order_events"),
                "fills": store.session_rows(sid, "fills"), "charges": store.session_rows(sid, "charges"),
                "end": store.session_day_end(sid, pf.EXPIRY), "state": row["eod_state_json"],
                "labels": row["labels_json"].replace(sid, "<sid>"), "status": row["status"],
                "bundle": row["bundle_sha256"] is not None}

    def test_live_and_replayed_finalisation_store_exactly_the_same_rows(self):
        """I1."""
        live = self.traded_day(sid="live")
        self.assertEqual(self.fin(sid="live", live=live).status, "finalised")
        self.traded_day(sid="replay").close()
        self.assertEqual(self.fin(sid="replay").status, "finalised")
        a, b = self.tables("live"), self.tables("replay")
        self.assertTrue(a["orders"] and a["events"] and a["fills"])
        for key in a:
            with self.subTest(table=key):
                self.assertEqual(a[key], b[key])

    def test_a_replayed_finalisation_reports_carried_orders(self):
        enable_logging(self)
        self.traded_day().close()
        with self.assertLogs("stockify.daemon.finalise", "WARNING") as logged:
            with mock.patch.object(store, "apply_session_outputs",
                                   side_effect=lambda *a, carried=None, **kw: carried.append(77) or 1):
                res = self.fin()
        self.assertEqual(res.status, "finalised", res.error)
        self.assertTrue(any("77" in m for m in logged.output))

    def test_a_finalised_journal_without_settles_ends_the_day_with_the_fallback(self):
        """I2: the journal was finalised before the evening files; nothing is appended to it."""
        sd = self.traded_day()
        sd.close(finalise=True)
        jpath = paths.journal_file(SID, pf.DAY)
        before = jpath.read_bytes()
        res = self.fin()
        self.assertEqual((res.status, res.error), ("finalised", None))
        self.assertIn("settlement_fallback", res.labels)
        self.assertEqual(jpath.read_bytes(), before)
        self.assertEqual(self.settles(), [])
        kinds = [r["kind"] for r in store.timeline(SID)]
        self.assertEqual(kinds.count("settlement_fallback"), 1)
        self.assertEqual(store.get_session_day(SID, pf.EXPIRY)["status"], "finalised")
        self.assertEqual(store.get_session(SID)["status"], "ARMED")
        self.assertEqual(finalise.next_sod_state(SID, DAY2)["trade_date"], pf.EXPIRY)

    def test_the_fallback_label_needs_an_expired_position_without_its_settle(self):
        """M2: the cut-off alone is not the label; a missing settle for a position that expired is."""
        self.traded_day(self.session_cfg(strategy={"kind": "script", "actions": []})).close()
        with refdata.transaction() as conn:
            conn.execute("DELETE FROM settlement_prices")
            conn.execute("DELETE FROM fo_eod")
        self.assertNotIn("settlement_fallback", self.fin(files_complete=False, cutoff_passed=True).labels)

    def test_a_large_state_stores_its_timeline_row_with_the_day_and_only_once(self):
        enable_logging(self)
        """M1."""
        self.traded_day().close()
        with mock.patch.object(sod_state, "warn_bytes", return_value=1):
            with mock.patch.object(store, "save_session_day_end", side_effect=sqlite3.OperationalError("locked")):
                with self.assertRaises(sqlite3.OperationalError):
                    self.fin()
            self.assertNotIn("sod_state_size", [r["kind"] for r in store.timeline(SID)])
            with self.assertLogs("stockify.daemon.finalise", "WARNING"):
                self.fin()
        self.assertEqual([r["kind"] for r in store.timeline(SID)].count("sod_state_size"), 1)

    def test_a_finalised_day_closes_the_live_day_it_is_given(self):
        """M4."""
        cfg = self.make_session()
        sd = self.day(cfg)
        sd.open(None, [], self.t("09:14:00"))
        sd.feed(self.records, None)
        self.assertEqual(self.fin(live=sd).status, "finalised")
        again = self.day(cfg)
        again.replay()                                                         # a finalised day: replay-only
        self.assertIsNotNone(again.engine)
        self.assertEqual(self.fin(live=again).status, "finalised")
        self.assertIsNone(again.engine)

    def test_replay_into_pushes_whole_records_in_batches(self):
        """M5."""
        self.traded_day().close()
        jpath = paths.journal_file(SID, pf.DAY)
        _, body, _ = journal.read(jpath)
        pushed = []

        class Spy:
            def push(self, data):
                pushed.append(bytes(data))

        n = finalise.replay_into(Spy(), jpath, batch_records=7)
        self.assertEqual(n, len(journal_records(pf.DAY)))
        self.assertEqual(b"".join(pushed), body)
        self.assertEqual(len(pushed), -(-n // 7))
        with jpath.open("ab") as f:                                            # a torn tail is never pushed
            f.write(b"\x01\x00\x20\x00partial")
        pushed.clear()
        self.assertEqual(finalise.replay_into(Spy(), jpath), n)
        self.assertEqual(b"".join(pushed), body)
        with self.assertRaises(ValueError):
            finalise.replay_into(Spy(), jpath, batch_records=0)

    def test_only_the_engines_run_error_is_a_failed_day(self):
        """M6."""
        self.traded_day().close()
        with mock.patch.object(finalise, "replay_into", side_effect=RuntimeError("a bug")):
            with self.assertRaises(RuntimeError):
                self.fin()
        with mock.patch.object(finalise, "replay_into", side_effect=engine.load().RunError("refused")):
            res = self.fin()
        self.assertEqual((res.status, res.error), ("failed", "refused"))

    # ── the previous day is the calendar's (I3) ─────────────────────────────────────────────────────────────────

    def put_day(self, day, status="finalised", state='{"trade_date": "x"}'):
        store.upsert_session_day(SID, day, status=status, eod_state_json=state)

    def test_next_state_skips_a_weekend(self):
        store.create_session(SID, "s", self.buy_cfg(), 1)
        self.put_day("2026-03-06", state='{"trade_date": "2026-03-06"}')      # a Friday
        self.assertEqual(finalise.next_sod_state(SID, date(2026, 3, 9))["trade_date"], "2026-03-06")

    def test_next_state_skips_a_holiday(self):
        store.create_session(SID, "s", self.buy_cfg(), 1)
        self.put_day("2026-03-02", state='{"trade_date": "2026-03-02"}')      # Holi was Tuesday the 3rd
        self.assertEqual(finalise.next_sod_state(SID, date(2026, 3, 4))["trade_date"], "2026-03-02")

    def test_a_missing_trading_day_fails_loudly_and_never_hands_on_an_older_state(self):
        store.create_session(SID, "s", self.buy_cfg(), 1)
        self.put_day("2026-03-02")
        with self.assertRaises(finalise.FinaliseError) as caught:
            finalise.next_sod_state(SID, date(2026, 3, 5))                    # Wednesday the 4th has no row
        self.assertIn("2026-03-04", str(caught.exception))

    def test_a_first_day_has_no_state_and_an_unfinalised_previous_day_fails(self):
        self.assertIsNone(finalise.next_sod_state(SID, date(2026, 3, 4)))
        self.put_day("2026-03-04", status="open")
        with self.assertRaises(finalise.FinaliseError):
            finalise.next_sod_state(SID, date(2026, 3, 5))


if __name__ == "__main__":
    unittest.main()
