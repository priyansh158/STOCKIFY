"""IWB 5.1 (M5.6; D-69): asking Kite for the paper book's margin, journaling the answer, logging the gap.

Offline: Kite is a fake client, the dispatcher a recording stub, the clock a ManualClock. `MarginPoller.step()` is
the whole thread body for one instant, so these tests never sleep.
"""
from __future__ import annotations

import json
import queue
import re
import unittest
from datetime import date
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.test_margin_check import ArchiveTest

import stockify.store as store
from stockify import kite_readonly, numerics, timeutil
from stockify.daemon import margin_poller as mp
from stockify.daemon.clock import ManualClock
from stockify.data import packs, refdata
from stockify.fno import live_margin, margin_check as MC, margin_live_report as report

SEC = 1_000_000_000
DAY = date(2026, 11, 17)
T0 = timeutil.ist_ns(DAY, "10:00:00")
CFG = {"margin_poll": {"debounce_s": 2, "every_s": 300, "max_rps": 10}}
FUT, CALL = 9001, 9005
LEGS = [(FUT, 65), (CALL, -65)]
CONTRACTS = {FUT: {"exchange": "NFO", "tradingsymbol": "NIFTY26NOVFUT", "kind": "FUT", "underlying": "NIFTY",
                   "expiry": "2026-11-24", "actual_expiry": None, "strike_paise": None},
             CALL: {"exchange": "NFO", "tradingsymbol": "NIFTY26N1725000CE", "kind": "CE", "underlying": "NIFTY",
                    "expiry": "2026-11-17", "actual_expiry": None, "strike_paise": 2_500_000}}


def fallback_rows(total: int) -> list[dict]:
    return [{"underlying_id": 1, "step": "B5_flat", "scan": total, "somc": 0, "elm": 0, "addons": 0, "total": total}]


def snap(sid: str = "s1", *, engine_ns: int = T0, legs=LEGS, total: int = 20_000_000, reason: str = "periodic"):
    return mp.MarginSnapshot(sid, engine_ns, list(legs), live_margin.portfolio_sha256(list(legs)),
                             fallback_rows(total), reason=reason)


def answer(span="150000.5", exposure="60000.25", additional="0", premium="1000"):
    return {"initial": {"span": 0, "exposure": 0}, "orders": [],
            "final": {"type": "", "span": float(span), "exposure": float(exposure), "additional": float(additional),
                      "option_premium": float(premium), "total": 1.0}}


class FakeDispatcher:
    replay = False

    def __init__(self, on_put=None):
        self.puts: list[tuple] = []
        self.on_put = on_put

    def put_inject(self, t, session_id, rank, record):
        if self.on_put:
            self.on_put()
        self.puts.append((t, session_id, rank, record))


class FakeKite:
    def __init__(self, clock, result=None, error=None):
        self.clock, self.result, self.error = clock, result or answer(), error
        self.calls: list[tuple] = []                      # (clock ns, orders, consider_positions)

    def basket_order_margins(self, orders, *, consider_positions=False, mode=None):
        self.calls.append((self.clock.now_ns(), orders, consider_positions))
        if self.error is not None:
            raise self.error
        return self.result


class PollerBase(SandboxedDBTest):
    db_modules = (store,)

    def make(self, kite=None, *, contracts=None, on_token_rejected=None, dispatcher=None, cfg=CFG):
        self.clock = ManualClock(T0)
        self.q: queue.Queue = queue.Queue()
        self.kite = kite or FakeKite(self.clock)
        self.disp = dispatcher or FakeDispatcher()
        self.rejected: list[int] = []
        return mp.MarginPoller(
            self.q, self.disp, client_factory=lambda: self.kite, clock=self.clock, cfg=cfg,
            contracts=contracts or (lambda iids: [CONTRACTS.get(i) for i in iids]),
            span_info=lambda day, now: ("nsccl.20261117.i1.zip", T0 - 3600 * SEC),
            on_token_rejected=on_token_rejected or (lambda: self.rejected.append(1)))

    def advance(self, seconds: float) -> None:
        self.clock.set(self.clock.now_ns() + int(seconds * SEC))

    def decode(self, record: bytes) -> dict:
        rec, _ = packs.decode(record, 0)
        return rec


