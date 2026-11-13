"""CH 6.13: a paper session day's chart, read incrementally from its journal, and GET /api/trade/sessions/{id}/chart.

Journals are written with the real `JournalWriter` (BAR records, so the bars are exactly what is appended); the
session's own order and fill rows go through `store.apply_session_outputs`, as the daemon stores them.
"""
from __future__ import annotations

import itertools
import json
import sys
import unittest
from datetime import date
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.charts.test_chart_model import DAY, NS, T, fill, order

from stockify import paths, store
from stockify.daemon import journal
from stockify.data import packs
from stockify.reporting import chart_session

SID = "paper1"
DAY2 = "2026-03-03"
INST = [{"id": 1, "tradingsymbol": "TEST", "kind": "EQ", "session_group": "CM", "cas_dates": []}]
_mono = itertools.count(0, 10)


def _d(day: str) -> date:
    return date.fromisoformat(day)


def bar_event(k: int, day: str = DAY, iid: int = 1, price: int = 10000) -> tuple[int, int, bytes]:
    start = T(9, 15 + k, day=day)
    t = start + 59 * NS
    return (t, packs.RANK[packs.BAR_K],
            packs.enc_bar(iid, t, 60, start, price + 10 * k, price + 10 * k + 5, price + 10 * k - 5, price + 10 * k + 2,
                          100, 0))


def append_bars(ks, day: str = DAY, sid: str = SID, price: int = 10000, *, final: bool = False) -> None:
    w = journal.JournalWriter(paths.journal_file(sid, _d(day)), fsync_every_mono_ns=1_000, mono=lambda: next(_mono))
    w.append([bar_event(k, day, price=price) for k in ks])
    w.close(finalise=final)


def write_bundle(day: str = DAY, sid: str = SID, instruments=INST) -> None:
    paths.day_bundle_file(sid, _d(day)).write_text(json.dumps({"instruments": instruments, "days": []}), encoding="utf-8")


def put_rows(day: str, orders, fills, sid: str = SID) -> None:
    store.apply_session_outputs(sid, day, {"orders": orders, "fills": fills}, first_event_no=1, last_frame=None)


def make_session(sid: str = SID, days=(DAY,)) -> str:
    store.create_session(sid, "n", {"capital_paise": 10_000_000}, 1)
    for d in days:
        store.upsert_session_day(sid, d, status="finalised")
    return sid


class SessionChartModelTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        chart_session.clear_cache()

    def test_session_chart_builds_bars_from_the_days_journal(self):
        make_session()
        write_bundle()
        append_bars(range(4), final=True)
        put_rows(DAY, [order(1, note="breakout"), order(2, side="SELL")],
                 [fill(1, 1, T(9, 16, 5), "BUY", 10, 10010), fill(2, 2, T(9, 18, 5), "SELL", 10, 10050)])
        m = chart_session.session_chart(SID, DAY, None)
        self.assertEqual((m["data_state"], m["trade_date"], m["days"], m["paper"], m["live"]), ("ok", DAY, [DAY], True, False))
        self.assertEqual([b["start_ns"] for b in m["bars"]], [T(9, 15 + k) for k in range(4)])
        self.assertEqual(m["bars"][0]["open"], 10000)
        self.assertEqual([(x["fill_id"], x["order_id"], x["note"]) for x in m["markers"]], [(1, 1, "breakout"), (2, 2, "")])
        self.assertEqual(m["markers"][1]["trade_pnl_paise"], 400)
        self.assertEqual(m["instrument_id"], 1)
        self.assertEqual(m["instruments"], [{"id": 1, "symbol": "TEST", "kind": "equity"}])

    def test_latest_day_is_the_default_and_live_follows_the_day_row(self):
        make_session(days=(DAY, DAY2))
        store.upsert_session_day(SID, DAY2, status="open")
        write_bundle(DAY2)
        append_bars(range(2), DAY2)
        m = chart_session.session_chart(SID, None, None)
        self.assertEqual((m["trade_date"], m["live"]), (DAY2, True))

    def test_day_picker_lists_session_days(self):
        make_session(days=(DAY2, DAY))
        self.assertEqual(chart_session.session_chart(SID, DAY, None)["days"], [DAY, DAY2])

    def test_day_with_no_journal_says_no_pack(self):
        make_session()
        put_rows(DAY, [order(1)], [fill(1, 1, T(9, 16, 5), "BUY", 10, 10010)])
        m = chart_session.session_chart(SID, DAY, None)
        self.assertEqual((m["data_state"], m["bars"]), ("no_pack", []))
        self.assertEqual(len(m["markers"]), 1)                     # the trades are still listed

    def test_second_call_decodes_only_new_records(self):
        make_session()
        write_bundle()
        append_bars(range(3))
        real = packs.decode
        calls = []

        def counting(data, off):
            if sys._getframe(1).f_globals["__name__"] == chart_session.__name__:    # not the journal's own scan
                calls.append(off)
            return real(data, off)

        with patch.object(packs, "decode", counting):
            first = chart_session.session_chart(SID, DAY, None)
            n1 = len(calls)
            same = chart_session.session_chart(SID, DAY, None)
            self.assertEqual(len(calls), n1)                       # nothing new: nothing decoded
            self.assertEqual(same["bars"], first["bars"])
            append_bars(range(3, 5))
            calls.clear()
            third = chart_session.session_chart(SID, DAY, None)
        self.assertEqual(len(calls), 2)                            # only the two new records
        self.assertEqual(len(third["bars"]), 5)
        self.assertEqual(third["bars"][:3], first["bars"])

    def test_journal_that_shrinks_resets_the_cache(self):
        make_session()
        write_bundle()
        append_bars(range(5))
        self.assertEqual(len(chart_session.session_chart(SID, DAY, None)["bars"]), 5)
        jp = paths.journal_file(SID, _d(DAY))
        for p in jp.parent.glob(jp.stem + ".*"):
            if p.suffix != ".json" and ".bundle" not in p.name:
                p.unlink()
        append_bars(range(2), price=20000)                         # a new, shorter journal at the same path
        m = chart_session.session_chart(SID, DAY, None)
        self.assertEqual([b["open"] for b in m["bars"]], [20000, 20010])

    def test_torn_tail_is_ignored(self):
        make_session()
        write_bundle()
        append_bars(range(4))
        jp = paths.journal_file(SID, _d(DAY))
        whole = jp.read_bytes()
        crc = [p for p in jp.parent.iterdir() if p.name.startswith(jp.stem) and p != jp and ".bundle" not in p.name]
        jp.write_bytes(whole[:-7])                                 # the writer is mid-record
        before = {p: p.read_bytes() for p in crc}
        m = chart_session.session_chart(SID, DAY, None)
        self.assertEqual((m["data_state"], len(m["bars"])), ("ok", 3))
        self.assertEqual(jp.read_bytes(), whole[:-7])              # nothing repaired
        self.assertEqual({p: p.read_bytes() for p in crc}, before)

    def test_unreadable_journal_is_a_state_not_an_error(self):
        make_session()
        write_bundle()
        paths.journal_file(SID, _d(DAY)).write_bytes(b"not a journal at all, but long enough to have a header " * 4)
        m = chart_session.session_chart(SID, DAY, None)
        self.assertEqual((m["data_state"], m["bars"]), ("pack unreadable", []))

    def test_overnight_position_closes_with_the_right_pnl(self):
        make_session(days=(DAY, DAY2))
        write_bundle(DAY2)
        append_bars(range(3), DAY2)
        # the same ids on both days: each day's engine numbers its own orders and fills from 1
        put_rows(DAY, [order(1, product="NRML")], [fill(1, 1, T(15, 0, day=DAY), "BUY", 10, 10000)])
        put_rows(DAY2, [order(1, side="SELL", product="NRML")], [fill(1, 1, T(9, 16, 5, day=DAY2), "SELL", 10, 10100)])
        m = chart_session.session_chart(SID, DAY2, None)
        self.assertEqual(len(m["markers"]), 1)
        mk = m["markers"][0]
        self.assertEqual((mk["fill_id"], mk["order_id"], mk["side"]), (1, 1, "SELL"))
        self.assertEqual(mk["trade_pnl_paise"], 1000)              # (10100 - 10000) * 10, not a lone sell
        self.assertEqual(mk["chunks"], [{"qty": 10, "open_price_paise": 10000, "pnl_paise": 1000}])
        d1 = chart_session.session_chart(SID, DAY, None)
        self.assertEqual([x["trade_pnl_paise"] for x in d1["markers"]], [None])

    def test_old_day_without_note_reads_empty(self):
        make_session()
        write_bundle()
        append_bars(range(2))
        o = order(1)
        del o["note"]                                              # a day finalised before the note existed
        put_rows(DAY, [o], [fill(1, 1, T(9, 15, 30), "BUY", 1, 10000)])
        self.assertEqual(chart_session.session_chart(SID, DAY, None)["markers"][0]["note"], "")

    def test_unknown_day_and_instrument_are_lookup_errors(self):
        make_session()
        write_bundle()
        append_bars(range(2))
        for args in ((SID, "2026-03-09", None), (SID, DAY, 99), ("nosuch", DAY, None)):
            with self.assertRaises(LookupError):
                chart_session.session_chart(*args)
        with self.assertRaises(ValueError):
            chart_session.session_chart(SID, "03/02/2026", None)
        with self.assertRaises(LookupError):
            chart_session.session_chart("..\\evil", DAY, None)

    def test_cache_is_bounded_to_eight_journals(self):
        for n in range(10):
            sid = make_session(f"s{n}")
            write_bundle(sid=sid)
            append_bars(range(2), sid=sid)
            chart_session.session_chart(sid, DAY, None)
        self.assertEqual(chart_session.cache_size(), 8)

    def test_reading_writes_nothing(self):
        make_session()
        write_bundle()
        append_bars(range(3))
        snap = {p: p.read_bytes() for p in paths.journals_dir(SID).iterdir()}
        chart_session.session_chart(SID, DAY, None)
        self.assertEqual({p: p.read_bytes() for p in paths.journals_dir(SID).iterdir()}, snap)


