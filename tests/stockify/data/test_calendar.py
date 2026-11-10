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

    def test_holiday_list_covers_2009_to_2026(self):
        years = {r["date"][:4] for r in cal.loader.load_csv("calendar/nse_holidays.csv")}
        self.assertEqual(years, {str(y) for y in range(2009, 2027)})

    def test_trading_days_and_neighbours(self):
        days = cal.trading_days("FO", date(2026, 11, 6), date(2026, 11, 12))
        self.assertEqual(days, [date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 11), date(2026, 11, 12)])
        self.assertEqual(cal.previous_trading_day("FO", date(2026, 11, 11)), date(2026, 11, 9))
        self.assertEqual(cal.next_trading_day("FO", date(2026, 11, 6)), date(2026, 11, 9))


class ShippedHolidayListTests(unittest.TestCase):
    """The 2009-2023 rows (D-75): a weekday with no NSE data in the lab is a holiday; NSE's own lists only name it."""

    def setUp(self):
        cal.reload()
        self.rows = cal.loader.load_csv("calendar/nse_holidays.csv")
        self.old = [r for r in self.rows if r["date"] < "2024-01-01"]

    def test_every_year_2009_to_2023_has_at_least_8_weekday_holidays(self):
        for year in range(2009, 2024):
            with self.subTest(year=year):
                days = [r for r in self.old
                        if r["date"].startswith(str(year)) and date.fromisoformat(r["date"]).weekday() < 5]
                self.assertGreaterEqual(len(days), 8)

    def test_weekdays_without_a_span_file_are_not_trading_days(self):
        # Each of these weekdays has no nsccl.<date>.s.zip in the lab's complete SPAN archive (checked 2026-11-09).
        for day in (date(2009, 4, 30),      # Parliamentary elections in Mumbai
                    date(2012, 8, 15),      # Independence Day
                    date(2014, 4, 24),      # General elections
                    date(2017, 8, 25),      # Ganesh Chaturthi
                    date(2020, 5, 25),      # Id-Ul-Fitr
                    date(2023, 12, 25)):    # Christmas
            for segment in ("CM", "FO"):
                with self.subTest(day=day, segment=segment):
                    self.assertFalse(cal.is_trading_day(segment, day))

    def test_a_muhurat_only_day_is_a_holiday(self):
        # NSE/CMTR/50560 lists 2022-10-24 as a trading holiday with Muhurat trading only; the owner's standing
        # decision is no special sessions, so the day is closed although a .s file exists for it.
        self.assertFalse(cal.is_trading_day("FO", date(2022, 10, 24)))
        row = next(r for r in self.old if r["date"] == "2022-10-24")
        self.assertIn("Muhurat session only", row["description"])

    def test_an_archive_gap_is_not_a_holiday(self):
        # 2021-10-11 has no .s file because NSE served a non-zip body, and no NSE list calls it a holiday.
        self.assertTrue(cal.is_trading_day("FO", date(2021, 10, 11)))

    def test_the_neighbours_of_a_holiday_stay_trading_days(self):
        self.assertEqual(cal.trading_days("FO", date(2009, 4, 29), date(2009, 5, 4)),
                         [date(2009, 4, 29), date(2009, 5, 4)])      # 30 Apr election, 1 May, a weekend

    def test_pre_2024_rows_are_weekdays_for_both_segments(self):
        self.assertTrue(self.old)
        for r in self.old:
            with self.subTest(row=r["date"]):
                self.assertLess(date.fromisoformat(r["date"]).weekday(), 5)
                self.assertEqual(r["segments"], "CM;FO")

    def test_dates_are_unique(self):
        dates = [r["date"] for r in self.rows]
        self.assertEqual(len(dates), len(set(dates)))

    def test_every_row_has_a_description_and_a_source(self):
        for r in self.rows:
            with self.subTest(row=r["date"]):
                self.assertTrue(r["description"].strip())
                self.assertTrue(r["source"].strip())

    def test_rows_are_sorted_by_date(self):
        dates = [r["date"] for r in self.rows]
        self.assertEqual(dates, sorted(dates))


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


class OwnerViewTests(unittest.TestCase):
    """The owner trades from Seattle: IST trade dates shown in local time."""

    def setUp(self):
        cal.reload()

    def local(self, t_ns):
        return timeutil.ns_to_ist(t_ns).astimezone(cal.owner_tz())

    def test_monday_9_nov_session_starts_sunday_evening_in_seattle(self):
        rows = {r["date"]: r for r in cal.upcoming(date(2026, 11, 7), 5)}
        self.assertFalse(rows[date(2026, 11, 8)]["open"])
        self.assertIn("Diwali", rows[date(2026, 11, 8)]["reason"])
        mon = rows[date(2026, 11, 9)]
        self.assertEqual(self.local(mon["session_start_ns"]).strftime("%a %d %H:%M"), "Sun 08 19:30")
        self.assertEqual(self.local(mon["login_from_ns"]).strftime("%a %d %H:%M"), "Sun 08 16:30")
        self.assertEqual(self.local(mon["session_end_ns"]).strftime("%a %d %H:%M"), "Mon 09 02:10")
        self.assertFalse(rows[date(2026, 11, 10)]["open"])

    def test_us_daylight_saving_shifts_local_times_only(self):
        before = cal.upcoming(date(2027, 3, 12), 1)[0]     # Fri, PST: IST - 13:30
        after = cal.upcoming(date(2027, 3, 15), 1)[0]      # Mon, PDT: IST - 12:30
        self.assertEqual(self.local(before["session_start_ns"]).strftime("%H:%M"), "19:30")
        self.assertEqual(self.local(after["session_start_ns"]).strftime("%H:%M"), "20:30")
        for row in (before, after):
            self.assertEqual(timeutil.ns_to_ist(row["session_start_ns"]).strftime("%H:%M"), "09:00")

    def test_holiday_coverage_end_is_known(self):
        self.assertEqual(cal.holidays_cover_until(), date(2026, 12, 31))


if __name__ == "__main__":
    unittest.main()
