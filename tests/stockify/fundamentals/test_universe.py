"""FS 1: the universe (today's Nifty 500 above the liquidity floor) and NSE's sector classification per stock."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.data.test_nse_files import FakeSession

from stockify import paths, timeutil
from stockify.config import loader
from stockify.daemon.clock import ManualClock
from stockify.data import nse_files, refdata
from stockify.fundamentals import nse_api, store, universe

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "fundamentals"
DAY = date(2026, 11, 17)
CRORE_PAISE = 10_000_000 * 100                       # ₹1 crore = 10^7 rupees = 10^9 paise
LIST_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"


def weekdays_back(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


class UniverseCase(SandboxedDBTest):
    db_modules = (store, refdata)

    def setUp(self):
        super().setUp()
        self.clock = ManualClock(timeutil.ist_ns(DAY, "20:00"))
        self.csv = (FIX / "ind_nifty500list.csv").read_bytes()
        conn = refdata._connect()
        self.iid = {}
        for n, sym in enumerate(("RELIANCE", "ABB", "HDFCBANK"), start=1):
            conn.execute("INSERT INTO instruments (instrument_id, exchange, kind, tradingsymbol, first_seen, "
                         "last_seen, source) VALUES (?, 'NSE', 'EQ', ?, '2024-01-01', '2026-11-17', 'kite_dump')", (n, sym))
            self.iid[sym] = n

    def client(self, routes: dict) -> tuple[nse_api.Client, FakeSession]:
        session = FakeSession(routes, self.clock)
        return nse_api.Client(nse_files.Http(session=session, clock=self.clock)), session

    def eod(self, day: date, sym: str, turnover_paise: int) -> None:
        refdata._connect().execute(
            "INSERT INTO cm_eod (trade_date, instrument_id, series, close_paise, volume, turnover_paise) "
            "VALUES (?, ?, 'EQ', 100, 1, ?)", (day.isoformat(), self.iid[sym], turnover_paise))


class LiquidityTests(UniverseCase):
    def test_the_floor_is_eight_crore(self):
        cfg = loader.load_json("fundamentals.json")
        self.assertEqual(cfg["liquidity_floor_paise"], 8 * CRORE_PAISE)
        self.assertEqual((cfg["liquidity_days"], cfg["filings_from"], cfg["basket_size"], cfg["top_kept"]),
                         (63, "2021-04-01", 20, 50))
        self.assertEqual(cfg["nifty500_list_url"], LIST_URL)

    def test_liquidity_floor_from_cm_eod(self):
        days = weekdays_back(DAY, 70)
        window, older = days[-63:], days[:-63]
        for i, d in enumerate(window):
            # RELIANCE: most days ₹9 crore, a few quiet days; median ₹9 crore. ABB: most days ₹7 crore, a few huge.
            self.eod(d, "RELIANCE", (1 if i % 10 == 0 else 9) * CRORE_PAISE)
            self.eod(d, "ABB", (500 if i % 10 == 0 else 7) * CRORE_PAISE)
            if i % 2 == 0:                               # HDFCBANK traded on half the days only: the others count 0
                self.eod(d, "HDFCBANK", 50 * CRORE_PAISE)
        for d in older:                                   # outside the 63 trading days: never counted
            self.eod(d, "ABB", 900 * CRORE_PAISE)
        self.eod(DAY + timedelta(days=1), "ABB", 900 * CRORE_PAISE)   # after the day: never counted
        client, session = self.client({})
        rows = universe.refresh(DAY, client=client, today_list_csv=self.csv)
        self.assertEqual(session.calls, [])                           # the list was given: nothing fetched
        self.assertEqual(len(rows), 501)
        by = {r["symbol"]: r for r in rows}
        self.assertEqual(by["RELIANCE"]["median_value_paise"], 9 * CRORE_PAISE)
        self.assertEqual(by["ABB"]["median_value_paise"], 7 * CRORE_PAISE)
        self.assertEqual(by["HDFCBANK"]["median_value_paise"], 50 * CRORE_PAISE)   # 32 of 63 days at 50, 31 at 0
        self.assertEqual((by["RELIANCE"]["liquid"], by["ABB"]["liquid"], by["HDFCBANK"]["liquid"]), (1, 0, 1))
        self.assertEqual(by["RELIANCE"]["instrument_id"], self.iid["RELIANCE"])
        self.assertEqual((by["RELIANCE"]["isin"], by["RELIANCE"]["company"], by["RELIANCE"]["list_industry"]),
                         ("INE002A01018", "Reliance Industries Ltd.", "Oil Gas & Consumable Fuels"))
        nobody = by["360ONE"]                                         # not in instruments: no price, not liquid
        self.assertEqual((nobody["instrument_id"], nobody["median_value_paise"], nobody["liquid"]), (None, None, 0))
        self.assertEqual([r["symbol"] for r in universe.current()], ["HDFCBANK", "RELIANCE"])
        self.assertTrue(all(r["as_of"] == "2026-11-17" for r in universe.current()))
        # The list itself is kept as NSE sent it, with its hash.
        raw = paths.fundamentals_raw_dir("nifty500")
        self.assertEqual((raw / "20261117.csv").read_bytes(), self.csv)
        self.assertTrue((raw / "20261117.csv.sha256").is_file())

    def test_too_little_cm_eod_history_is_refused(self):
        for d in weekdays_back(DAY, 62):
            self.eod(d, "RELIANCE", 9 * CRORE_PAISE)
        client, _ = self.client({})
        with self.assertRaisesRegex(universe.NotEnoughHistory, r"62 trading days .* 63"):
            universe.refresh(DAY, client=client, today_list_csv=self.csv)
        self.assertEqual(universe.current(), [])

    def test_the_list_is_fetched_with_the_fallback_when_nsearchives_refuses(self):
        for d in weekdays_back(DAY, 63):
            self.eod(d, "RELIANCE", 9 * CRORE_PAISE)
        client, session = self.client({"nsearchives.nseindia.com/content/indices/ind_nifty500list.csv":
                                       [(403, b"<HTML>Access Denied</HTML>")],
                                       "niftyindices.com/IndexConstituent/ind_nifty500list.csv": [(200, self.csv)]})
        rows = universe.refresh(DAY, client=client)
        self.assertEqual(len(rows), 501)
        self.assertEqual([u for u, _ in session.calls if u.endswith(".csv")],
                         [LIST_URL, "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"])

    def test_a_list_without_nses_header_is_refused(self):
        for d in weekdays_back(DAY, 63):
            self.eod(d, "RELIANCE", 9 * CRORE_PAISE)
        client, _ = self.client({})
        with self.assertRaisesRegex(ValueError, "header"):
            universe.refresh(DAY, client=client, today_list_csv=b"Symbol,Company\nRELIANCE,Reliance\n")

    def test_a_second_refresh_on_the_same_day_replaces_that_day_only(self):
        for d in weekdays_back(DAY, 64):
            self.eod(d, "RELIANCE", 9 * CRORE_PAISE)
        client, _ = self.client({})
        universe.refresh(DAY - timedelta(days=1), client=client, today_list_csv=self.csv)
        universe.refresh(DAY, client=client, today_list_csv=self.csv)
        short = b"Company Name,Industry,Symbol,Series,ISIN Code\nABB India Ltd.,Capital Goods,ABB,EQ,INE117A01022\n"
        universe.refresh(DAY, client=client, today_list_csv=short)
        self.assertEqual(universe.current(), [])                      # ABB has no traded value: not liquid
        self.assertEqual([r["symbol"] for r in store.universe_on("2026-11-16")][:3],
                         ["360ONE", "AADHARHFC", "AARTIIND"])
        self.assertEqual([r["symbol"] for r in store.universe_on("2026-11-17", liquid_only=False)], ["ABB"])


# The shape the plan expects from /api/quote-equity (`industryInfo`). NOT a saved NSE answer: the real endpoint
# answered 403 on 2026-11-17 (tests/fixtures/fundamentals/README.md), so this shape is unverified.
def quote_body(sym: str, macro: str, sector: str, industry: str, basic: str) -> bytes:
    info = {"macro": macro, "sector": sector, "industry": industry, "basicIndustry": basic}
    return json.dumps({"info": {"symbol": sym}, "industryInfo": info}).encode()


class SectorTests(UniverseCase):
    def test_sectors_parsed_from_industry_info(self):
        client, session = self.client({
            "quote-equity?symbol=RELIANCE": [(200, quote_body("RELIANCE", "Energy", "Oil Gas & Consumable Fuels",
                                                              "Petroleum Products", "Refineries & Marketing"))],
            "quote-equity?symbol=HDFCBANK": [(200, quote_body("HDFCBANK", "Financial Services", "Financial Services",
                                                              "Banks", "Private Sector Bank"))],
            "quote-equity?symbol=ABB": [(200, b'{"info":{"symbol":"ABB"}}')],     # no industryInfo: skipped
        })
        n = universe.refresh_sectors(["RELIANCE", "HDFCBANK", "ABB"], DAY, client=client)
        self.assertEqual(n, 2)
        got = store.sectors()
        self.assertEqual(sorted(got), ["HDFCBANK", "RELIANCE"])
        self.assertEqual(got["RELIANCE"], {"symbol": "RELIANCE", "as_of": "2026-11-17", "macro": "Energy",
                                           "sector": "Oil Gas & Consumable Fuels", "industry": "Petroleum Products",
                                           "basic_industry": "Refineries & Marketing"})
        pages = [u for u, _ in session.calls if "get-quotes" in u]
        self.assertEqual(pages, [f"https://www.nseindia.com/get-quotes/equity?symbol={s}"
                                 for s in ("RELIANCE", "HDFCBANK", "ABB")])
        self.assertTrue((paths.fundamentals_raw_dir("quote-equity") / "RELIANCE_20261117.json").is_file())

    def test_blocked_stops_the_run_and_keeps_what_was_read(self):
        denied = (FIX / "quote-equity_RELIANCE_403.html").read_bytes()
        client, _ = self.client({
            "quote-equity?symbol=HDFCBANK": [(200, quote_body("HDFCBANK", "Financial Services", "Financial Services",
                                                              "Banks", "Private Sector Bank"))],
            "quote-equity?symbol=RELIANCE": [(403, denied)],
        })
        with self.assertRaises(nse_api.Blocked):
            universe.refresh_sectors(["HDFCBANK", "RELIANCE", "ABB"], DAY, client=client)
        self.assertEqual(sorted(store.sectors()), ["HDFCBANK"])

    def test_the_latest_reading_of_a_symbol_wins(self):
        store.put_sectors("2026-11-10", [{"symbol": "RELIANCE", "macro": "A", "sector": "B", "industry": "C",
                                          "basic_industry": "D"}])
        store.put_sectors("2026-11-17", [{"symbol": "RELIANCE", "macro": "Energy", "sector": "B", "industry": "C",
                                          "basic_industry": "D"}])
        self.assertEqual(store.sectors()["RELIANCE"]["macro"], "Energy")