@support.requires_fastapi
class SessionChartApiTests(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        chart_session.clear_cache()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)

    def get(self, sid=SID, **q):
        return self.client.get(f"/api/trade/sessions/{sid}/chart", params=q)

    def world(self):
        make_session()
        write_bundle()
        append_bars(range(3), final=True)
        put_rows(DAY, [order(1, note="<b>x</b>")], [fill(1, 1, T(9, 16, 5), "BUY", 10, 10010)])

    def test_response_says_paper(self):
        self.world()
        r = self.get(trade_date=DAY)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertIs(d["paper"], True)
        self.assertIs(d["live"], False)
        self.assertIn("Paper trading: no real orders", d["plain"])
        self.assertEqual((d["trade_date"], d["days"], d["data_state"]), (DAY, [DAY], "ok"))
        self.assertEqual(d["bars"][0]["open"], "100.00")
        self.assertEqual(d["bars"][0]["t"], T(9, 15) // NS)
        self.assertEqual(d["markers"][0]["id"], "f1")
        self.assertEqual(d["markers"][0]["note"], "<b>x</b>")      # verbatim

    def test_default_day_and_no_journal_day(self):
        make_session(days=(DAY, DAY2))
        r = self.get()
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual((d["trade_date"], d["data_state"], d["bars"]), (DAY2, "no_pack", []))
        self.assertIn("Paper trading: no real orders", d["plain"])

    def test_unknown_session_404_and_bad_date_422(self):
        self.world()
        for r in (self.get("nosuch"), self.get(trade_date="2026-03-09"), self.get(instrument=99)):
            self.assertEqual(r.status_code, 404, r.text)
        self.assertNotIn("nosuch", self.get("nosuch").text)
        self.assertNotIn("2026-03-09", self.get(trade_date="2026-03-09").text)
        for q in ({"trade_date": "yesterday"}, {"trade_date": "2026-13-45"}, {"instrument": "x"}, {"instrument": -1},
                  {"instrument": 2**63}):
            r = self.get(**q)
            self.assertEqual(r.status_code, 422, (q, r.text))

    def test_unsafe_session_id_is_404(self):
        self.world()
        for sid in ("..%5Cevil", "a%00b", "x" * 200):
            r = self.client.get(f"/api/trade/sessions/{sid}/chart")
            self.assertEqual(r.status_code, 404, (sid, r.text))

    def test_non_loopback_host_refused(self):
        self.world()
        r = self.client.get(f"/api/trade/sessions/{SID}/chart", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("bars", r.text)

    def test_no_path_or_exception_text_in_any_response(self):
        self.world()
        paths.journal_file(SID, _d(DAY)).write_bytes(b"garbage " * 20)
        chart_session.clear_cache()
        r = self.get(trade_date=DAY)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data_state"], "pack unreadable")
        for bad in (str(paths.journals_dir(SID)), "Traceback", "JournalError", ".evt"):
            self.assertNotIn(bad, r.text)

    def test_other_session_routes_are_not_shadowed(self):
        self.world()
        self.assertEqual(self.client.get(f"/api/trade/sessions/{SID}/fills").status_code, 200)
        self.assertEqual(self.client.get(f"/api/trade/sessions/{SID}").status_code, 200)


if __name__ == "__main__":
    unittest.main()
