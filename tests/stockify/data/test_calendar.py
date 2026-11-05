"""stockify.data.calendar against the shipped config (M1.2; A3 U-02, G15)."""
from __future__ import annotations

import unittest
from datetime import date, datetime

import tests.support  # noqa: F401  — must precede project imports

from stockify import timeutil
from stockify.data import calendar as cal


def at(d: date, hhmm: str) -> int:
    return timeutil.ist_ns(d, hhmm)


def names(group: str, d: date, **kw) -> list[tuple[str, str, str]]:
    out = []
    for p in cal.phases(group, d, **kw):
        out.append((p.phase, timeutil.ns_to_ist(p.start_ns).strftime("%H:%M"),
                    timeutil.ns_to_ist(p.end_ns).strftime("%H:%M")))
    return out


class TradingDayTests(unittest.TestCase):
    def setUp(self):
        cal.reload()

    def test_weekends_and_holidays(self):
        self.assertFalse(cal.is_trading_day("FO", date(2026, 11, 7)))      # Saturday
        self.assertFalse(cal.is_trading_day("CM", date(2026, 1, 26)))      # Republic Day
        self.assertFalse(cal.is_trading_day("FO", date(2026, 11, 10)))     # Diwali-Balipratipada
        self.assertTrue(cal.is_trading_day("FO", date(2026, 11, 9)))

    def test_the_2024_special_saturday_is_not_a_holiday_row(self):
        # NSE's API lists 2024-03-02 "Special Live Trading"; it is a session, not a holiday.
        rows = [r for r in cal.loader.load_csv("calendar/nse_holidays.csv") if r["date"] == "2024-03-02"]
        self.assertEqual(rows, [])

    def test_holiday_list_covers_2024_to_2026(self):
        years = {r["date"][:4] for r in cal.loader.load_csv("calendar/nse_holidays.csv")}
        self.assertEqual(years, {"2024", "2025", "2026"})

    def test_trading_days_and_neighbours(self):
        days = cal.trading_days("FO", date(2026, 11, 6), date(2026, 11, 12))
        self.assertEqual(days, [date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 11), date(2026, 11, 12)])
        self.assertEqual(cal.previous_trading_day("FO", date(2026, 11, 11)), date(2026, 11, 9))
        self.assertEqual(cal.next_trading_day("FO", date(2026, 11, 6)), date(2026, 11, 9))


