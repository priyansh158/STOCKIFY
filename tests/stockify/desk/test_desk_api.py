"""HD 9.2: /api/desk (the verdict chips, one holding's view, its chart) and the session chain poller.

Offline: FastAPI's TestClient, a sandboxed lab, the holdings a fake snapshot, the tick buffer and Kite fakes. INFY
has options (the chain tests' refdata), TCS has none."""
from __future__ import annotations

import math
import unittest
from datetime import date, timedelta
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import kite_client
import portfolio_sync
import tick_buffer
from stockify import kite_readonly, timeutil
from stockify.data import history, instruments, refdata
from stockify.desk import ideas as ideas_mod
from stockify.desk import nightly, replay
from stockify.desk import store as desk_store
from tests.stockify.desk.test_chain import NEAR, NEXT, _dumps, _q

DAY = date(2026, 11, 17)                                   # a Tuesday
IN_SESSION = timeutil.ist_ns(DAY, "11:00")
AFTER_CLOSE = timeutil.ist_ns(DAY, "20:00")
STRIKES = list(range(1400, 1720, 20))
TOKENS = {"INFY": 408065, "TCS": 2953217}
FORBIDDEN = ("average_price", "pnl", "cost")


def _weekdays_to(last: date, n: int) -> list[date]:
    out, d = [], last
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def install() -> dict[str, int]:
    """INFY (F&O) and TCS on NSE with 400 weekdays of daily bars ending the day before DAY."""
    instruments.sync(DAY, _dumps(STRIKES))
    ids = {}
    for k, sym in enumerate(("INFY", "TCS")):
        iid = instruments.find("NSE", sym)
        ids[sym] = iid
        p = prev = 150_000 + 10_000 * k
        with history._Tx() as conn:
            for i, d in enumerate(_weekdays_to(DAY - timedelta(days=1), 400)):
                p = max(40_000, p + int(900 * math.sin(i / 9.0) + 120 * math.sin(i / 2.3) + 40) // 5 * 5)
                conn.execute("INSERT INTO bars VALUES (?, 'day', ?, ?, ?, ?, ?, 10000000, NULL, '2026-01-01')",
                             (iid, timeutil.ist_ns(d, "00:00"), prev, max(prev, p) + 100, min(prev, p) - 100, p))
                prev = p
    return ids


def grade_and_view(iid: int, sym: str, grade: str = "strong") -> dict:
    """Evidence rows (every voter `grade`) and the closing view of the day before DAY, as the nightly job stores."""
    bars = replay.daily_bars(iid)
    first = timeutil.ns_to_ist(bars[0].start_ns).date().isoformat()
    rows = [{"trade_date": "2026-11-16", "instrument_id": iid, "ref": ref, "params": {"symbol": sym, **extra},
             "from_date": first, "to_date": "2026-07-31", "trades": 12, "cagr": 0.1, "buy_hold_cagr": 0.08,
             "sharpe": 1.0, "buy_hold_sharpe": 0.8, "max_drawdown": 0.2, "win_rate": 0.5, "windows": [],
             "windows_won": 3, "windows_total": 4, "grade": grade, "note": "Fake."}
            for ref, extra in replay.VOTERS]
    desk_store.put_evidence(rows)
    label, view = nightly.closing_view(iid, sym, bars, rows)
    desk_store.put_view("2026-11-16", iid, sym, label, view, AFTER_CLOSE - 86_400 * 10**9)
    return view


def holding(sym: str, token: int, *, exchange: str = "NSE", isin: str = "INE000A01011", qty: int = 800):
    return portfolio_sync.Holding(tradingsymbol=sym, exchange=exchange, quantity=qty, average_price=1234.5,
                                  last_price=1300.0, pnl=655.0, day_change_pct=0.4, instrument_token=token, isin=isin)


def the_holdings():
    return [holding("INFY", TOKENS["INFY"]), holding("TCS", TOKENS["TCS"], isin="INE467B01029"),
            holding("SGBMAR29", 500003, isin="IN0020230001"),
            holding("BSEONLY", 900001, exchange="BSE", isin="INE999Z01011")]


def snapshot(holdings=(), error=None):
    return portfolio_sync.PortfolioSnapshot(holdings=list(holdings), fetched_at=1, error=error)


class FakeKite:
    """`quote` / `ohlc` from canned answers (every listed name, unless `fail`), recording each call."""

    def __init__(self, *, fail: bool = False, spot: float = 1530.0):
        self.fail, self.spot, self.calls = fail, spot, []

    def _one(self, name: str) -> dict:
        if name.startswith("NSE:"):
            return {"last_price": self.spot, "ohlc": {"open": 1500.0, "high": 1545.0, "low": 1490.0, "close": 1510.0}}
        return _q(ltp=12.0, bid=11.5, ask=12.5, oi=1000)

    def quote(self, names):  # noqa: ANN001
        self.calls.append(("quote", list(names)))
        if self.fail:
            raise RuntimeError("Too many requests")
        return {n: self._one(n) for n in names}

    def ohlc(self, names):  # noqa: ANN001
        self.calls.append(("ohlc", list(names)))
        if self.fail:
            raise RuntimeError("Too many requests")
        return {n: self._one(n) for n in names if n.startswith("NSE:")}

    def historical_data(self, token, from_dt, to_dt, interval, **kw):  # noqa: ANN001, ANN003
        self.calls.append(("historical_data", token, interval))
        if self.fail:
            raise RuntimeError("Too many requests")
        start = timeutil.ist_ns(DAY, "09:15")
        return [{"date": timeutil.ns_to_ist(start + i * 60 * 10**9), "open": 1500 + i, "high": 1502 + i,
                 "low": 1499 + i, "close": 1501 + i, "volume": 100} for i in range(5)]

    def margins(self):
        return {"equity": {"net": 100000.0}}

    def basket_order_margins(self, orders, consider_positions=False):  # noqa: ANN001
        return {"initial": {"span": 1000.0, "exposure": 500.0, "additional": 0.0, "option_premium": 0.0},
                "final": {"span": 1000.0, "exposure": 500.0, "additional": 0.0, "option_premium": 0.0}}


class NoBucket:
    def acquire(self) -> None:
        pass


def _keys(obj, out=None):  # noqa: ANN001
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, out)
    return out


