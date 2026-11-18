"""FS 6: the ranking history: the screen's order on the first trading day of each month, from figures usable then."""
from __future__ import annotations

import hashlib
from datetime import date

from tests.stockify.fundamentals.screen_support import CR, DAY, ScreenCase
from tests.stockify.fundamentals.test_screen import five_capital_goods

from stockify import timeutil
from stockify.data import refdata
from stockify.fundamentals import filings, history, pit, screen, store


class HistoryTests(ScreenCase):
    def _late_filing(self, symbol: str, period_end: str, fields: dict, broadcast_day: date) -> None:
        """A filing (here a restatement) broadcast on `broadcast_day` at 18:00 IST."""
        self.n += 1
        url = f"https://nsearchives.nseindia.com/corporate/xbrl/LATE_{self.n}.xml"
        b = timeutil.ist_ns(broadcast_day, "18:00")
        fid = filings.filing_id(symbol, period_end, "Q", "consolidated", b, url)
        row = {"filing_id": fid, "symbol": symbol, "period_start": None, "period_end": period_end,
               "period_kind": "Q", "basis": "consolidated", "broadcast_ns": b, "broadcast_source": "broadCastDate",
               "source": "results", "seq_number": str(self.n), "xbrl_url": url,
               "xbrl_sha256": hashlib.sha256(url.encode()).hexdigest(), "xbrl_file": None, "status": "parsed",
               "reason": None}
        conn = store._connect()
        conn.execute(f"INSERT INTO filings ({', '.join(filings.COLUMNS)}) "
                     f"VALUES ({', '.join('?' * len(filings.COLUMNS))})", tuple(row[c] for c in filings.COLUMNS))
        conn.executemany("INSERT INTO figures (filing_id, field, value_paise, value_raw) VALUES (?, ?, ?, ?)",
                         [(fid, k, v, f"test {v}") for k, v in fields.items()])

    def test_rankings_use_only_figures_known_then(self):
        five_capital_goods(self)
        for iid in range(1, 6):                          # a close on 2025-12-08 too, so that day can be judged
            refdata._connect().execute(
                "INSERT INTO cm_eod (trade_date, instrument_id, series, close_paise, prev_close_paise, volume, "
                "turnover_paise) SELECT '2025-12-08', instrument_id, series, close_paise, 0, 1, 1 FROM cm_eod "
                "WHERE trade_date = ? AND instrument_id = ?", (DAY.isoformat(), iid))
        before = history.rankings(date(2025, 12, 1), date(2025, 12, 31))
        self.assertEqual(sorted(before), ["0.3", "0.5", "0.7"])
        self.assertEqual([e["date"] for e in before["0.5"]], ["2025-12-01"])       # December's first trading day
        self.assertEqual(before["0.5"][0]["symbols"][0], "GOOD")
        self.assertEqual(before["0.5"][0]["symbols"][-1], "BAD")
        self.assertEqual(before["0.5"][0]["symbols"], [r["symbol"] for r in screen.order(DAY, balance=0.5)])

        # BAD restates its September quarter on Thursday 2025-12-04: tripled profit, no debt. Usable from 12-05.
        sept = {"profit_owners": 3000 * CR, "net_profit": 3000 * CR, "paid_up_equity": 10**9 * 1000,
                "face_value": 1000, "revenue_ops": 1000 * CR, "other_income": 100 * CR, "total_income": 1100 * CR,
                "total_expenses": 100 * CR, "finance_costs": 0, "depreciation": 0, "total_equity": 9000 * CR,
                "borrowings_current": 0, "borrowings_noncurrent": 0}
        self._late_filing("BAD", "2025-09-30", sept, date(2025, 12, 4))
        later = [r["symbol"] for r in screen.order(date(2025, 12, 8), balance=0.5)]
        self.assertNotEqual(later[-1], "BAD")           # the restatement does move BAD, once it is known
        self.assertEqual(history.rankings(date(2025, 12, 1), date(2025, 12, 31)), before)

    def test_first_trading_days_and_top_kept(self):
        days = history.rebalance_days(date(2022, 1, 1), date(2022, 4, 30))
        # 2022-01-01 is a Saturday; 2022-03-01 a holiday (Mahashivratri); 2022-04-01 a Friday (a trading day)
        self.assertEqual(days, [date(2022, 1, 3), date(2022, 2, 1), date(2022, 3, 2), date(2022, 4, 1)])
        self.assertEqual(history.rebalance_days(date(2022, 1, 4), date(2022, 2, 1)), [date(2022, 2, 1)])
        self.assertEqual(history.top_kept(), 50)
        self.assertEqual(history.balances(), ["0.3", "0.5", "0.7"])
        self.assertEqual(history.basket_sizes(), [10, 20, 30])

    def test_balances_equal_as_numbers_are_refused(self):
        from stockify.config import loader
        doc = loader.load_json("fundamentals.json")
        for bad in (["0.5", "0.50"], ["0.3", ".3"], ["1", "1.0"]):
            with self.subTest(bad=bad), self.assertRaisesRegex(loader.ConfigError, "balances"):
                loader._check_fundamentals({**doc, "balances": bad})
        loader._check_fundamentals({**doc, "balances": ["0.3", "0.5", "0.7"]})

    def test_a_day_with_no_placed_stock_is_left_out(self):
        five_capital_goods(self)
        got = history.rankings(date(2025, 11, 1), date(2025, 11, 30))   # no close in early November: none placed
        self.assertEqual(got, {"0.3": [], "0.5": [], "0.7": []})

    def test_cached_reads_give_the_same_answers_and_end_with_the_block(self):
        five_capital_goods(self)
        plain = {b: screen.run(DAY, balance=b) for b in (0.3, 0.5, 0.7)}
        with pit.reads_cached():
            self.assertEqual({b: screen.run(DAY, balance=b) for b in (0.3, 0.5, 0.7)}, plain)
            self.assertEqual(screen.run(DAY, balance=0.5), plain[0.5])       # the second time from the cache
            refdata._connect().execute("DELETE FROM cm_eod")
            self.assertEqual(screen.run(DAY, balance=0.5), plain[0.5])       # inside the block: as read before
        self.assertEqual(screen.run(DAY, balance=0.5)["rows"], [])          # after it: read again
