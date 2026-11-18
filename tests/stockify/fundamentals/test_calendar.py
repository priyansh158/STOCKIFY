"""FS 7: the upcoming results calendar from NSE's event calendar (real answer of 2026-11-17 as the fixture)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.data.test_nse_files import FakeSession

from stockify import timeutil
from stockify.daemon.clock import ManualClock
from stockify.data import migrate, nse_files
from stockify.fundamentals import calendar, nse_api, store

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "fundamentals"
DAY = date(2026, 11, 17)
RANGE_SUFFIX = "index=equities&from_date=17-11-2026&to_date=15-12-2026"


class CalendarCase(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        self.clock = ManualClock(timeutil.ist_ns(DAY, "20:00"))
        self.body = (FIX / "event_calendar_2026-11-17.json").read_bytes()

    def universe(self, *symbols: str) -> None:
        store.put_universe(DAY.isoformat(), [
            {"symbol": s, "instrument_id": None, "isin": "INE000000000", "company": s, "median_value_paise": None,
             "liquid": 1, "list_industry": "X"} for s in symbols])

    def client(self, body: bytes | None = None, day: date = DAY) -> tuple[nse_api.Client, FakeSession]:
        suffix = f"index=equities&from_date={day:%d-%m-%Y}&to_date={day + timedelta(days=28):%d-%m-%Y}"
        session = FakeSession({suffix: [(200, self.body if body is None else body)]}, self.clock)
        return nse_api.Client(nse_files.Http(session=session, clock=self.clock)), session

    def refresh(self, day: date = DAY, body: bytes | None = None) -> int:
        client, _ = self.client(body, day)
        return calendar.refresh(day, client=client)


class RefreshTests(CalendarCase):
    def test_migration_adds_the_table(self):
        conn = store._connect()
        self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(results_calendar)")],
                         ["symbol", "meeting_date", "purpose", "period_hint", "read_on"])
        self.assertGreaterEqual(conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0], 4)
        self.assertEqual(len(migrate._files(store.STORE)), conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])

    def test_only_results_meetings_of_universe_stocks(self):
        # In the fixture: GVPTECH twice (results), ANTGRAPHIC (results), BETA (other business), INDIANB (fund raising),
        # LIBAS ("Financial Results/Other business matters"), MUTHOOTMF (results; not in this universe).
        self.universe("GVPTECH", "ANTGRAPHIC", "BETA", "INDIANB", "LIBAS", "ABB")
        self.assertEqual(self.refresh(), 4)
        got = [(r["symbol"], r["meeting_date"]) for r in calendar.upcoming(DAY)]
        self.assertEqual(got, [("LIBAS", "2026-11-17"), ("GVPTECH", "2026-11-18"), ("ANTGRAPHIC", "2026-11-19"),
                               ("GVPTECH", "2026-11-20")])
        self.assertEqual(calendar.upcoming(DAY)[0]["purpose"], "Financial Results/Other business matters")

    def test_asks_for_the_next_28_days_after_the_warmup(self):
        self.universe("GVPTECH")
        client, session = self.client()
        calendar.refresh(DAY, client=client)
        urls = [u for u, _ in session.calls]
        self.assertTrue(urls[-1].endswith("/api/event-calendar?" + RANGE_SUFFIX), urls)
        self.assertIn(calendar.WARMUP, urls)
        self.assertLess(urls.index(calendar.WARMUP), len(urls) - 1)

    def test_a_meeting_nse_no_longer_lists_is_dropped(self):
        self.universe("GVPTECH", "ANTGRAPHIC")
        self.refresh()
        doc = [d for d in json.loads(self.body) if d["symbol"] != "ANTGRAPHIC"]
        self.assertEqual(self.refresh(body=json.dumps(doc).encode()), 2)
        self.assertEqual({r["symbol"] for r in calendar.upcoming(DAY)}, {"GVPTECH"})

    def test_an_answer_that_is_not_a_list_changes_nothing(self):
        self.universe("GVPTECH")
        self.refresh()
        with self.assertRaises(nse_files.NseError):
            self.refresh(body=b'{"error": "x"}')
        self.assertEqual(len(calendar.upcoming(DAY)), 2)


class PeriodHintTests(CalendarCase):
    def test_period_hint_from_month(self):
        want = {1: "Oct–Dec quarter", 2: "Oct–Dec quarter", 3: "Oct–Dec quarter",
                4: "Jan–Mar quarter (and the year)", 5: "Jan–Mar quarter (and the year)",
                6: "Jan–Mar quarter (and the year)", 7: "Apr–Jun quarter", 8: "Apr–Jun quarter",
                9: "Apr–Jun quarter", 10: "Jul–Sep quarter", 11: "Jul–Sep quarter", 12: "Jul–Sep quarter"}
        for month, hint in want.items():
            with self.subTest(month=month):
                self.assertEqual(calendar.period_hint(date(2026, month, 15)), hint)

    def test_hint_stored_with_the_meeting(self):
        self.universe("GVPTECH", "BRNL")
        self.refresh()
        self.assertEqual({r["period_hint"] for r in calendar.upcoming(DAY)}, {"Jul–Sep quarter"})


class UpcomingTests(CalendarCase):
    def test_past_meetings_drop_off_upcoming(self):
        self.universe("GVPTECH", "ANTGRAPHIC", "VALECHAENG")
        self.refresh()
        self.assertEqual(len(calendar.upcoming(DAY)), 4)
        later = calendar.upcoming(date(2026, 11, 19))
        self.assertEqual([(r["symbol"], r["meeting_date"]) for r in later],
                         [("ANTGRAPHIC", "2026-11-19"), ("GVPTECH", "2026-11-20"), ("VALECHAENG", "2026-11-20")])
        self.assertEqual(calendar.upcoming(date(2026, 11, 21)), [])

    def test_days_limits_the_look_ahead(self):
        self.universe("GVPTECH", "BRNL")
        self.refresh()
        self.assertEqual([r["meeting_date"] for r in calendar.upcoming(DAY, days=3)], ["2026-11-18", "2026-11-20"])

    def test_refresh_keeps_meetings_already_past(self):
        self.universe("GVPTECH")
        self.refresh()
        self.refresh(date(2026, 11, 19), body=b"[]")
        conn = store._connect()
        self.assertEqual(conn.execute("SELECT meeting_date FROM results_calendar").fetchall()[0][0], "2026-11-18")