@support.requires_fastapi
class DeskApiBase(SandboxedDBTest):
    db_modules = (refdata, history, desk_store)
    viewed = ("INFY", "TCS")                   # the holdings with a stored closing view

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        from server.services import desk_poller
        self.poller = desk_poller
        from server.routers import desk as desk_router
        for reset in (desk_poller.reset, desk_router.reset):
            reset()
            self.addCleanup(reset)
        self.client = TestClient(app, base_url=support.API_BASE_URL)
        self.ids = install()
        self.views = {s: grade_and_view(self.ids[s], s) for s in self.viewed}
        self.now = AFTER_CLOSE
        self.kite = FakeKite()
        self.ticks: dict[int, dict] = {}
        self.subscribed: list = []
        self.snap = snapshot(the_holdings())
        self.authenticated = True
        for target, attr, fn in (
                (desk_poller, "now_ns", lambda: self.now),
                (portfolio_sync, "cached_snapshot", lambda *a, **k: self.snap),
                (portfolio_sync, "fetch_snapshot", lambda *a, **k: self.snap),
                (tick_buffer, "latest_ticks_bulk", lambda tokens: {t: self.ticks[t] for t in tokens
                                                                  if t in self.ticks}),
                (kite_client, "is_authenticated", lambda: self.authenticated),
                (kite_readonly, "client", lambda: self.kite),
                (desk_poller, "_bucket", lambda: NoBucket())):
            p = mock.patch.object(target, attr, side_effect=fn) if attr != "_bucket" else \
                mock.patch.object(target, attr, fn)
            p.start()
            self.addCleanup(p.stop)
        from server.services import ticker_bridge
        p = mock.patch.object(ticker_bridge, "ensure_subscribed",
                              side_effect=lambda tokens, **kw: self.subscribed.append(sorted(tokens)) or True)
        p.start()
        self.addCleanup(p.stop)

    def get(self, path: str, status: int = 200) -> dict:
        r = self.client.get(path)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()


