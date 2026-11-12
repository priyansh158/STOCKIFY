"""M5.12 (Task 28): /api/trade, the paper-trading API (A2 §6).

Writes are commands: the API queues them in `session_commands` and never changes a session's state itself.
Offline: FastAPI's TestClient, a sandboxed lab, no daemon.
"""
from __future__ import annotations

import copy
import json
import unittest
from datetime import date
from unittest.mock import patch

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import store, timeutil
from stockify.data import refdata

DAY = "2026-11-17"
CFG = {"schema": "stockify.session/1", "name": "orb_nifty",
       "strategy": {"kind": "python", "ref": "stockify.strategies.sma_cross:SmaCross",
                    "params": {"symbol": "NSE:RELIANCE", "fast": 20, "slow": 50}},
       "capital_paise": 10_000_000, "costs": {"allow_unverified": True},
       "universe": ["NSE:RELIANCE"]}


@support.requires_fastapi
class TradeBase(SandboxedDBTest):
    db_modules = (store, refdata)

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.app = app
        self.client = TestClient(app, base_url=support.API_BASE_URL)

    def make(self, sid="s1", status="CREATED", reasons=(), name="n"):
        store.create_session(sid, name, {"capital_paise": 10_000_000}, 1)
        if status != "CREATED":
            store.set_session_state(sid, status, list(reasons))
        return sid

    def day(self, sid, d, nav, *, cov=None, positions=(), cash=0):
        store.save_session_day_end(sid, d, orders=[], positions=list(positions),
                                   equity={"nav_paise": nav, "cash_paise": cash, "margin_paise": 0},
                                   eod_state_json="{}", labels=[])
        if cov is not None:
            store.upsert_session_day(sid, d, coverage=cov[0] / cov[1],
                                     coverage_json=json.dumps({"fraction": f"{cov[0]}/{cov[1]}"}), status="finalised")


class CreateTests(TradeBase):
    def post(self, **body):
        return self.client.post("/api/trade/sessions", json=body)

    def test_create_validates_and_returns_201(self):
        r = self.post(config=copy.deepcopy(CFG))
        self.assertEqual(r.status_code, 201, r.text)
        sid = r.json()["session_id"]
        row = store.get_session(sid)
        self.assertEqual((row["status"], row["name"]), ("CREATED", "orb_nifty"))
        self.assertEqual(row["config"]["universe"], ["NSE:RELIANCE"])
        # an owner-chosen id is used; a duplicate (ignoring case) is a conflict; an unsafe id is refused
        self.assertEqual(self.post(session_id="Mine", config=CFG).status_code, 201)
        self.assertEqual(self.post(session_id="mine", config=CFG).status_code, 409)
        bad = self.post(session_id="..\\evil", config=CFG)
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(len(store.list_sessions()), 2)
        # a create queues no command and starts nothing
        self.assertEqual(store.pending_commands(), [])

    def test_bad_params_return_422_list(self):
        bad = copy.deepcopy(CFG)
        bad["strategy"]["params"] = {"symbol": "NSE:RELIANCE", "fast": "twenty", "colour": "red"}
        r = self.post(config=bad)
        self.assertEqual(r.status_code, 422)
        detail = r.json()["detail"]
        self.assertIsInstance(detail, list)
        self.assertTrue(len(detail) >= 2 and all(isinstance(p, str) for p in detail))
        self.assertTrue(any("colour" in p for p in detail))
        unknown_key = copy.deepcopy(CFG)
        unknown_key["data"] = {"packs": []}
        self.assertEqual(self.post(config=unknown_key).status_code, 422)
        outside = copy.deepcopy(CFG)
        outside["strategy"]["ref"] = "os:system"          # code outside the catalogue is never imported by the API
        r = self.post(config=outside)
        self.assertEqual(r.status_code, 422)
        self.assertIn("catalogue", r.json()["detail"][0])
        self.assertEqual(self.post().status_code, 422)
        self.assertEqual(store.list_sessions(), [])


