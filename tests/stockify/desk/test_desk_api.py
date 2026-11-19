"""HD 9.2, 12.2: /api/desk (the verdict chips, one holding's view, its daily chart), as of the latest close.

Offline: FastAPI's TestClient, a sandboxed lab, the holdings a fake snapshot, a Kite fake that records every call
(the desk's requests must make none: it updates once a day, after the session)."""
from __future__ import annotations

import math
import unittest
from datetime import date, timedelta
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

import kite_client
import portfolio_sync
from stockify import kite_readonly, timeutil
from stockify.data import history, instruments, refdata
from stockify.desk import nightly, replay
from stockify.desk import store as desk_store
from tests.stockify.test_bundle import HEADER

DAY = date(2026, 11, 17)                                   # a Tuesday
IN_SESSION = timeutil.ist_ns(DAY, "11:00")
AFTER_CLOSE = timeutil.ist_ns(DAY, "20:00")
TOKENS = {"INFY": 408065, "TCS": 2953217}
FORBIDDEN = ("average_price", "pnl", "cost")
GONE = ("gauge", "ideas", "fno", "lot", "live")            # HD 12: no derivatives, no live view


def _weekdays_to(last: date, n: int) -> list[date]:
    out, d = [], last
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def install() -> dict[str, int]:
    """INFY and TCS on NSE with 400 weekdays of daily bars ending the day before DAY."""
    rows = "".join(f"{tok},1,{sym},{sym},0,,0,0.05,1,EQ,NSE,NSE\n" for sym, tok in TOKENS.items())
    instruments.sync(DAY, {"NSE": f"{HEADER}\n{rows}"}, underlyings={})
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
    """Records every call; the desk's requests must make none."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):  # noqa: ANN204
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: self.calls.append(name)


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
        from server.routers import desk as desk_router
        desk_router.reset()
        self.addCleanup(desk_router.reset)
        self.client = TestClient(app, base_url=support.API_BASE_URL)
        self.ids = install()
        self.views = {s: grade_and_view(self.ids[s], s) for s in self.viewed}
        self.now = AFTER_CLOSE
        self.kite = FakeKite()
        self.snap = snapshot(the_holdings())
        self.authenticated = True
        for target, attr, fn in (
                (desk_router, "now_ns", lambda: self.now),
                (portfolio_sync, "cached_snapshot", lambda *a, **k: self.snap),
                (portfolio_sync, "fetch_snapshot", lambda *a, **k: self.snap),
                (kite_client, "is_authenticated", lambda: self.authenticated),
                (kite_readonly, "client", lambda: self.kite)):
            p = mock.patch.object(target, attr, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def get(self, path: str, status: int = 200) -> dict:
        r = self.client.get(path)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()


def FORBIDDEN_IN(body) -> list[str]:  # noqa: ANN001, N802
    return [k for k in _keys(body) for bad in FORBIDDEN if bad in k]


class ListTests(DeskApiBase):
    def test_list_returns_a_chip_per_holding_and_the_skips(self):
        body = self.get("/api/desk")
        chips = {c["symbol"]: c for c in body["items"]}
        self.assertEqual(sorted(chips), ["INFY", "TCS"])
        for sym in ("INFY", "TCS"):
            c = chips[sym]
            self.assertEqual((c["exchange"], c["instrument_id"], c["verdict"]),
                             ("NSE", self.ids[sym], self.views[sym]["verdict"]))
            self.assertEqual(c["as_of_ns"], AFTER_CLOSE - 86_400 * 10**9)
            self.assertEqual(c["close_date"], "2026-11-16")
            self.assertEqual((c["holding_symbol"], c["holding_exchange"]), (sym, "NSE"))      # the page matches on it
            self.assertNotIn("live", c)
        skipped = {s["symbol"]: s["reason"] for s in body["skipped"]}
        self.assertEqual(skipped, {"SGBMAR29": nightly.NOT_NSE_STOCK, "BSEONLY": nightly.BSE_ONLY})
        self.assertFalse(FORBIDDEN_IN(body))


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
        for model in (schemas.DeskListDTO, schemas.DeskChipDTO, schemas.DeskViewDTO, schemas.DeskSignalDTO):
            for name in model.model_fields:
                self.assertFalse(any(bad in name for bad in FORBIDDEN), f"{model.__name__}.{name}")
        self.assertEqual((body["symbol"], body["instrument_id"]), ("INFY", self.ids["INFY"]))
        self.assertEqual(body["verdict"], self.views["INFY"]["verdict"])
        self.assertEqual(body["price_paise"], self.views["INFY"]["close_paise"])
        self.assertEqual(len(body["signals"]), len(replay.VOTERS))
        for s in body["signals"]:
            self.assertEqual((s["grade"], s["trades"], s["windows_won"], s["windows_total"]), ("strong", 12, 3, 4))
            self.assertNotRegex(s["note"], r"^(Bought|Sold|Flipped) [0-9,]+ ")      # no replay share counts
        self.assertEqual(body["evidence_to"], "2026-07-31")
        self.assertEqual(set(body["agree"]) | set(body["disagree"]),
                         {s["display_name"] for s in body["signals"]})

    def test_the_view_is_the_latest_close_with_its_date_and_nothing_on_options(self):
        from server import schemas
        body = self.get("/api/desk/TCS")
        self.assertEqual((body["close_date"], body["as_of_ns"]), ("2026-11-16", AFTER_CLOSE - 86_400 * 10**9))
        self.assertIn("once a day, after the session", body["plain"])
        self.assertNotIn("option", body["plain"].lower())
        for name in GONE:
            self.assertNotIn(name, body)
            self.assertNotIn(name, schemas.DeskViewDTO.model_fields)
        self.assertFalse(any(n.startswith(("DeskGauge", "DeskIdea")) for n in dir(schemas)))

    def test_during_the_session_it_is_still_the_stored_close_and_kite_is_not_called(self):
        self.now = IN_SESSION
        body = self.get("/api/desk/INFY")
        self.assertEqual(body["price_paise"], self.views["INFY"]["close_paise"])
        self.assertEqual(body["close_date"], "2026-11-16")
        self.get("/api/desk")
        self.get("/api/desk/INFY/chart")
        self.assertEqual(self.kite.calls, [])

    def test_unknown_or_unheld_symbols_are_404(self):
        self.get("/api/desk/NOSUCH", 404)
        self.get("/api/desk/bad%20name!", 404)


class ChartTests(DeskApiBase):
    def test_chart_shape_matches_run_chart(self):
        from server import schemas
        body = self.get("/api/desk/INFY/chart")
        self.assertEqual(set(body), set(schemas.RunChartDTO.model_fields))
        schemas.RunChartDTO.model_validate(body)
        self.assertEqual((body["markers"], body["data_state"], body["interval_s"]), ([], "ok", 86_400))
        self.assertEqual(body["instrument_id"], self.ids["INFY"])
        self.assertTrue(body["bars"])
        self.assertEqual(body["bars"][-1]["d"], "2026-11-16")                # the last stored close; no live candle
        self.assertTrue(body["plots"])
        self.assertLessEqual(len(body["plots"]), 8)
        for p in body["plots"]:
            self.assertEqual(p["kind"], "price")
            self.assertTrue(p["label"].endswith("exit level"), p["label"])
            self.assertTrue(p["points"])
        bar_t = {b["t"] for b in body["bars"]}
        self.assertTrue(all(pt["t"] in bar_t for p in body["plots"] for pt in p["points"]))

    def test_the_chart_is_daily_only_and_cached_until_the_bars_change(self):
        with mock.patch.object(replay, "walk", wraps=replay.walk) as walk:
            day = self.get("/api/desk/INFY/chart")
            self.assertEqual(self.get("/api/desk/INFY/chart?interval=minute"), day)     # no minute chart any more
            self.assertEqual(walk.call_count, len(replay.VOTERS))                        # the second is cached
        self.assertEqual(day["interval_s"], 86_400)


class DegradeTests(DeskApiBase):
    def test_logged_out_returns_the_last_views_not_500(self):
        self.authenticated = False
        self.snap = snapshot([], error="Kite is not logged in")
        body = self.get("/api/desk")
        self.assertEqual(sorted(c["symbol"] for c in body["items"]), ["INFY", "TCS"])
        self.assertTrue(all(c["holding_symbol"] is None for c in body["items"]))      # no holdings while logged out
        self.assertTrue(all(c["close_date"] == "2026-11-16" for c in body["items"]))
        self.assertTrue(body["plain"])
        view = self.get("/api/desk/INFY")
        self.assertIn("not logged in", view["plain"])
        self.assertIn("16 Nov 2026", view["plain"])
        self.get("/api/desk/INFY/chart")
        self.assertEqual(self.kite.calls, [])


    def test_an_expired_login_says_so_and_claims_nothing_is_ungraded(self):
        self.snap = snapshot([], error=portfolio_sync.EXPIRED)
        self.snap.expired = True
        body = self.get("/api/desk")
        self.assertEqual(sorted(c["symbol"] for c in body["items"]), ["INFY", "TCS"])
        self.assertEqual(body["skipped"], [])
        self.assertIn("expired", body["plain"])
        self.assertNotIn("not graded", body["plain"])


class BseHeldTests(DeskApiBase):
    """I-1: a stock held through BSE and listed on NSE is the NSE stock everywhere."""

    def setUp(self):
        super().setUp()
        self.snap = snapshot([holding("INFY", 800001, exchange="BSE"), holding("TCS", TOKENS["TCS"])])

    def test_the_chip_names_the_bse_holding_it_came_from(self):
        chips = {c["symbol"]: c for c in self.get("/api/desk")["items"]}
        self.assertEqual((chips["INFY"]["exchange"], chips["INFY"]["holding_symbol"], chips["INFY"]["holding_exchange"]),
                         ("NSE", "INFY", "BSE"))

    def test_the_view_and_chart_are_the_nse_stocks(self):
        body = self.get("/api/desk/INFY")
        self.assertEqual((body["symbol"], body["instrument_id"]), ("INFY", self.ids["INFY"]))
        self.assertEqual(self.get("/api/desk/INFY/chart")["instrument_id"], self.ids["INFY"])


if __name__ == "__main__":
    unittest.main()