class PollerTests(PollerBase):
    def test_fill_debounced_2s_then_one_request(self):
        p = self.make()
        p.poke("s1")
        self.q.put_nowait(snap(reason="poke"))
        self.assertEqual(p.step(), 0)                         # a fill: nothing is asked at once
        self.advance(1.0)
        p.poke("s1")                                          # a second fill inside the window joins the first
        self.q.put_nowait(snap(reason="poke"))
        self.assertEqual(p.step(), 0)
        self.advance(0.9)
        self.assertEqual(p.step(), 0)                         # 1.9 s after the first
        self.advance(0.1)
        self.assertEqual(p.step(), 1)                         # 2.0 s: exactly one request for both fills
        self.assertEqual(len(self.kite.calls), 1)
        self.advance(10)
        self.assertEqual(p.step(), 0)
        self.assertEqual(len(self.kite.calls), 1)

    def test_every_5_minutes_while_positions_open(self):
        p = self.make()
        for n in range(3):                                    # the dispatcher puts one every_s of stamp time
            self.q.put_nowait(snap(engine_ns=T0 + n * 300 * SEC))
            self.assertEqual(p.step(), 1)
            self.assertEqual(p.step(), 0)                     # the same snapshot is never asked twice
            self.advance(300)
        self.assertEqual(len(self.kite.calls), 3)
        self.q.put_nowait(snap(legs=[], engine_ns=T0 + 900 * SEC))      # flat: nothing to ask
        self.assertEqual(p.step(), 0)
        self.assertEqual(len(self.kite.calls), 3)

    def test_answer_is_converted_journaled_and_logged_with_gap(self):
        seen = []
        disp = FakeDispatcher(on_put=lambda: seen.append(len(store.margin_live_rows())))
        p = self.make(dispatcher=disp)
        self.q.put_nowait(snap(total=21_000_000))
        self.assertEqual(p.step(), 1)
        [(t, sid, rank, record)] = disp.puts
        self.assertEqual(seen, [0])                           # journal first: the store row comes after the put
        self.assertEqual((t, sid, rank), (T0, "s1", packs.RANK[packs.REFDATA_K]))
        rec = self.decode(record)
        self.assertEqual((rec["subtype"], rec["instrument_id"]), ("margin_requirement", 0))
        payload = rec["payload"]
        live_margin.check_payload(payload)                    # what both runners accept
        self.assertEqual(payload["span_paise"], 15_000_050)   # Decimal(str(x)) * 100
        self.assertEqual(payload["exposure_paise"], 6_000_025)
        self.assertEqual(payload["additional_paise"], 0)
        self.assertEqual(payload["option_premium_paise"], 100_000)
        self.assertEqual(payload["legs"], [[FUT, 65], [CALL, -65]])
        self.assertEqual(payload["requested_ns"], T0)
        self.assertEqual(payload["source"], "kite_basket")
        [row] = store.margin_live_rows()
        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["portfolio_sha256"], payload["portfolio_sha256"])
        self.assertEqual(row["kite_total_paise"], 21_000_075)
        self.assertEqual(row["ours_total_paise"], 21_000_000)
        self.assertEqual(row["gap_paise"], -75)
        self.assertEqual(row["gap_ppm"], -4)                  # -75 / 21,000,075 = -3.57 ppm, rounded half away
        self.assertEqual((row["step"], row["trade_date"], row["error"]), ("current", "2026-11-17", None))
        self.assertEqual((row["request_ns"], row["response_ns"], row["engine_ns"]), (T0, T0, T0))
        self.assertEqual(json.loads(row["model_json"]), fallback_rows(21_000_000))
        self.assertEqual(json.loads(row["kite_json"])["final"]["span"], 150000.5)
        self.assertEqual(json.loads(row["legs_json"]), [[FUT, 65], [CALL, -65]])
        self.assertEqual(row["span_file"], "nsccl.20261117.i1.zip")
        market = json.loads(row["market_json"])
        self.assertEqual((market["underlying"], market["type"], market["expiry_day"]), ("NIFTY", "hedged_future", True))
        self.assertEqual(market["span_age_s"], 3600)           # kept for the X-9 expiry-day check later
        orders = self.kite.calls[0][1]
        self.assertEqual([(o["tradingsymbol"], o["transaction_type"], o["quantity"], o["product"]) for o in orders],
                         [("NIFTY26NOVFUT", "BUY", 65, "NRML"), ("NIFTY26N1725000CE", "SELL", 65, "NRML")])

    def test_a_kite_answer_that_is_not_a_valid_payload_is_an_error_row_only(self):
        p = self.make(FakeKite(ManualClock(T0), result={"final": {"span": -5, "exposure": 1, "additional": 0}}))
        self.q.put_nowait(snap())
        p.step()
        self.assertEqual(self.disp.puts, [])
        [row] = store.margin_live_rows()
        self.assertIn("span_paise", row["error"])
        self.assertIsNone(row["kite_total_paise"])

    def test_kite_error_logs_redacted_row_without_journal_record(self):
        secret = "abcSECRETtoken123"
        for n, (err, auth) in enumerate([(RuntimeError(f"HTTP 429 slow down access_token={secret}"), False),
                                         (OSError("network down"), False),
                                         (_Coded(403, f"TokenException: access_token={secret}"), True)]):
            p = self.make(FakeKite(ManualClock(T0), error=err))
            self.q.put_nowait(snap(sid=f"s{n}"))
            p.step()
            self.assertEqual(self.rejected, [1] if auth else [], str(err))
        self.assertEqual(self.disp.puts, [])
        rows = store.margin_live_rows()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertTrue(row["error"])
            self.assertNotIn(secret, row["error"])
            self.assertIsNone(row["kite_json"])
            self.assertIsNone(row["gap_paise"])
            self.assertEqual(row["ours_total_paise"], 20_000_000)

    def test_unknown_contract_is_an_error_row_and_no_request(self):
        p = self.make(contracts=lambda iids: [None for _ in iids])
        self.q.put_nowait(snap())
        self.assertEqual(p.step(), 0)
        self.assertEqual(self.kite.calls, [])
        [row] = store.margin_live_rows()
        self.assertIn("unknown contract", row["error"])

    def test_never_more_than_10_requests_per_second(self):
        p = self.make()
        for n in range(30):
            self.q.put_nowait(snap(sid=f"s{n:02d}"))
        sent = 0
        for _ in range(1000):
            sent += p.step()
            self.advance(0.01)
            if sent == 30:
                break
        self.assertEqual(sent, 30)
        times = [c[0] for c in self.kite.calls]
        for t in times:
            self.assertLessEqual(sum(1 for u in times if t - SEC < u <= t), 10)
        self.assertGreaterEqual(times[-1] - times[0], 2 * SEC)

    def test_poller_holds_no_engine_reference(self):
        p = self.make()
        self.q.put_nowait(snap())
        p.step()
        for name, value in vars(p).items():
            self.assertFalse(hasattr(value, "margin_now") or hasattr(value, "push"), name)
        self.assertEqual(sorted(mp.MarginSnapshot.__dataclass_fields__)[:3], ["engine_ns", "fallback", "legs"])
        src = Path(mp.__file__).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*(from|import)\s+stockify\.(engine|daemon\.session_day)", src, re.M))

    def test_consider_positions_false_on_the_wire(self):
        posted = []

        class Inner:
            def _post(self, route, params=None, is_json=False, query_params=None):
                posted.append((route, query_params, is_json, params))
                return answer()

        self.clock = ManualClock(T0)
        self.q = queue.Queue()
        self.disp = FakeDispatcher()
        p = mp.MarginPoller(self.q, self.disp, client_factory=lambda: kite_readonly.ReadOnlyKite(Inner()),
                            clock=self.clock, cfg=CFG, contracts=lambda iids: [CONTRACTS[i] for i in iids],
                            span_info=lambda d, n: None)
        self.q.put_nowait(snap())
        p.step()
        [(route, query, is_json, params)] = posted
        self.assertEqual((route, query, is_json), ("order.margins.basket", {"consider_positions": "false"}, True))
        self.assertEqual(len(params), 2)
        self.assertEqual(len(self.disp.puts), 1)

    def test_a_replay_poller_never_asks(self):
        disp = FakeDispatcher()
        disp.replay = True
        p = self.make(dispatcher=disp)
        p.poke("s1")
        self.q.put_nowait(snap())
        self.advance(5)
        self.assertEqual(p.step(), 0)
        self.assertEqual(self.kite.calls, [])
        self.assertEqual(store.margin_live_rows(), [])

    def test_snapshots_keep_the_latest(self):
        q: queue.Queue = queue.Queue(maxsize=1)
        mp.put_latest(q, snap(engine_ns=1))
        mp.put_latest(q, snap(engine_ns=2))
        self.assertEqual(q.get_nowait().engine_ns, 2)
        self.assertTrue(q.empty())

    def test_config_bounds_are_used(self):
        for bad in ({"debounce_s": 0, "every_s": 300, "max_rps": 10}, {"debounce_s": 2, "every_s": 300, "max_rps": 0}):
            with self.assertRaises(ValueError):
                self.make(cfg={"margin_poll": bad})