class ListTests(DeskApiBase):
    def test_list_returns_a_chip_per_holding_and_the_skips(self):
        body = self.get("/api/desk")
        chips = {c["symbol"]: c for c in body["items"]}
        self.assertEqual(sorted(chips), ["INFY", "TCS"])
        for sym in ("INFY", "TCS"):
            c = chips[sym]
            self.assertEqual((c["exchange"], c["instrument_id"], c["verdict"], c["live"]),
                             ("NSE", self.ids[sym], self.views[sym]["verdict"], False))
            self.assertEqual(c["as_of_ns"], AFTER_CLOSE - 86_400 * 10**9)
        skipped = {s["symbol"]: s["reason"] for s in body["skipped"]}
        self.assertEqual(skipped, {"SGBMAR29": nightly.NOT_NSE_STOCK, "BSEONLY": nightly.BSE_ONLY})
        self.assertFalse(FORBIDDEN_IN(body))



def FORBIDDEN_IN(body) -> list[str]:  # noqa: ANN001, N802
    return [k for k in _keys(body) for bad in FORBIDDEN if bad in k]


class NeverGradedTests(DeskApiBase):
    viewed = ("INFY",)

    def test_a_holding_never_graded_is_listed_with_a_reason(self):
        body = self.get("/api/desk")
        self.assertEqual([c["symbol"] for c in body["items"]], ["INFY"])
        skipped = {s["symbol"]: s["reason"] for s in body["skipped"]}
        self.assertIn("after the close", skipped["TCS"])
        self.get("/api/desk/TCS", 404)