class PhaseBoundaryTests(unittest.TestCase):
    """U-02: the dated session changes of 2025-12-08 and 2026-08-03."""

    def setUp(self):
        cal.reload()

    def test_fo_futures_preopen_starts_2025_12_08(self):
        self.assertEqual(names("FO_FUT", date(2025, 12, 5)), [("CONTINUOUS", "09:15", "15:30")])
        self.assertEqual(names("FO_FUT", date(2025, 12, 8)),
                         [("PREOPEN", "09:00", "09:15"), ("CONTINUOUS", "09:15", "15:30")])

    def test_preopen_dropped_for_ineligible_contracts(self):
        self.assertEqual(names("FO_FUT", date(2025, 12, 8), preopen=False),
                         [("CONTINUOUS", "09:15", "15:30")])

    def test_fo_close_moves_to_15_40_on_2026_08_03(self):
        self.assertEqual(names("FO_OPT", date(2026, 7, 31)), [("CONTINUOUS", "09:15", "15:30")])
        self.assertEqual(names("FO_OPT", date(2026, 8, 3)), [("CONTINUOUS", "09:15", "15:40")])
        start, end = cal.close_vwap_window("FO_OPT", date(2026, 8, 3))
        self.assertEqual(timeutil.ns_to_ist(start).strftime("%H:%M"), "15:10")
        self.assertEqual(timeutil.ns_to_ist(end).strftime("%H:%M"), "15:40")

    def test_closing_auction_only_from_2026_08_03(self):
        self.assertEqual(names("CM_CAS", date(2026, 7, 31)), names("CM", date(2026, 7, 31)))
        self.assertIn(("CAS", "15:15", "15:35"), names("CM_CAS", date(2026, 8, 3)))

    def test_phase_at_boundaries(self):
        d = date(2026, 8, 3)
        self.assertIsNone(cal.phase_at("FO_OPT", at(d, "09:14")))
        self.assertEqual(cal.phase_at("FO_OPT", at(d, "09:15")), "CONTINUOUS")
        self.assertEqual(cal.phase_at("FO_OPT", at(d, "15:39")), "CONTINUOUS")
        self.assertIsNone(cal.phase_at("FO_OPT", at(d, "15:40")))       # end is exclusive
        self.assertEqual(cal.phase_at("FO_FUT", at(d, "09:05")), "PREOPEN")

    def test_no_phases_on_a_holiday(self):
        self.assertEqual(cal.phases("FO_OPT", date(2026, 11, 10)), [])

    def test_ist_conversion_is_fixed_offset(self):
        # 09:15 IST is 03:45 UTC every day of the year (no daylight saving).
        for d in (date(2026, 1, 5), date(2026, 7, 6)):
            utc = datetime.fromtimestamp(at(d, "09:15") // 10**9, tz=timeutil.timezone.utc)
            self.assertEqual((utc.hour, utc.minute), (3, 45))


class FuturesPreopenEligibilityTests(unittest.TestCase):
    def setUp(self):
        cal.reload()
        self.months = [date(2026, 11, 24), date(2026, 12, 29), date(2027, 1, 26)]

    def test_near_month_is_eligible(self):
        self.assertTrue(cal.futures_preopen_eligible(date(2026, 11, 24), date(2026, 11, 2), self.months))

    def test_next_month_only_in_the_last_5_trading_days(self):
        nxt = date(2026, 12, 29)
        self.assertFalse(cal.futures_preopen_eligible(nxt, date(2026, 11, 13), self.months))
        self.assertTrue(cal.futures_preopen_eligible(nxt, date(2026, 11, 18), self.months))

    def test_far_month_never(self):
        self.assertFalse(cal.futures_preopen_eligible(date(2027, 1, 26), date(2026, 11, 23), self.months))


class ExpiryRuleTests(unittest.TestCase):
    def setUp(self):
        cal.reload()

    def test_thursday_to_tuesday_switch_g15(self):
        got = [(e["date"], e["kind"]) for e in
               cal.generated_expiries("NIFTY", date(2025, 8, 20), date(2025, 9, 10))]
        self.assertEqual(got, [(date(2025, 8, 21), "weekly"), (date(2025, 8, 28), "monthly"),
                               (date(2025, 9, 2), "weekly"), (date(2025, 9, 9), "weekly")])

    def test_holiday_shifts_to_the_previous_trading_day(self):
        got = {e["nominal"]: e["date"] for e in
               cal.generated_expiries("NIFTY", date(2026, 3, 1), date(2026, 3, 31))}
        self.assertEqual(got[date(2026, 3, 3)], date(2026, 3, 2))      # Holi, a Tuesday
        self.assertEqual(got[date(2026, 3, 31)], date(2026, 3, 30))    # Mahavir Jayanti, monthly

    def test_monthly_only_for_banknifty(self):
        got = cal.generated_expiries("BANKNIFTY", date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual([(e["date"], e["kind"]) for e in got], [(date(2026, 11, 23), "monthly")])

    def test_no_rules_no_invented_history(self):
        self.assertEqual(cal.generated_expiries("NIFTY", date(2024, 1, 1), date(2024, 1, 31)), [])

    def test_validate_reports_disagreement(self):
        actual = [date(2026, 11, 3), date(2026, 11, 17), date(2026, 11, 23)]   # 11-09 missing; 11-17 fine
        report = cal.validate_expiries("NIFTY", actual)
        self.assertEqual(report["missing"], [date(2026, 11, 9)])
        self.assertEqual(report["unexpected"], [])


if __name__ == "__main__":
    unittest.main()