class ControlTests(TradeBase):
    def test_pause_writes_a_command_not_a_status(self):
        sid = self.make(status="RUNNING")
        r = self.client.post(f"/api/trade/sessions/{sid}/pause")
        self.assertEqual(r.status_code, 202)
        cmd = [c for c in store.pending_commands() if c["cmd_id"] == r.json()["cmd_id"]][0]
        self.assertEqual((cmd["session_id"], cmd["command"]), (sid, "pause"))
        self.assertEqual(store.get_session(sid)["status"], "RUNNING")        # the daemon applies it, not the API
        self.assertEqual(store.get_session(sid)["pause_reasons"], [])
        for verb in ("resume", "stop"):
            self.assertEqual(self.client.post(f"/api/trade/sessions/{sid}/{verb}").status_code, 202)
        self.assertEqual([c["command"] for c in store.pending_commands()], ["pause", "resume", "stop"])
        self.assertEqual(store.get_session(sid)["status"], "RUNNING")

    def test_stopped_is_409_and_unknown_is_404(self):
        sid = self.make(status="STOPPED", reasons=["stopped"])
        for verb in ("pause", "resume", "stop"):
            self.assertEqual(self.client.post(f"/api/trade/sessions/{sid}/{verb}").status_code, 409)
        self.assertEqual(self.client.post("/api/trade/sessions/nope/pause").status_code, 404)
        self.assertIn(self.client.post(f"/api/trade/sessions/{sid}/arm").status_code, (404, 405))
        self.assertEqual(store.pending_commands(), [])

    def test_stop_all_targets_every_session(self):
        self.make("a", status="RUNNING")
        self.make("b", status="STOPPED", reasons=["stopped"])
        r = self.client.post("/api/trade/stop-all")
        self.assertEqual(r.status_code, 202)
        (cmd,) = store.pending_commands()
        self.assertEqual((cmd["cmd_id"], cmd["session_id"], cmd["command"]), (r.json()["cmd_id"], None, "stop_all"))
        self.assertEqual(store.get_session("a")["status"], "RUNNING")

    def test_no_route_can_place_an_order(self):
        posts = sorted(path for path, ops in self.app.openapi()["paths"].items()
                       if "post" in ops and path.startswith("/api/trade"))
        every = sorted(path for path, ops in self.app.openapi()["paths"].items()
                       if path.startswith("/api/trade") and set(ops) - {"get", "post"})
        self.assertEqual(every, [])                                    # no PUT, PATCH or DELETE either
        self.assertEqual(posts, ["/api/trade/sessions", "/api/trade/sessions/{session_id}/pause",
                                 "/api/trade/sessions/{session_id}/resume",
                                 "/api/trade/sessions/{session_id}/stop", "/api/trade/stop-all"])

    def test_loopback_rules_apply(self):
        from fastapi.testclient import TestClient
        foreign = TestClient(self.app, base_url="http://example.com")
        self.assertEqual(foreign.get("/api/trade/sessions").status_code, 400)
        r = self.client.post("/api/trade/stop-all", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(store.pending_commands(), [])


class ReadTests(TradeBase):
    def test_list_and_detail_shapes(self):
        self.make("a", status="PAUSED", reasons=["owner"], name="alpha")
        self.day("a", "2026-11-16", 10_100_000, cov=(99, 100))
        self.day("a", DAY, 10_050_000, cov=(90, 100))
        store.upsert_session_day("a", DAY, labels_json=json.dumps(["refdata_stale"]))
        store.put_parity_result("a", DAY, "journal", "pass", {}, created_ns=5)
        (row,) = self.client.get("/api/trade/sessions").json()
        self.assertEqual(set(row), {"session_id", "name", "status", "pause_reasons", "today_pnl_paise",
                                    "open_positions", "coverage_today", "parity_last", "labels", "created_ist"})
        self.assertEqual((row["status"], row["pause_reasons"], row["parity_last"]), ("PAUSED", ["owner"], "pass"))
        self.assertEqual(row["today_pnl_paise"], -50_000)          # that day's NAV less the day before's
        self.assertAlmostEqual(row["coverage_today"], 0.9)
        self.assertEqual(row["labels"], ["refdata_stale"])
        self.assertEqual(self.client.get("/api/trade/sessions/nope").status_code, 404)
        self.assertEqual(self.client.get("/api/trade/sessions/nope/fills").status_code, 404)

    def test_stats_twice_all_days_and_full_coverage(self):
        self.make("a")                                   # capital 10,000,000
        self.day("a", "2026-11-12", 10_100_000, cov=(99, 100))      # +100,000   full
        self.day("a", "2026-11-13", 10_000_000, cov=(90, 100))      # -100,000   a hole
        self.day("a", "2026-11-16", 10_300_000)                     # +300,000   coverage unknown: not full
        self.day("a", "2026-11-17", 10_250_000, cov=(100, 100))     # -50,000    full
        d = self.client.get("/api/trade/sessions/a").json()
        a, f = d["stats_all"], d["stats_full_coverage"]
        self.assertEqual((a["days"], a["pnl_paise"], a["win_days"], a["loss_days"]), (4, 250_000, 2, 2))
        self.assertEqual((a["best_day_paise"], a["worst_day_paise"]), (300_000, -100_000))
        self.assertEqual(a["max_drawdown_paise"], 100_000)
        self.assertEqual((f["days"], f["pnl_paise"], f["win_days"], f["loss_days"]), (2, 50_000, 1, 1))
        self.assertEqual(f["max_drawdown_paise"], 50_000)
        self.assertTrue(d["has_coverage_holes"])
        self.assertEqual(d["coverage_threshold"], "0.98")

    def test_positions_carry_todays_token(self):
        self.make("a")
        with refdata._connect() as c:
            c.execute("INSERT INTO instruments (instrument_id, exchange, kind, tradingsymbol, first_seen, last_seen, "
                      "source) VALUES (7, 'NFO', 'FUT', 'NIFTYFUT', '2026-01-01', '2026-11-17', 'kite_dump')")
            c.execute("INSERT INTO instrument_tokens (instrument_id, instrument_token, valid_from, valid_to) "
                      "VALUES (7, 111, '2026-01-01', '2026-11-10')")
            c.execute("INSERT INTO instrument_tokens (instrument_id, instrument_token, valid_from, valid_to) "
                      "VALUES (7, 222, '2026-11-10', NULL)")
        self.day("a", DAY, 10_000_000, positions=[{"instrument_id": 7, "product": "NRML", "qty": 65,
                                                  "avg_price_paise": 2_400_000, "mark_paise": 2_410_000,
                                                  "unrealised_paise": 650_000}])
        (p,) = self.client.get("/api/trade/sessions/a/positions").json()
        self.assertEqual((p["instrument_token"], p["instrument_id"], p["qty"], p["tradingsymbol"]),
                         (222, 7, 65, "NIFTYFUT"))
        self.assertEqual(self.client.get("/api/trade/sessions/a").json()["session"]["open_positions"], 1)
        old = self.client.get("/api/trade/sessions/a/positions", params={"trade_date": "2026-11-12"}).json()
        self.assertEqual(old, [])
        self.assertEqual(self.client.get("/api/trade/sessions/a/positions",
                                         params={"trade_date": "yesterday"}).status_code, 422)

    def test_open_positions_come_from_fills_before_the_day_ends(self):
        self.make("a")
        store.upsert_session_day("a", DAY)
        store.apply_session_outputs("a", DAY, {
            "orders": [{"order_id": 1, "instrument_id": 7, "side": "BUY", "product": "NRML", "qty": 65,
                        "status": "OPEN"}],
            "fills": [{"fill_id": 1, "order_id": 1, "t_ns": 5, "instrument_id": 7, "side": "BUY", "qty": 65,
                       "price_paise": 100}]}, first_event_no=1, last_frame=None)
        (p,) = self.client.get("/api/trade/sessions/a/positions").json()
        self.assertEqual((p["qty"], p["avg_price_paise"], p["source"]), (65, 100, "fills"))
        self.assertEqual(len(self.client.get("/api/trade/sessions/a/orders").json()), 1)
        self.assertEqual(len(self.client.get("/api/trade/sessions/a/fills").json()), 1)

    def test_timeline_and_margin_leak_no_paths(self):
        self.make("a")
        store.add_timeline("a", "session_failed", {"error": r"boom at C:\Users\priya\AppData\Local\STOCKIFY\x.sqlite"},
                           wall_ns=1, engine_ns=None)
        store.add_margin_live({"session_id": "a", "wall_ns": 2, "trade_date": DAY, "step": "B1",
                               "kite_total_paise": 100, "ours_total_paise": 90, "gap_paise": 10, "gap_ppm": 100000,
                               "span_file": r"C:\lab\span.spn", "error": None})
        t = self.client.get("/api/trade/sessions/a/timeline")
        self.assertEqual(t.status_code, 200)
        self.assertNotIn("priya", t.text)
        self.assertEqual(t.json()[0]["kind"], "session_failed")
        m = self.client.get("/api/trade/sessions/a/margin")
        self.assertEqual(m.json()[0]["kite_total_paise"], 100)
        self.assertNotIn("spn", m.text)
        self.assertNotIn("lab", m.text)


class RoundTwoTests(TradeBase):
    """WP 5.2: bounds, deduplicated commands, command status, scrubbed rows, clean errors."""

    def post(self, **body):
        return self.client.post("/api/trade/sessions", json=body)

    def test_body_over_64_kib_is_413(self):
        r = self.client.post("/api/trade/sessions", content=json.dumps({"config": CFG, "pad": "x" * 70_000}),
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)
        self.assertIn("64 KiB", r.json()["detail"])
        self.assertEqual(store.list_sessions(), [])

    def test_universe_over_500_is_422(self):
        big = copy.deepcopy(CFG)
        big["universe"] = [f"NSE:S{n}" for n in range(501)]
        r = self.post(config=big)
        self.assertEqual(r.status_code, 422)
        self.assertIn("500", r.json()["detail"][0])
        self.assertEqual(store.list_sessions(), [])

    def test_fifty_live_sessions_is_the_cap(self):
        for n in range(49):
            self.make(f"s{n}")
        self.make("old", status="STOPPED", reasons=["stopped"])      # a stopped session is not live
        self.assertEqual(self.post(config=CFG).status_code, 201)
        r = self.post(config=CFG)
        self.assertEqual(r.status_code, 422)
        self.assertIn("50", r.json()["detail"][0])
        self.assertEqual(len(store.list_sessions()), 51)

    def test_double_pause_returns_one_cmd_id(self):
        sid = self.make(status="RUNNING")
        a = self.client.post(f"/api/trade/sessions/{sid}/pause").json()["cmd_id"]
        b = self.client.post(f"/api/trade/sessions/{sid}/pause").json()["cmd_id"]
        self.assertEqual(a, b)
        c = self.client.post(f"/api/trade/sessions/{sid}/resume").json()["cmd_id"]
        self.assertNotEqual(a, c)
        x = self.client.post("/api/trade/stop-all").json()["cmd_id"]
        self.assertEqual(x, self.client.post("/api/trade/stop-all").json()["cmd_id"])
        self.assertEqual(len(store.pending_commands()), 3)
        store.mark_command(a, 9, "ok")                                # once applied, a new pause is a new command
        self.assertNotEqual(self.client.post(f"/api/trade/sessions/{sid}/pause").json()["cmd_id"], a)

    def test_command_status(self):
        sid = self.make(status="RUNNING")
        a = self.client.post(f"/api/trade/sessions/{sid}/pause").json()["cmd_id"]
        j = self.client.get(f"/api/trade/commands/{a}").json()
        self.assertEqual((j["status"], j["command"], j["session_id"], j["applied_ns"], j["error"]),
                         ("pending", "pause", sid, None, None))
        store.mark_command(a, 5_000_000_000, "ok")
        j = self.client.get(f"/api/trade/commands/{a}").json()
        self.assertEqual((j["status"], j["applied_ns"], j["error"]), ("applied", 5_000_000_000, None))
        self.assertTrue(j["created_ist"] and j["applied_ist"])
        b = self.client.post(f"/api/trade/sessions/{sid}/stop").json()["cmd_id"]
        store.mark_command(b, 6, r"error: s1: failed at C:\Users\priya\x.sqlite")
        j = self.client.get(f"/api/trade/commands/{b}").json()
        self.assertEqual(j["status"], "failed")
        self.assertNotIn("priya", j["error"])
        self.assertEqual(self.client.get("/api/trade/commands/9999").status_code, 404)

    def test_orders_and_fills_are_scrubbed(self):
        self.make("a")
        store.upsert_session_day("a", DAY)
        store.apply_session_outputs("a", DAY, {
            "orders": [{"order_id": 1, "instrument_id": 7, "side": "BUY", "product": "NRML", "qty": 65,
                        "status": "OPEN", "tag": r"C:\Users\priya\secret.txt"}],
            "fills": [{"fill_id": 1, "order_id": 1, "t_ns": 5, "instrument_id": 7, "side": "BUY", "qty": 65,
                       "price_paise": 100, "evidence": r"D:\lab\x"}]}, first_event_no=1, last_frame=None)
        for part in ("orders", "fills"):
            r = self.client.get(f"/api/trade/sessions/a/{part}")
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("priya", r.text)
            self.assertNotIn("lab", r.text)

    def test_malformed_rows_are_a_clean_error_not_a_500(self):
        self.make("a")
        store.upsert_session_day("a", DAY)
        store.apply_session_outputs("a", DAY, {"fills": [{"fill_id": 1, "order_id": 1}]}, first_event_no=1,
                                    last_frame=None)
        r = self.client.get("/api/trade/sessions/a/positions")
        self.assertIn(r.status_code, (200, 409))
        if r.status_code == 409:
            self.assertIsInstance(r.json()["detail"], str)
        self.assertEqual(self.client.get("/api/trade/sessions").status_code, 200)
        self.assertEqual(self.client.get("/api/trade/sessions/a").status_code, 200)

    def test_date_with_trailing_newline_is_422(self):
        self.make("a")
        r = self.client.get("/api/trade/sessions/a/positions", params={"trade_date": "2026-11-17\n"})
        self.assertEqual(r.status_code, 422)

    def test_id_and_body_edges(self):
        self.assertEqual(self.post(session_id="a" * 128, config=CFG).status_code, 201)
        self.assertEqual(self.post(session_id="a" * 129, config=CFG).status_code, 422)
        self.assertEqual(self.post(session_id="CON", config=CFG).status_code, 422)
        self.assertEqual(self.post(config=CFG, extra="x").status_code, 422)
        cpp = copy.deepcopy(CFG)
        cpp["strategy"] = {"kind": "cpp", "ref": "not_in_the_catalogue", "params": {}}
        self.assertEqual(self.post(config=cpp).status_code, 422)
        self.assertEqual(len(store.list_sessions()), 1)

    def test_stop_all_fans_out_to_every_live_session(self):
        from stockify.daemon.dispatcher import Dispatcher
        for sid, st, rs in (("a", "RUNNING", []), ("b", "PAUSED", ["owner"]), ("c", "STOPPED", ["stopped"])):
            self.make(sid, status=st, reasons=rs)

        class Day:
            resumed = False
            last_clock = 0

            def __init__(self, sid):
                self.session_id = sid

            def control(self, enabled, reasons, t):
                pass

        cfg = {"clock_every_s": 1, "commands_every_s": 1, "dispatch": {"batch_max_items": 10, "batch_max_ms": 10},
               "dispatcher_queue_max": 100}
        d = Dispatcher([Day(x) for x in "abc"], None, clock=None, cfg=cfg, replay=False)
        cmd = self.client.post("/api/trade/stop-all").json()["cmd_id"]
        (row,) = [c for c in store.pending_commands() if c["cmd_id"] == cmd]
        result = d._apply(1, row)
        self.assertEqual(store.get_session("a")["pause_reasons"], ["stop_all"])
        self.assertEqual(store.get_session("b")["pause_reasons"], ["owner", "stop_all"])
        self.assertEqual(store.get_session("c")["status"], "STOPPED")
        self.assertTrue(result.startswith("error") and "c:" in result)      # the stopped one is skipped, noted


class LoginTests(TradeBase):
    def test_login_window_follows_owner_tz(self):
        monday = timeutil.ist_ns(date(2026, 11, 16), "05:00")       # before that morning's login window
        with patch.object(timeutil, "now_ns", return_value=monday):
            r = self.client.get("/api/trade/login")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(set(j), {"waiting_for_login", "day", "window_local", "last_usable_local", "market_open_local"})
        self.assertEqual(j["day"], "2026-11-16")
        # 06:00-09:00 IST is 16:30-19:30 PST the evening before; the market opens 09:15 IST = 19:45 PST
        self.assertEqual(j["window_local"], ["16:30 PST", "19:30 PST"])
        self.assertEqual((j["last_usable_local"], j["market_open_local"]), ("19:30 PST", "19:45 PST"))
        self.assertIs(j["waiting_for_login"], False)
        # in summer the same IST times are an hour later on the owner's wall clock
        summer = timeutil.ist_ns(date(2026, 6, 1), "05:00")
        with patch.object(timeutil, "now_ns", return_value=summer):
            j = self.client.get("/api/trade/login").json()
        self.assertEqual(j["window_local"], ["17:30 PDT", "20:30 PDT"])
        # after the day's open the next trading day is named
        late = timeutil.ist_ns(date(2026, 11, 16), "16:00")
        with patch.object(timeutil, "now_ns", return_value=late):
            self.assertEqual(self.client.get("/api/trade/login").json()["day"], "2026-11-17")

    def test_waiting_flag_is_the_existing_status(self):
        with patch("server.routers.kite._daemon_waiting_for_login", return_value=True):
            self.assertIs(self.client.get("/api/trade/login").json()["waiting_for_login"], True)


if __name__ == "__main__":
    unittest.main()
