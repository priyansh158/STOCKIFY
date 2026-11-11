"""DM 5.4 (M5.3, D-68): a session-day writes to the journal first, then pushes to the engine, then stores."""
from __future__ import annotations

import itertools
import json
import struct
import unittest

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.daemon import paper_fixture as pf
from tests.stockify.test_secrets import SECRET, assert_no_secret, plant_secrets

from stockify import engine, paths, store
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