class _Coded(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


class DefaultContractsTests(ArchiveTest):
    def test_contracts_come_from_refdata(self):
        from stockify.data import instruments
        fut = instruments.find("NFO", "NIFTY26NOVFUT")
        [row, missing] = mp.refdata_contracts([fut, 123_456_789])
        self.assertEqual((row["exchange"], row["tradingsymbol"], row["kind"]), ("NFO", "NIFTY26NOVFUT", "FUT"))
        self.assertIsNone(missing)


class ScorecardLiveTests(ArchiveTest):
    def test_scorecard_counts_live_rows(self):
        add_live(wall_ns=T0, gap=-75, kite_total=21_000_075)
        add_live(wall_ns=T0 + SEC, error="HTTP 429")
        card = MC.scorecard()
        self.assertEqual(card.rows, [])
        self.assertEqual((card.live_answers, card.live_errors), (1, 1))
        text = MC.scorecard_text(card)
        self.assertIn("Live Kite answers: 1 logged, 1 error", text)


def add_live(*, wall_ns, gap=None, kite_total=None, ours=20_000_000, error=None, sid="s1", typ="future",
             und="NIFTY", span_age_s=3600, step="current") -> int:
    market = {"underlying": und, "type": typ, "expiry_day": False, "span_age_s": span_age_s}
    return store.add_margin_live({
        "session_id": sid, "wall_ns": wall_ns, "portfolio_sha256": "0" * 64, "kite_json": None if error else "{}",
        "model_json": "[]", "engine_ns": wall_ns, "trade_date": timeutil.ns_to_ist(wall_ns).date().isoformat(),
        "legs_json": "[]", "market_json": json.dumps(market), "span_file": "nsccl.i1.zip", "step": step,
        "kite_total_paise": kite_total, "ours_total_paise": ours, "gap_paise": gap,
        "gap_ppm": None if gap is None or not kite_total else numerics_ppm(gap, kite_total),
        "request_ns": wall_ns, "response_ns": wall_ns, "error": error})


def numerics_ppm(gap: int, kite: int) -> int:
    return mp.gap_ppm(gap, kite)


class LiveReportTests(SandboxedDBTest):
    db_modules = (store,)

    def test_live_report_groups(self):
        ten = timeutil.ist_ns(DAY, "10:15:00")
        eleven = timeutil.ist_ns(DAY, "11:15:00")
        add_live(wall_ns=ten, gap=-100_000, kite_total=20_100_000, typ="future", und="NIFTY", span_age_s=3600)
        add_live(wall_ns=ten + SEC, gap=-200_000, kite_total=20_200_000, typ="future", und="NIFTY", span_age_s=3600)
        add_live(wall_ns=eleven, gap=50_000, kite_total=9_950_000, typ="straddle", und="BANKNIFTY",
                 span_age_s=90_000, ours=10_000_000)
        add_live(wall_ns=eleven + SEC, error="HTTP 429")
        text = report.report(start=DAY, end=DAY)
        self.assertIn("4 rows, 3 answers, 1 error", text)
        for word in ("future", "straddle", "NIFTY", "BANKNIFTY", "10:00", "11:00", "experimental"):
            self.assertIn(word, text)
        self.assertRegex(text, r"by portfolio type\n(.*\n)*.*future\s+2\b")
        self.assertRegex(text, r"1 h to 24 h\s+2\b")
        self.assertRegex(text, r"over 24 h\s+1\b")
        # The range filters by IST trade date.
        self.assertIn("no live margin rows", report.report(start=date(2026, 11, 18), end=None))

    def test_l2_is_a_correction_learned_from_earlier_rows_only(self):
        base = timeutil.ist_ns(DAY, "10:15:00")
        for n in range(3):                                    # Kite is always 1 % above the staircase
            add_live(wall_ns=base + n * SEC, gap=-200_000, kite_total=20_200_000)
        text = report.report(start=None, end=None)
        self.assertIn("L2 (experimental", text)
        self.assertIn("2 of 3", text)                         # the first row has no earlier row to learn from


if __name__ == "__main__":
    unittest.main()
