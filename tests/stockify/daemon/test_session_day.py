"""DM 5.4 (M5.3, D-68): a session-day writes to the journal first, then pushes to the engine, then stores."""
from __future__ import annotations

import csv
import itertools
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf
from tests.stockify.test_secrets import SECRET, assert_no_secret, plant_secrets

from stockify import engine, paths, store
from stockify.config import loader
from stockify.daemon import journal, session_day
from stockify.data import packs

SID = "s1"


def pack_records(sha: str) -> list[tuple[int, int, bytes]]:
    data = packs.pack_path(sha).read_bytes()
    out, off = [], 64
    while off < len(data):
        size = struct.unpack_from("<H", data, off + 2)[0]
        kind, _, _, t, _ = packs.PREFIX.unpack_from(data, off)
        out.append((t, packs.RANK[kind], data[off:off + size]))
        off += size
    return out


class Spy:
    """Stands in for the engine: checks the journal before delegating a push."""

    def __init__(self, real, check):
        self._real, self._check = real, check

    def push(self, batch):
        self._check(batch)
        return self._real.push(batch)

    def __getattr__(self, name):
        return getattr(self._real, name)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class SessionDayTest(pf.PaperDayTest):
    """Records fed directly, without threads."""

    def setUp(self):
        super().setUp()
        self.record(seconds=120)
        sha = packs.build({"dataset": "ticks", "from": pf.EXPIRY, "to": pf.EXPIRY, "clock_cadence_ms": 5000})
        self.records = pack_records(sha)
        self.ticks = itertools.count(0, 10)

    def buy_cfg(self, **over):
        """BuyOnFirstTick: one market buy of a call on the 60th event."""
        act = [{"on_event": 60, "place": {"instrument": self.ces[4], "side": "BUY", "qty": 65, "product": "NRML",
                                          "type": "MARKET"}}]
        return self.session_cfg(strategy={"kind": "script", "actions": act}, **over)

    def day(self, cfg=None, day=pf.DAY, sid=SID):
        sd = session_day.SessionDay(sid, day, cfg or self.buy_cfg(), mono=lambda: next(self.ticks))
        self.addCleanup(sd.close)
        return sd

    def test_journal_is_written_before_push(self):
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        path = paths.journal_file(SID, pf.DAY)
        seen = []

        def check(batch):
            data = path.read_bytes()
            self.assertTrue(data.endswith(bytes(batch)), "the journal must already end with the pushed batch")
            seen.append(len(batch))

        sd.engine = Spy(sd.engine, check)
        sd.feed(self.records[:10], None)
        sd.feed(self.records[10:20], None)
        self.assertEqual(len(seen), 2)

    def test_one_fill_and_rows(self):
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        for i in range(0, len(self.records), 7):
            sd.feed(self.records[i:i + 7], None)
        fills = store.session_rows(SID, "fills")
        self.assertEqual(len(fills), 1)
        self.assertEqual((fills[0]["side"], fills[0]["qty"], fills[0]["instrument_id"]), ("BUY", 65, self.ces[4]))
        orders = store.session_rows(SID, "orders", trade_date=pf.EXPIRY)
        self.assertEqual([o["status"] for o in orders], ["COMPLETE"])
        events = store.session_rows(SID, "order_events")
        self.assertEqual([e["event_no"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[-1]["event"], "COMPLETE")
        self.assertEqual(store.session_rows(SID, "charges")[0]["kind"], "order")
        self.assertEqual(store.session_rows(SID, "fills", trade_date="2000-01-01"), [])
        sd.fsync()
        _, _, torn = journal.read(paths.journal_file(SID, pf.DAY))
        self.assertEqual(torn, 0)

    def test_reapplying_outputs_is_harmless(self):
        outputs = {"orders": [{"order_id": 1, "status": "OPEN", "qty": 5}],
                   "order_events": [{"t_ns": 9, "order_id": 1, "event": "SUBMIT", "detail": ""},
                                    {"t_ns": 10, "order_id": 1, "event": "ACTIVATE", "detail": ""}],
                   "fills": [{"fill_id": 1, "order_id": 1, "t_ns": 11, "qty": 5}],
                   "charges": [{"row_no": 1, "total": 7}], "log": []}
        nxt = store.apply_session_outputs(SID, pf.EXPIRY, outputs, first_event_no=1, last_frame=(3, 30))
        again = store.apply_session_outputs(SID, pf.EXPIRY, outputs, first_event_no=1, last_frame=(3, 30))
        self.assertEqual((nxt, again), (3, 3))
        self.assertEqual(len(store.session_rows(SID, "order_events")), 2)
        self.assertEqual(len(store.session_rows(SID, "orders")), 1)
        self.assertEqual(len(store.session_rows(SID, "fills")), 1)
        self.assertEqual(len(store.session_rows(SID, "charges")), 1)

    def test_order_status_follows_its_newest_event(self):
        first = {"orders": [{"order_id": 1, "status": "OPEN"}],
                 "order_events": [{"t_ns": 1, "order_id": 1, "event": "SUBMIT", "detail": ""}],
                 "fills": [], "charges": [], "log": []}
        second = {"orders": [],                                           # later changes are events alone
                  "order_events": [{"t_ns": 2, "order_id": 1, "event": "COMPLETE", "detail": ""}],
                  "fills": [], "charges": [], "log": []}
        n = store.apply_session_outputs(SID, pf.EXPIRY, first, first_event_no=1, last_frame=None)
        n = store.apply_session_outputs(SID, pf.EXPIRY, second, first_event_no=n, last_frame=None)
        self.assertEqual(n, 3)
        store.apply_session_outputs(SID, pf.EXPIRY, first, first_event_no=1, last_frame=None)   # a stale repeat
        self.assertEqual(store.session_rows(SID, "orders")[0]["status"], "COMPLETE")

    def test_a_failed_write_stores_nothing(self):
        bad = {"orders": [{"order_id": 1, "status": "OPEN"}], "order_events": [], "fills": [{"qty": 1}],
               "charges": [], "log": []}                                  # a fill without its id
        with self.assertRaises(Exception):
            store.apply_session_outputs(SID, pf.EXPIRY, bad, first_event_no=1, last_frame=(1, 2))
        self.assertEqual(store.session_rows(SID, "orders"), [])
        self.assertIsNone(store.get_session_day(SID, pf.EXPIRY))

    def test_open_journals_control_when_state_disagrees(self):
        nxt = pf.DAY.fromordinal(pf.DAY.toordinal() + 1)
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        sd.feed(self.records, None)
        sd.engine.finish()
        state = json.loads(sd.engine.eod_state())
        self.assertTrue(state["strategy_enabled"])
        sd.close()
        t0 = self.t("09:14:00") + 86_400 * pf.SEC
        index_only = self.buy_cfg(universe=["NSE:NIFTY 50"])              # the expiry chain is gone the next day
        two = self.day(index_only, day=nxt)
        two.open(state, ["owner"], t0)
        _, body, _ = journal.read(paths.journal_file(SID, nxt))
        first = packs.decode(body, 0)[0]
        self.assertEqual((first["kind"], first["subtype"], first["t_event"]), ("REFDATA", "strategy_control", t0))
        self.assertEqual(first["payload"], {"enabled": False, "reasons": ["owner"]})
        agree = self.day(index_only, day=nxt, sid="s2")
        agree.open(state, [], t0)                                          # agrees: nothing journaled
        self.assertEqual(agree.journal_records, 0)

    def test_control_records_reach_the_engine(self):
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        sd.control(False, ["owner"], self.t("09:15:00"))
        sd.feed(self.records, None)
        self.assertEqual(store.session_rows(SID, "fills"), [])             # paused the whole day: no order
        sd.engine.finish()
        self.assertEqual(json.loads(sd.engine.eod_state())["pause_reasons"], ["owner"])

    def test_frame_mark_stored_with_outputs(self):
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        sd.feed(self.records[:5], (41, 1234))
        row = store.get_session_day(SID, pf.EXPIRY)
        self.assertEqual((row["last_frame_no"], row["last_frame_recv_ns"]), (41, 1234))
        self.assertEqual(row["bundle_sha256"], sd.bundle_sha256)
        sd.feed(self.records[5:6], None)                                   # no mark: the stored one stays
        self.assertEqual(store.get_session_day(SID, pf.EXPIRY)["last_frame_no"], 41)

    def test_ids_are_the_days_instruments(self):
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        self.assertIn(self.idx, sd.ids)
        self.assertIn(self.ces[4], sd.ids)

    def test_open_refuses_a_journal_that_has_records(self):
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        sd.feed(self.records[:3], None)
        sd.close()
        again = self.day()
        with self.assertRaises(session_day.SessionDayError):
            again.open(None, [], self.t("09:14:00"))

    # ── F1, F5: a failure after the append fails the day; a journal failure means no push ─────────────────────────

    def counting(self, sd):
        calls = []
        sd.engine = Spy(sd.engine, lambda batch: calls.append(len(batch)))
        return calls

    def assert_failed_day(self, sd, first_error: str):
        for call in (lambda: sd.feed(self.records[20:25], None),
                     lambda: sd.control(False, ["owner"], self.t("09:20:00"))):
            with self.assertRaises(session_day.SessionDayError) as c:
                call()
            self.assertIn(first_error, str(c.exception))

    def test_a_push_error_fails_the_day(self):
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        real = sd.engine

        class Boom(Spy):
            def push(self, batch):
                raise RuntimeError("engine refused the batch")

        sd.engine = Boom(real, None)
        with self.assertRaises(RuntimeError):
            sd.feed(self.records[:10], None)
        sd.engine = real                                  # even a healthy engine is not trusted again
        self.assert_failed_day(sd, "engine refused the batch")
        self.assertEqual(sd.journal_records, 10)          # the input is not lost: the journal holds it

    def test_a_store_error_fails_the_day(self):
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        with mock.patch.object(store, "apply_session_outputs", side_effect=OSError("db locked")):
            with self.assertRaises(OSError):
                sd.feed(self.records[:10], None)
        self.assert_failed_day(sd, "db locked")
        self.assertEqual(sd.journal_records, 10)

    def test_a_journal_failure_means_no_push(self):
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        calls = self.counting(sd)
        sd.feed(self.records[:5], None)
        with mock.patch.object(sd._journal, "append", side_effect=journal.JournalError("disk full")):
            with self.assertRaises(journal.JournalError):
                sd.feed(self.records[5:10], None)
        self.assertEqual(len(calls), 1)                   # only the first batch was pushed
        self.assert_failed_day(sd, "disk full")
        self.assertEqual(len(calls), 1)

    # ── F3: the cadence is paper.json's ───────────────────────────────────────────────────────────────────────────

    def test_fsync_cadence_comes_from_paper_json(self):
        real = loader.load_json("paper.json")
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        self.assertEqual(sd._journal._every, real["journal"]["fsync_every_s"] * 10 ** 9)
        sd.close()
        changed = {**real, "journal": {"fsync_every_s": 7}}
        with mock.patch.object(loader, "load_json", return_value=changed):
            other = self.day(self.buy_cfg(universe=["NSE:NIFTY 50"]), day=pf.DAY.fromordinal(pf.DAY.toordinal() + 1),
                             sid="s3")
        other.open(None, [], self.t("09:14:00"))
        self.assertEqual(other._journal._every, 7 * 10 ** 9)

    # ── F2: order rows follow the engine's own book ───────────────────────────────────────────────────────────────

    def test_order_rows_equal_the_engines_orders_after_partial_and_full_fills(self):
        ce = self.ces[4]
        act = [{"on_event": 60, "place": {"instrument": ce, "side": "BUY", "qty": 195, "product": "NRML",
                                          "type": "LIMIT", "price": 11620}},      # fills over two batches
               {"on_event": 90, "place": {"instrument": ce, "side": "BUY", "qty": 65, "product": "NRML",
                                          "type": "MARKET"}},                     # fills at once
               {"on_event": 100, "place": {"instrument": ce, "side": "BUY", "qty": 65, "product": "NRML",
                                           "type": "LIMIT", "price": 1000}},      # rests, then is cancelled
               {"on_event": 400, "cancel": 3}]
        sd = self.day(self.session_cfg(strategy={"kind": "script", "actions": act}))
        sd.open(None, [], self.t("09:14:00"))
        for i in range(0, len(self.records), 5):
            sd.feed(self.records[i:i + 5], None)
        sd.engine.finish()
        with tempfile.TemporaryDirectory(prefix="session-orders-") as tmp:
            sd.engine.write_artifacts(tmp)
            with open(Path(tmp) / "orders.csv", newline="", encoding="utf-8") as f:
                book = list(csv.DictReader(f))
        stored = {o["order_id"]: o for o in store.session_rows(SID, "orders")}
        self.assertEqual(len(book), 3)
        for row in book:
            mine = stored[int(row["order_id"])]
            for col in ("status", "filled_qty", "avg_price_paise", "reason", "t_activate_ns", "qty", "price_paise"):
                with self.subTest(order=row["order_id"], col=col):
                    self.assertEqual(str(mine[col]), row[col])
        self.assertEqual([(r["status"], r["filled_qty"]) for r in book],
                         [("COMPLETE", "195"), ("COMPLETE", "65"), ("CANCELLED", "0")])
        self.assertNotEqual(book[2]["reason"], "")

    def test_fills_recompute_filled_qty_and_the_floored_average(self):
        order = {"order_id": 1, "status": "OPEN", "qty": 5, "filled_qty": 0, "avg_price_paise": 0,
                 "reason": "", "t_activate_ns": 0}
        first = {"orders": [order], "order_events": [{"t_ns": 4, "order_id": 1, "event": "SUBMIT", "detail": ""}],
                 "fills": [], "charges": [], "log": []}
        n = store.apply_session_outputs(SID, pf.EXPIRY, first, first_event_no=1, last_frame=None)
        second = {"orders": [],
                  "order_events": [{"t_ns": 5, "order_id": 1, "event": "ACTIVATE", "detail": "limit 11"},
                                   {"t_ns": 6, "order_id": 1, "event": "FILL", "detail": "2@10 x"},
                                   {"t_ns": 7, "order_id": 1, "event": "FILL", "detail": "1@11 x"},
                                   {"t_ns": 8, "order_id": 1, "event": "CANCEL", "detail": "R_TEST"}],
                  "fills": [{"fill_id": 1, "order_id": 1, "t_ns": 6, "qty": 2, "price_paise": 10},
                            {"fill_id": 2, "order_id": 1, "t_ns": 7, "qty": 1, "price_paise": 11}],
                  "charges": [], "log": []}
        store.apply_session_outputs(SID, pf.EXPIRY, second, first_event_no=n, last_frame=None)
        row = store.session_rows(SID, "orders")[0]
        self.assertEqual((row["status"], row["filled_qty"], row["avg_price_paise"], row["reason"],
                          row["t_activate_ns"]), ("CANCELLED", 3, 10, "R_TEST", 5))      # floor(31 / 3)

    # ── F10: carried-over orders are reported ─────────────────────────────────────────────────────────────────────

    def test_orders_carried_from_a_prior_day_are_reported(self):
        out = {"orders": [], "order_events": [{"t_ns": 9, "order_id": 41, "event": "COMPLETE", "detail": ""}],
               "fills": [], "charges": [], "log": []}
        carried: list[int] = []
        store.apply_session_outputs(SID, pf.EXPIRY, out, first_event_no=1, last_frame=None, carried=carried)
        self.assertEqual(carried, [41])
        self.assertEqual(store.session_rows(SID, "orders"), [])
        self.assertEqual(len(store.session_rows(SID, "order_events")), 1)         # its event is stored regardless

    # ── F4, F6: reset and replay ──────────────────────────────────────────────────────────────────────────────────

    def trading_cfg(self):
        ce = self.ces[4]
        return self.session_cfg(strategy={"kind": "script", "actions": [
            {"on_event": 60, "place": {"instrument": ce, "side": "BUY", "qty": 195, "product": "NRML",
                                       "type": "LIMIT", "price": 11620}},
            {"on_event": 90, "place": {"instrument": ce, "side": "BUY", "qty": 65, "product": "NRML",
                                       "type": "MARKET"}}]})

    def all_rows(self, sid=SID):
        return {t: store.session_rows(sid, t) for t in ("orders", "order_events", "fills", "charges")}

    def test_reset_removes_the_days_output_rows_and_never_the_journal(self):
        sd = self.day(self.trading_cfg())
        sd.open(None, [], self.t("09:14:00"))
        sd.feed(self.records, (9, 99))
        store.apply_session_outputs("other", pf.EXPIRY, {"fills": [{"fill_id": 1, "order_id": 1, "qty": 1}]},
                                    first_event_no=1, last_frame=(1, 1))
        size = paths.journal_file(SID, pf.DAY).stat().st_size
        store.reset_session_outputs(SID, pf.EXPIRY)
        self.assertEqual(self.all_rows(), {"orders": [], "order_events": [], "fills": [], "charges": []})
        self.assertEqual(len(store.session_rows("other", "fills")), 1)             # another session is untouched
        self.assertEqual(paths.journal_file(SID, pf.DAY).stat().st_size, size)
        row = store.get_session_day(SID, pf.EXPIRY)
        self.assertEqual(row["bundle_sha256"], sd.bundle_sha256)                    # the day row stays ...
        self.assertEqual((row["last_frame_no"], row["last_frame_recv_ns"]), (None, None))   # ... its mark does not

    def test_replaying_the_journal_gives_the_live_rows(self):
        live = self.day(self.trading_cfg())
        live.open(None, [], self.t("09:14:00"))
        for i in range(0, len(self.records), 13):
            live.feed(self.records[i:i + 13], None)
        rows = self.all_rows()
        self.assertEqual(len(rows["fills"]), 4)
        live.close()                                                                # an open journal: a crash
        store.apply_session_outputs(SID, pf.EXPIRY, {"fills": [{"fill_id": 99, "order_id": 9, "qty": 1}]},
                                    first_event_no=500, last_frame=(1, 1))          # a phantom row from beyond the journal
        again = self.day(self.trading_cfg())
        result = again.replay()
        self.assertEqual(self.all_rows(), rows)
        self.assertEqual(result["records"], len(self.records))
        self.assertEqual(result["next_event_no"], len(rows["order_events"]) + 1)
        self.assertEqual(result["carried_orders"], [])
        again.feed([], None)                                                        # a resumed open day takes more input

    def test_replaying_a_finalised_journal_gives_the_live_rows(self):
        live = self.day(self.trading_cfg())
        live.open(None, [], self.t("09:14:00"))
        live.feed(self.records, None)
        rows = self.all_rows()
        live.close(finalise=True)
        self.assertTrue(journal.is_finalised(paths.journal_file(SID, pf.DAY)))
        again = self.day(self.trading_cfg())
        again.replay()
        self.assertEqual(self.all_rows(), rows)
        with self.assertRaises(session_day.SessionDayError):                        # a finished day takes no input
            again.feed(self.records[:1], None)

    def test_replay_needs_the_days_bundle_and_journal(self):
        with self.assertRaises(session_day.SessionDayError):
            self.day().replay()

    def test_no_secret_in_sandbox_after_a_session_day(self):
        plant_secrets(self)
        sd = self.day()
        sd.open(None, [], self.t("09:14:00"))
        for i in range(0, len(self.records), 9):
            sd.feed(self.records[i:i + 9], (i, i))
        sd.fsync()
        assert_no_secret(self._secret_root, SECRET)


if __name__ == "__main__":
    unittest.main()
