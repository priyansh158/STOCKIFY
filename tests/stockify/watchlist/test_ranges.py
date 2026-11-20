"""CW 2: the 52-week range from daily candles: the window, its boundaries, short history, a leap day."""
from __future__ import annotations

import unittest
from datetime import date, timedelta

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import timeutil
from stockify.watchlist import ranges


def bar(d: date, high: int, low: int, close: int) -> dict:
    return {"ts_ns": timeutil.ist_ns(d, "00:00"), "open_paise": close, "high_paise": high, "low_paise": low,
            "close_paise": close, "volume": 1}


def flat(first: date, n: int, *, high=110, low=90, close=100) -> list[dict]:
    return [bar(first + timedelta(days=i), high, low, close) for i in range(n)]


class PriorRangeTests(unittest.TestCase):
    DAY = date(2026, 11, 17)

    def test_high_low_dates_and_last_close(self):
        bars = flat(self.DAY - timedelta(days=400), 399)
        bars[100] = bar(date.fromisoformat("2026-01-01"), 500, 90, 100)
        bars[200] = bar(date.fromisoformat("2026-04-20"), 110, 7, 100)
        bars[-1] = bar(self.DAY - timedelta(days=1), 110, 90, 123)
        r = ranges.prior_range(bars, self.DAY, 365, 365)
        self.assertEqual((r["high_paise"], r["high_date"]), (500, "2026-01-01"))
        self.assertEqual((r["low_paise"], r["low_date"]), (7, "2026-04-20"))
        self.assertEqual((r["last_close_paise"], r["last_close_date"]), (123, "2026-11-16"))

    def test_the_day_itself_and_later_are_not_prior(self):
        bars = flat(self.DAY - timedelta(days=400), 401)             # the last bar is on DAY
        bars[-1] = bar(self.DAY, 9999, 1, 100)
        bars.append(bar(self.DAY + timedelta(days=1), 9999, 1, 100))
        r = ranges.prior_range(bars, self.DAY, 365, 365)
        self.assertEqual((r["high_paise"], r["low_paise"]), (110, 90))
        self.assertEqual(r["last_close_date"], (self.DAY - timedelta(days=1)).isoformat())

    def test_window_boundary_is_inclusive_at_lookback_days(self):
        start = self.DAY - timedelta(days=365)
        before = flat(start - timedelta(days=30), 29)                # history before the window
        edge = [bar(start, 700, 90, 100)]                            # exactly lookback_days back: in
        just_out = [bar(start - timedelta(days=1), 900, 90, 100)]    # one day older: out
        rest = flat(start + timedelta(days=1), 364)
        r = ranges.prior_range(before + just_out + edge + rest, self.DAY, 365, 300)
        self.assertEqual((r["high_paise"], r["high_date"]), (700, start.isoformat()))
        r2 = ranges.prior_range(before + just_out + rest, self.DAY, 365, 300)
        self.assertEqual(r2["high_paise"], 110)

    def test_short_history_is_none(self):
        self.assertIsNone(ranges.prior_range([], self.DAY, 365, 365))
        self.assertIsNone(ranges.prior_range(flat(self.DAY - timedelta(days=200), 199), self.DAY, 365, 365))
        # the first bar must be at least min_history_days old
        first = self.DAY - timedelta(days=364)
        self.assertIsNone(ranges.prior_range(flat(first, 364), self.DAY, 365, 365))
        first = self.DAY - timedelta(days=365)
        self.assertIsNotNone(ranges.prior_range(flat(first, 365), self.DAY, 365, 365))

    def test_min_history_can_be_shorter_than_the_window(self):
        r = ranges.prior_range(flat(self.DAY - timedelta(days=100), 99), self.DAY, 365, 90)
        self.assertEqual(r["high_paise"], 110)

    def test_leap_day_counts_as_a_day(self):
        day = date(2024, 3, 1)                                       # 2024-02-29 exists
        self.assertEqual(day - timedelta(days=365), date(2023, 3, 2))
        bars = flat(date(2023, 3, 1), 366)                           # 2023-03-01 .. 2024-02-29
        bars[0] = bar(date(2023, 3, 1), 800, 90, 100)                # one day outside the window
        bars[1] = bar(date(2023, 3, 2), 600, 90, 100)                # the first day inside it
        r = ranges.prior_range(bars, day, 365, 360)
        self.assertEqual((r["high_paise"], r["high_date"]), (600, "2023-03-02"))
        self.assertEqual(r["last_close_date"], "2024-02-29")

    def test_tie_takes_the_latest_date(self):
        bars = flat(self.DAY - timedelta(days=400), 399)
        bars[10] = bar(self.DAY - timedelta(days=300), 150, 90, 100)
        bars[20] = bar(self.DAY - timedelta(days=200), 150, 90, 100)
        r = ranges.prior_range(bars, self.DAY, 365, 365)
        self.assertEqual(r["high_date"], (self.DAY - timedelta(days=200)).isoformat())

    def test_unsorted_input_gives_the_same_answer(self):
        bars = flat(self.DAY - timedelta(days=400), 399)
        self.assertEqual(ranges.prior_range(bars, self.DAY, 365, 365),
                         ranges.prior_range(list(reversed(bars)), self.DAY, 365, 365))


class LastCloseTests(unittest.TestCase):
    def test_latest_bar_on_or_before_day(self):
        d = date(2026, 11, 17)
        bars = [bar(d - timedelta(days=2), 1, 1, 11), bar(d - timedelta(days=1), 1, 1, 12), bar(d, 1, 1, 13)]
        self.assertEqual(ranges.last_close(bars, d), (13, d.isoformat()))
        self.assertEqual(ranges.last_close(bars, d - timedelta(days=1)), (12, (d - timedelta(days=1)).isoformat()))
        self.assertIsNone(ranges.last_close(bars, d - timedelta(days=5)))
        self.assertIsNone(ranges.last_close([], d))
