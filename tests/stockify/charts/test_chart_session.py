"""CH 6.13: a paper session day's chart, read incrementally from its journal, and GET /api/trade/sessions/{id}/chart.

Journals are written with the real `JournalWriter` (BAR records, so the bars are exactly what is appended); the
session's own order and fill rows go through `store.apply_session_outputs`, as the daemon stores them.
"""
from __future__ import annotations

import itertools
import json
import unittest
from datetime import date
from pathlib import Path
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
            calls.append(off)                                      # every decode, the journal reader's own included
            return real(data, off)

        with patch.object(packs, "decode", counting):
            first = chart_session.session_chart(SID, DAY, None)
            n1 = len(calls)
            self.assertEqual(n1, 3)                                # the 3 bars, once each
            same = chart_session.session_chart(SID, DAY, None)
            self.assertEqual(len(calls), n1)                       # nothing new: nothing decoded
            self.assertEqual(same["bars"], first["bars"])
            append_bars(range(3, 5))
            calls.clear()
            third = chart_session.session_chart(SID, DAY, None)
        self.assertEqual(len(calls), 2)                            # only the 2 new records
        self.assertEqual(len(third["bars"]), 5)
        self.assertEqual(third["bars"][:3], first["bars"])

    def test_a_poll_reads_only_the_new_bytes(self):
        make_session()
        write_bundle()
        append_bars(range(40))
        chart_session.session_chart(SID, DAY, None)
        jp = paths.journal_file(SID, _d(DAY))
        size = jp.stat().st_size
        append_bars(range(40, 42))
        grown = jp.stat().st_size - size
        sizes = []
        import builtins
        real_open = builtins.open

        class Spy:
            def __init__(self, f):
                self.f = f

            def read(self, *a):
                data = self.f.read(*a)
                sizes.append(len(data))
                return data

            def __getattr__(self, name):
                return getattr(self.f, name)

            def __enter__(self):
                self.f.__enter__()
                return self

            def __exit__(self, *a):
                return self.f.__exit__(*a)

        def spy_open(file, mode="r", *a, **kw):
            f = real_open(file, mode, *a, **kw)
            return Spy(f) if "b" in mode and str(file).endswith((".evt", ".crc")) else f

        with patch.object(builtins, "open", spy_open):
            chart_session.session_chart(SID, DAY, None)
        self.assertGreater(size, 3_000)
        self.assertLess(sum(sizes), grown + 600)                   # the new bytes, their checksums and two small probes

    def test_a_different_longer_file_at_the_same_path_resets_the_cache(self):
        make_session()
        write_bundle()
        append_bars(range(3))
        self.assertEqual(len(chart_session.session_chart(SID, DAY, None)["bars"]), 3)
        jp = paths.journal_file(SID, _d(DAY))
        other = paths.journal_file("other", _d(DAY))
        append_bars(range(6), sid="other", price=30000)
        jp.write_bytes(other.read_bytes())                  # the same file (same id) now holds another, longer journal
        Path(str(jp) + ".crc").write_bytes(Path(str(other) + ".crc").read_bytes())
        m = chart_session.session_chart(SID, DAY, None)
        self.assertEqual([b["open"] for b in m["bars"]], [30000 + 10 * k for k in range(6)])

    def test_only_the_charted_instruments_records_are_decoded(self):
        two = INST + [{"id": 2, "tradingsymbol": "OTHER", "kind": "EQ", "session_group": "CM", "cas_dates": []}]
        make_session()
        write_bundle(instruments=two)
        w = journal.JournalWriter(paths.journal_file(SID, _d(DAY)), fsync_every_mono_ns=1_000, mono=lambda: next(_mono))
        evs = []
        for k in range(6):
            for iid in (1, 2, 2):
                start = T(9, 15 + k)
                t = start + 59 * NS
                evs.append((t, packs.RANK[packs.BAR_K], packs.enc_bar(iid, t, 60, start, 100, 105, 95, 102, 1, 0)))
        w.append(evs)
        w.close(finalise=False)
        real, calls = packs.decode, []

        def counting(data, off):
            calls.append(off)
            return real(data, off)

        with patch.object(packs, "decode", counting):
            m = chart_session.session_chart(SID, DAY, 1)
        self.assertEqual((len(m["bars"]), len(calls)), (6, 6))             # the 6 of instrument 1, not all 18

    def test_a_failed_read_right_after_a_restart_never_serves_the_old_file(self):
        make_session()
        write_bundle()
        append_bars(range(3))
        self.assertEqual(len(chart_session.session_chart(SID, DAY, None)["bars"]), 3)
        jp = paths.journal_file(SID, _d(DAY))
        other = paths.journal_file("other", _d(DAY))
        append_bars(range(6), sid="other", price=30000)
        data = bytearray(other.read_bytes())
        data[journal.HEADER_SIZE + 88 + 40] ^= 0xFF                      # a damaged record with whole ones after it
        jp.write_bytes(bytes(data))
        Path(str(jp) + ".crc").write_bytes(Path(str(other) + ".crc").read_bytes())
        m = chart_session.session_chart(SID, DAY, None)
        self.assertEqual((m["data_state"], m["bars"]), ("pack unreadable", []))
        self.assertEqual(chart_session.cache_size(), 0)

    def test_two_first_reads_of_one_journal_share_one_scan(self):
        import threading
        make_session()
        write_bundle()
        append_bars(range(10))
        real_read, real_decode = journal.read_from, packs.decode
        inside, gate, calls = threading.Event(), threading.Event(), []

        def slow_read(*a, **kw):
            inside.set()
            gate.wait(5)                                                   # hold the first reader until the second has arrived
            return real_read(*a, **kw)

        def counting(data, off):
            calls.append(off)
            return real_decode(data, off)

        out = []
        t1 = threading.Thread(target=lambda: out.append(chart_session.session_chart(SID, DAY, None)))
        with patch.object(journal, "read_from", slow_read), patch.object(packs, "decode", counting):
            t1.start()
            self.assertTrue(inside.wait(5))
            t2 = threading.Thread(target=lambda: out.append(chart_session.session_chart(SID, DAY, None)))
            t2.start()
            import time
            time.sleep(0.3)
            gate.set()
            t1.join(10)
            t2.join(10)
        self.assertEqual([len(m["bars"]) for m in out], [10, 10])
        self.assertEqual(len(calls), 10)                                   # one scan, not two

    def test_cached_basis_survives_a_transient_read_failure(self):
        make_session()
        write_bundle()
        append_bars(range(3))
        first = chart_session.session_chart(SID, DAY, None)
        self.assertEqual(first["basis"], "raw")
        with patch.object(journal, "read_from", side_effect=journal.JournalError("mid-append")):
            again = chart_session.session_chart(SID, DAY, None)
        self.assertEqual((again["basis"], again["bars"]), ("raw", first["bars"]))

    def test_a_chunked_first_read_gives_the_same_bars(self):
        make_session()
        write_bundle()
        append_bars(range(20))
        whole = chart_session.session_chart(SID, DAY, None)["bars"]
        chart_session.clear_cache()
        with patch.object(journal, "CHUNK_BYTES", 300):
            self.assertEqual(chart_session.session_chart(SID, DAY, None)["bars"], whole)

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

    def test_a_read_creates_no_folder(self):
        make_session()
        put_rows(DAY, [order(1)], [fill(1, 1, T(9, 16, 5), "BUY", 10, 10010)])
        folder = paths.journals_dir() / SID                        # (the journals root only, never the session's folder)
        self.assertFalse(folder.exists())
        m = chart_session.session_chart(SID, DAY, None)
        self.assertEqual(m["data_state"], "no_pack")
        self.assertFalse(folder.exists())

    def test_the_readers_paths_match_the_writers_and_create_nothing(self):
        root = paths.journals_dir()
        d = _d(DAY)
        self.assertEqual(paths.journal_path_ro("fresh", d), root / "fresh" / "20260302.evt")
        self.assertEqual(paths.day_bundle_path_ro("fresh", d), root / "fresh" / "20260302.bundle.json")
        self.assertFalse((root / "fresh").exists())
        self.assertEqual(paths.journal_path_ro("fresh", d), paths.journal_file("fresh", d))    # (this one creates it)
        for bad in ("..\evil", "a/b", "", "x" * 200, "CON"):
            with self.assertRaises(ValueError):
                paths.journal_path_ro(bad, d)
            with self.assertRaises(ValueError):
                paths.day_bundle_path_ro(bad, d)

    def test_a_later_days_order_id_that_collides_with_a_carried_one(self):
        make_session(days=(DAY, DAY2))
        write_bundle(DAY2)
        append_bars(range(3), DAY2)
        put_rows(DAY, [order(1, product="NRML"), order(2, product="NRML")],
                 [fill(1, 1, T(15, 0, day=DAY), "BUY", 10, 10000)])
        # day 2: its own order 1 (not day 1's order 1), a fill of day 1's still-open order 2, and a sell that must
        # close that carried buy under its NRML product
        put_rows(DAY2, [order(1, side="SELL", product="NRML"), order(3, side="SELL", product="NRML")],
                 [fill(1, 1, T(9, 16, 5, day=DAY2), "SELL", 10, 10100),
                  fill(2, 2, T(9, 17, 5, day=DAY2), "BUY", 6, 10050),
                  fill(3, 3, T(9, 18, 5, day=DAY2), "SELL", 6, 10200)])
        m = chart_session.session_chart(SID, DAY2, None)
        self.assertEqual([(x["fill_id"], x["order_id"], x["trade_pnl_paise"]) for x in m["markers"]],
                         [(1, 1, 1000), (2, 2, None), (3, 3, 900)])

    def test_a_position_left_open_in_an_expired_contract_says_the_profit_is_incomplete(self):
        fut = [{"id": 1, "tradingsymbol": "TESTFUT", "kind": "FUT", "session_group": "FO_FUT", "cas_dates": [],
                "expiry": DAY, "actual_expiry": None, "strike_paise": None}]
        make_session()
        write_bundle(instruments=fut)
        append_bars(range(3))
        put_rows(DAY, [order(1, product="NRML")], [fill(1, 1, T(9, 16, 5), "BUY", 10, 10010)])
        m = chart_session.session_chart(SID, DAY, None)
        self.assertIs(m["pnl_complete"], False)
        self.assertEqual(m["skipped_settlements"], 1)
        self.assertIn("expiry", m["notice"])
        write_bundle(instruments=[{**fut[0], "expiry": "2026-03-26"}])          # not yet expired: nothing is missing
        m = chart_session.session_chart(SID, DAY, None)
        self.assertEqual((m["pnl_complete"], m["notice"]), (True, None))


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

    def test_an_interval_this_chart_cannot_honour_is_refused_not_ignored(self):
        self.world()
        for v in ("60", "0", "x"):
            self.assertEqual(self.get(trade_date=DAY, interval_s=v).status_code, 422, v)

    def test_unknown_session_404_and_bad_date_422(self):
        self.world()
        for r in (self.get("nosuch"), self.get(trade_date="2026-03-09"), self.get(instrument=99)):
            self.assertEqual(r.status_code, 404, r.text)
        self.assertNotIn("nosuch", self.get("nosuch").text)
        self.assertNotIn("2026-03-09", self.get(trade_date="2026-03-09").text)
        for q in ({"trade_date": "yesterday"}, {"trade_date": "2026-13-45"}, {"instrument": "abc"}, {"instrument": -1},
                  {"instrument": 2**63}, {"instrument": "1.5"}):
            r = self.get(**q)
            self.assertEqual(r.status_code, 422, (q, r.text))
            self.assertEqual(r.json(), {"detail": "the chart request is not valid"})     # fixed: nothing echoed

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
