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

    def __init__(self, on_put=None, accept=True):
        self.puts: list[tuple] = []
        self.on_put = on_put
        self.accept = accept

    def put_inject(self, t, session_id, rank, record):
        if self.on_put:
            self.on_put()
        if self.accept:
            self.puts.append((t, session_id, rank, record))
        return self.accept


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


# ── IWB 5.2: the review's fixes ──────────────────────────────────────────────────────────────────────────────────

LONG_CALL = [(CALL, 65)]


class _Err(Exception):
    def __init__(self, text, code=None):
        super().__init__(text)
        self.code = code


def _named(name, module="kiteconnect.exceptions"):
    cls = type(name, (Exception,), {})
    cls.__module__ = module
    return cls


class PartialAnswerTests(PollerBase):
    def test_an_answer_missing_or_nulling_a_required_figure_is_refused_and_kept(self):
        for key in ("span", "exposure", "additional"):
            for how in ("missing", "null"):
                final = {"span": 1500.0, "exposure": 600.0, "additional": 0.0, "option_premium": 10.0}
                if how == "missing":
                    del final[key]
                else:
                    final[key] = None
                p = self.make(FakeKite(ManualClock(T0), result={"final": final}))
                self.q.put_nowait(snap(sid=f"{key}-{how}"))
                p.step()
                row = store.margin_live_rows(session_id=f"{key}-{how}")[0]
                self.assertIn(f"final.{key}", row["error"], (key, how))
                self.assertEqual(json.loads(row["kite_json"])["final"], final)       # what Kite said is kept
                self.assertIsNone(row["kite_total_paise"])
        self.assertEqual(self.disp.puts, [])                                          # nothing was journaled

    def test_a_missing_option_premium_is_fine(self):
        p = self.make(FakeKite(ManualClock(T0), result={"final": {"span": 1500.0, "exposure": 600.0,
                                                                  "additional": 0.0}}))
        self.q.put_nowait(snap())
        p.step()
        [(_, _, _, record)] = self.disp.puts
        self.assertEqual(self.decode(record)["payload"]["option_premium_paise"], 0)

    def test_a_zero_total_is_refused_for_a_future_or_a_short_option_only(self):
        zero = {"final": {"span": 0.0, "exposure": 0.0, "additional": 0.0, "option_premium": 0.0}}
        p = self.make(FakeKite(ManualClock(T0), result=zero))
        self.q.put_nowait(snap(sid="hedged"))                                        # a future and a short call
        self.q.put_nowait(snap(sid="short", legs=[(CALL, -65)]))
        p.step()
        self.assertEqual(self.disp.puts, [])
        for sid in ("hedged", "short"):
            [row] = store.margin_live_rows(session_id=sid)
            self.assertIn("zero", row["error"])
            self.assertIn("final", json.loads(row["kite_json"]))
        self.q.put_nowait(snap(sid="long", legs=LONG_CALL))                          # a bought option can need none
        p.step()
        [(_, sid, _, record)] = self.disp.puts
        self.assertEqual(sid, "long")
        self.assertEqual(self.decode(record)["payload"]["span_paise"], 0)


class JournalFirstTests(PollerBase):
    def test_an_answer_the_dispatcher_did_not_queue_is_an_error_row_that_keeps_kites_answer(self):
        p = self.make(dispatcher=FakeDispatcher(accept=False))
        self.q.put_nowait(snap())
        p.step()
        [row] = store.margin_live_rows()
        self.assertIn("not_journaled", row["error"])
        self.assertIsNone(row["kite_total_paise"])                                   # no applied answer is logged
        self.assertIsNone(row["gap_paise"])
        self.assertEqual(json.loads(row["kite_json"])["final"]["span"], 150000.5)

    def test_the_real_dispatcher_says_whether_it_queued(self):
        from stockify.daemon.dispatcher import Dispatcher
        from stockify.daemon.encoder import FrameEncoder
        from stockify.config import loader
        cfg = {**loader.load_json("paper.json"), "dispatcher_queue_max": 1}
        disp = Dispatcher([], FrameEncoder({}), clock=ManualClock(T0), cfg=cfg, replay=False)
        rank = packs.RANK[packs.REFDATA_K]
        self.assertIs(disp.put_inject(T0, "s1", rank, b"x"), True)
        self.assertIs(disp.put_inject(T0, "s1", rank, b"x"), False)                  # the queue is full: dropped
        self.assertEqual(disp.stats.dropped, 1)

    def test_an_answer_that_arrives_after_stop_is_never_journaled(self):
        holder = []

        class StoppingKite(FakeKite):
            def basket_order_margins(self, orders, *, consider_positions=False, mode=None):
                holder[0].stop()                                                     # the shutdown begins mid-request
                return super().basket_order_margins(orders, consider_positions=consider_positions, mode=mode)

        p = self.make()
        self.kite = StoppingKite(self.clock)
        holder.append(p)
        self.q.put_nowait(snap())
        p.step()
        self.assertEqual(self.disp.puts, [])
        [row] = store.margin_live_rows()
        self.assertIn("stopped", row["error"])
        self.assertIsNone(row["kite_total_paise"])
        self.assertIn("final", json.loads(row["kite_json"]))

    def test_the_module_says_only_what_is_guaranteed(self):
        text = Path(mp.__file__).read_text(encoding="utf-8")
        self.assertIn("queued", text)
        self.assertNotIn("journaled as a\nREFDATA", text)