class ViewTests(DeskApiBase):
    def test_view_has_no_cost_or_pnl_fields(self):
        from server import schemas
        body = self.get("/api/desk/INFY")
        self.assertEqual(FORBIDDEN_IN(body), [])
        for model in (schemas.DeskListDTO, schemas.DeskChipDTO, schemas.DeskViewDTO, schemas.DeskSignalDTO,
                      schemas.DeskGaugeDTO, schemas.DeskIdeaDTO, schemas.DeskIdeaLegDTO):
            for name in model.model_fields:
                self.assertFalse(any(bad in name for bad in FORBIDDEN), f"{model.__name__}.{name}")
        self.assertEqual((body["symbol"], body["instrument_id"]), ("INFY", self.ids["INFY"]))
        self.assertEqual(body["verdict"], self.views["INFY"]["verdict"])
        self.assertEqual(len(body["signals"]), len(replay.VOTERS))
        for s in body["signals"]:
            self.assertEqual((s["grade"], s["trades"], s["windows_won"], s["windows_total"]), ("strong", 12, 3, 4))
            self.assertNotRegex(s["note"], r"^(Bought|Sold|Flipped) [0-9,]+ ")      # no replay share counts
        self.assertEqual(body["evidence_to"], "2026-07-31")
        self.assertTrue(body["fno"])
        self.assertEqual(body["lot"], 400)
        self.assertFalse(body["live"])
        self.assertIsNone(body["gauge"])           # no chain snapshot yet
        self.assertEqual(set(body["agree"]) | set(body["disagree"]),
                         {s["display_name"] for s in body["signals"]})

    def test_a_stock_without_options_says_so(self):
        body = self.get("/api/desk/TCS")
        self.assertFalse(body["fno"])
        self.assertIsNone(body["lot"])
        self.assertIsNone(body["gauge"])
        self.assertEqual(body["ideas"], [])
        self.assertIn("No options are listed on NSE for this stock.", body["plain"])

    def test_unknown_or_unheld_symbols_are_404(self):
        self.get("/api/desk/NOSUCH", 404)
        self.get("/api/desk/bad%20name!", 404)

    def test_live_view_uses_the_latest_tick_and_is_cached(self):
        self.now = IN_SESSION
        self.ticks[TOKENS["INFY"]] = {"ts": IN_SESSION // 10**9 - 5, "ltp": 1531.5, "volume": 0, "oi": 0}
        with mock.patch.object(nightly, "closing_view", wraps=nightly.closing_view) as cv:
            body = self.get("/api/desk/INFY")
            self.get("/api/desk/INFY")
            self.assertEqual(cv.call_count, 1)               # the second request within 30 s is the cached view
        self.assertTrue(body["live"])
        # Before any pass: one ohlc call for the day's range, and its price is newer than the tick.
        self.assertEqual([c[0] for c in self.kite.calls], ["ohlc"])
        self.assertEqual((body["price_paise"], body["as_of_ns"]), (153_000, IN_SESSION))
        self.now = IN_SESSION + 31 * 10**9
        self.ticks[TOKENS["INFY"]] = {"ts": IN_SESSION // 10**9 + 30, "ltp": 1531.5, "volume": 0, "oi": 0}
        body = self.get("/api/desk/INFY")
        self.assertEqual((body["price_paise"], body["as_of_ns"]), (153_150, IN_SESSION + 30 * 10**9))
        self.assertEqual([c[0] for c in self.kite.calls], ["ohlc"])      # the day's range is kept

    def test_ideas_get_the_atr_the_verdict_used(self):
        self.poller.take_pass(IN_SESSION, client=self.kite)
        self.now = IN_SESSION + 10**9
        with mock.patch.object(ideas_mod, "ideas", wraps=ideas_mod.ideas) as fn:
            body = self.get("/api/desk/INFY")
        self.assertTrue(body["ideas"])
        kw = fn.call_args.kwargs
        self.assertGreater(kw["atr_paise"], 0)
        self.assertEqual(fn.call_args.args[5], 800)         # free shares: the holding's quantity


class ChartTests(DeskApiBase):
    def test_chart_shape_matches_run_chart(self):
        from server import schemas
        body = self.get("/api/desk/INFY/chart?interval=day")
        self.assertEqual(set(body), set(schemas.RunChartDTO.model_fields))
        schemas.RunChartDTO.model_validate(body)
        self.assertEqual((body["markers"], body["data_state"], body["interval_s"]), ([], "ok", 86_400))
        self.assertEqual(body["instrument_id"], self.ids["INFY"])
        self.assertTrue(body["bars"])
        self.assertTrue(body["plots"])
        self.assertLessEqual(len(body["plots"]), 8)
        for p in body["plots"]:
            self.assertEqual(p["kind"], "price")
            self.assertTrue(p["label"].endswith("exit level"), p["label"])
            self.assertTrue(p["points"])
        bar_t = {b["t"] for b in body["bars"]}
        self.assertTrue(all(pt["t"] in bar_t for p in body["plots"] for pt in p["points"]))

    def test_minute_chart_is_todays_candles(self):
        self.now = IN_SESSION
        body = self.get("/api/desk/INFY/chart?interval=minute")
        self.assertEqual((body["interval_s"], body["data_state"], body["markers"]), (60, "ok", []))
        self.assertEqual(len(body["bars"]), 5)
        self.assertEqual(body["bars"][0]["d"], DAY.isoformat())
        self.get("/api/desk/INFY/chart?interval=minute")
        self.assertEqual([c[0] for c in self.kite.calls].count("historical_data"), 1)     # cached
        self.get("/api/desk/INFY/chart?interval=week", 422)


class DegradeTests(DeskApiBase):
    def test_logged_out_returns_last_snapshot_not_500(self):
        self.poller.take_pass(IN_SESSION, client=self.kite)            # a good pass while logged in
        taken = desk_store.latest_chain(self.ids["INFY"])["taken_ns"]
        self.assertEqual(taken, IN_SESSION)
        self.authenticated = False
        self.snap = snapshot([], error="Kite is not logged in")
        self.kite.fail = True
        self.now = IN_SESSION + 600 * 10**9
        self.assertEqual(self.poller.take_pass(self.now)["active"], False)
        body = self.get("/api/desk")
        self.assertEqual(sorted(c["symbol"] for c in body["items"]), ["INFY", "TCS"])
        self.assertTrue(all(not c["live"] for c in body["items"]))
        self.assertTrue(body["plain"])
        view = self.get("/api/desk/INFY")
        self.assertFalse(view["live"])
        self.assertEqual(view["gauge"]["taken_ns"], taken)
        self.assertFalse(view["gauge"]["live"])
        self.assertEqual(view["ideas"], [])
        self.assertIn("not logged in", view["plain"])
        self.get("/api/desk/INFY/chart")

    def test_a_refused_quote_batch_keeps_the_last_snapshot(self):
        self.poller.take_pass(IN_SESSION, client=self.kite)
        self.kite.fail = True
        later = IN_SESSION + 60 * 10**9
        out = self.poller.take_pass(later, client=self.kite)
        self.assertTrue(out["error"])
        self.assertEqual(desk_store.latest_chain(self.ids["INFY"])["taken_ns"], IN_SESSION)
        self.now = later + 300 * 10**9
        view = self.get("/api/desk/INFY")
        self.assertEqual(view["gauge"]["taken_ns"], IN_SESSION)
        self.assertFalse(view["gauge"]["live"])


class PollerTests(DeskApiBase):
    def test_poller_does_nothing_outside_the_session(self):
        with mock.patch.object(portfolio_sync, "cached_snapshot") as snap:
            out = self.poller.take_pass(AFTER_CLOSE, client=self.kite)
            out2 = self.poller.take_pass(timeutil.ist_ns(date(2026, 11, 15), "11:00"), client=self.kite)  # a Sunday
        self.assertEqual((out["active"], out2["active"]), (False, False))
        snap.assert_not_called()
        self.assertEqual(self.kite.calls, [])
        self.assertEqual(self.subscribed, [])
        self.assertIsNone(desk_store.latest_chain(self.ids["INFY"]))

    def test_poller_does_nothing_logged_out(self):
        self.authenticated = False
        out = self.poller.take_pass(IN_SESSION, client=self.kite)
        self.assertFalse(out["active"])
        self.assertEqual(self.kite.calls, [])

    def test_a_pass_quotes_every_holding_in_one_batch_and_stores_the_chain(self):
        out = self.poller.take_pass(IN_SESSION, client=self.kite)
        self.assertTrue(out["active"])
        quotes = [c for c in self.kite.calls if c[0] == "quote"]
        self.assertEqual(len(quotes), 1)
        names = quotes[0][1]
        self.assertIn("NSE:INFY", names)
        self.assertIn("NSE:TCS", names)
        self.assertTrue(any(n.startswith("NFO:INFY") for n in names))
        self.assertEqual(self.subscribed, [sorted(TOKENS.values())])
        snap = desk_store.latest_chain(self.ids["INFY"])
        self.assertEqual(snap["taken_ns"], IN_SESSION)
        self.assertEqual(snap["snapshot"]["expiries"], [NEAR, NEXT])
        self.assertIsNone(desk_store.latest_chain(self.ids["TCS"]))
        self.now = IN_SESSION + 10**9
        view = self.get("/api/desk/INFY")
        self.assertTrue(view["live"])
        self.assertEqual(view["price_paise"], 153_000)
        self.assertTrue(view["gauge"]["live"])
        self.assertEqual(view["gauge"]["taken_ns"], IN_SESSION)
        self.assertEqual([c[0] for c in self.kite.calls].count("ohlc"), 0)     # the pass's quotes held the range

    def test_the_thread_starts_and_stops(self):
        p = self.poller.DeskPoller(interval_s=3600)
        with mock.patch.object(self.poller, "take_pass", return_value={"active": False}) as tp:
            p.start()
            p.stop(timeout=5)
        self.assertFalse(p.is_alive())
        self.assertLessEqual(tp.call_count, 1)


if __name__ == "__main__":
    unittest.main()