class RejectionTests(PollerBase):
    def test_only_a_token_rejection_stops_the_recorder(self):
        token, perm = _named("TokenException")("Incorrect api_key or access_token"), \
            _named("PermissionException")("Insufficient permission for that call")
        token.code = perm.code = 403
        for exc, stops in ((token, True), (_Err("Forbidden", code=403), True), (perm, False),
                           (_Err("Insufficient permission for that call", code=403), False),
                           (_Err("Too many requests", code=429), False)):
            p = self.make(FakeKite(ManualClock(T0), error=exc))
            self.q.put_nowait(snap())
            p.step()
            self.assertEqual(len(self.rejected), 1 if stops else 0, repr(exc))
            self.assertTrue(store.margin_live_rows()[-1]["error"])


class BackoffTests(PollerBase):
    def test_a_429_backs_off_exponentially_up_to_every_s_and_a_success_resets_it(self):
        p = self.make()
        self.kite.error = _Err("Too many requests", code=429)
        self.q.put_nowait(snap())
        delays = []
        for _ in range(14):
            self.assertEqual(p.step(), 1)
            delays.append(p.blocked_until_ns - self.clock.now_ns())
            self.advance(delays[-1] / SEC - 0.5)
            self.assertEqual(p.step(), 0)                                            # still backing off
            self.advance(0.5)
        self.assertEqual(delays[:4], [1 * SEC, 2 * SEC, 4 * SEC, 8 * SEC])
        self.assertTrue(all(b >= a for a, b in zip(delays, delays[1:])))
        self.assertEqual(max(delays), 300 * SEC)                                     # capped at every_s
        self.kite.error = None
        self.assertEqual(p.step(), 1)                                                # the retry succeeds
        self.assertEqual(len(self.disp.puts), 1)
        self.kite.error = _Err("Too many requests", code=429)
        self.q.put_nowait(snap(engine_ns=T0 + 600 * SEC))
        self.advance(1)
        p.step()
        self.assertEqual(p.blocked_until_ns - self.clock.now_ns(), 1 * SEC)          # reset by the success

    def test_a_missing_login_writes_one_error_row_per_login_state_change(self):
        import kite_client
        state = {"login": False}
        p = self.make()

        def factory():
            if not state["login"]:
                raise kite_client.KiteNotAuthenticated("no token")
            return self.kite

        p.client_factory = factory
        for n in range(3):
            self.q.put_nowait(snap(engine_ns=T0 + n * 300 * SEC))
            p.step()
            self.advance(300)
        self.assertEqual(len(store.margin_live_rows()), 1)
        self.assertEqual(self.kite.calls, [])
        state["login"] = True
        self.q.put_nowait(snap(engine_ns=T0 + 900 * SEC))
        p.step()
        self.assertEqual(len(self.kite.calls), 1)
        state["login"] = False                                                       # the login is lost again
        self.advance(300)
        self.q.put_nowait(snap(engine_ns=T0 + 1200 * SEC))
        p.step()
        self.assertEqual(len(store.margin_live_rows()), 3)                           # the first, the answer, this one


class SpinAndConfigTests(PollerBase):
    def test_a_poke_with_no_snapshot_does_not_spin_the_thread(self):
        p = self.make()
        p.poke("s1")
        self.advance(5)
        self.assertEqual(p.step(), 0)
        self.assertGreaterEqual(p._next_wait_s(), mp.IDLE_WAIT_S)

    def test_max_rps_defaults_to_3_and_is_bounded_at_10(self):
        from stockify.config import loader
        self.assertEqual(loader.load_json("paper.json")["margin_poll"]["max_rps"], 3)
        self.assertEqual(loader.PAPER_BOUNDS["margin_poll.max_rps"], 10)
        with self.assertRaises(ValueError):
            self.make(cfg={"margin_poll": {"debounce_s": 2, "every_s": 300, "max_rps": 11}})
        self.make(cfg={"margin_poll": {"debounce_s": 2, "every_s": 300, "max_rps": 10}})


class RedactTests(unittest.TestCase):
    def test_the_poller_error_text_hides_header_and_json_tokens(self):
        for text in ("HTTP 403 Authorization: token abcd1234abcd1234:SECRETvalueXYZ rejected",
                     'body {"access_token": "SECRETvalueXYZ", "x": 1}',
                     "access_token=SECRETvalueXYZ&y=1"):
            self.assertNotIn("SECRETvalueXYZ", mp.redact(text), text)
        self.assertIn('"x": 1', mp.redact('body {"access_token": "SECRETvalueXYZ", "x": 1}'))


class LiveReportWindowTests(SandboxedDBTest):
    db_modules = (store,)

    def test_l2_learns_from_every_earlier_answer_not_only_those_in_the_window(self):
        day1 = timeutil.ist_ns(DAY, "10:15:00")
        day2 = timeutil.ist_ns(date(2026, 11, 18), "10:15:00")
        for n in range(3):
            add_live(wall_ns=day1 + n * SEC, gap=-200_000, kite_total=20_200_000)
        add_live(wall_ns=day2, gap=-200_000, kite_total=20_200_000)
        whole = report.report(start=None, end=None)
        window = report.report(start=date(2026, 11, 18), end=None)
        self.assertIn("1 of 1", window)                                              # learned from the earlier day
        self.assertIn("3 of 4", whole)

    def test_the_l2_columns_line_up(self):
        base = timeutil.ist_ns(DAY, "10:15:00")
        for n in range(12):
            add_live(wall_ns=base + n * SEC, gap=-200_000, kite_total=20_200_000)
        lines = report.report(start=None, end=None).splitlines()
        i = next(k for k, ln in enumerate(lines) if ln.startswith("L2"))
        header, row = lines[i + 1], lines[i + 2]
        self.assertIn("11 of 12", row)
        self.assertEqual(len(header), len(row))
        self.assertEqual(header.index("L2 ppm") + len("L2 ppm"), len(row))


if __name__ == "__main__":
    unittest.main()
