// A4 §1.9 and M3.4: IST dates, the session calendar, and dated instrument specs.
// Expected timestamps come from stockify.timeutil.ist_ns.
#include <ostream>  // doctest prints string_view through operator<<

#include <string>

#include "doctest.h"
#include "stocksim/errors.hpp"
#include "stocksim/instruments.hpp"
#include "stocksim/session.hpp"

using namespace stocksim;

namespace {

constexpr TimeNs k0915 = 1'793'677'500'000'000'000;  // 2026-11-03 09:15 IST
constexpr TimeNs k1530 = 1'793'700'000'000'000'000;  // 2026-11-03 15:30 IST
constexpr TimeNs kMidnight = 1'793'644'200'000'000'000;  // 2026-11-03 00:00 IST
constexpr TimeNs kMinute = 60 * kNsPerSecond;

Json days_json() {
    // Two days; CM_CAS has a CAS phase on the second day only.
    auto group = [](TimeNs base, bool cas) {
        Json g = Json::array();
        g.push_back({{"phase", "PREOPEN"}, {"start_ns", base - 15 * kMinute}, {"end_ns", base - 7 * kMinute}});
        g.push_back({{"phase", "CONTINUOUS"}, {"start_ns", base}, {"end_ns", base + 375 * kMinute}});
        if (cas) {
            g.push_back({{"phase", "CAS"}, {"start_ns", base + 375 * kMinute}, {"end_ns", base + 385 * kMinute}});
        }
        return g;
    };
    Json days = Json::array();
    for (int i = 0; i < 2; ++i) {
        const TimeNs base = k0915 + i * kNsPerDay;
        Json d;
        d["date"] = i == 0 ? "2026-11-03" : "2026-11-04";
        d["groups"] = {{"CM", group(base, false)}, {"CM_CAS", group(base, i == 1)}, {"FO_FUT", group(base, false)},
                       {"FO_OPT", group(base, false)}};
        d["mis_cutoff_ns"] = {{"CM", base + 365 * kMinute}, {"CM_CAS", base + 365 * kMinute}, {"FO", nullptr}};
        d["day_end_ns"] = kMidnight + (i + 1) * kNsPerDay - 1;
        days.push_back(d);
    }
    return days;
}

}  // namespace

TEST_CASE("IST dates of UTC nanoseconds") {
    CHECK(ist_date(kMidnight) == 20261103);
    CHECK(ist_date(kMidnight - 1) == 20261102);  // 18:29:59.999999999 UTC is still the 2nd in India
    CHECK(ist_date(k1530) == 20261103);
    CHECK(ist_date(0) == 19700101);
    CHECK(parse_iso_date("2026-11-03") == 20261103);
    CHECK(iso_date(20260105) == "2026-01-05");
    CHECK(next_date(20261231) == 20270101);
    CHECK(next_date(20280228) == 20280229);
    CHECK(weekday(20261103) == 1);  // Tuesday, as datetime.date.weekday()
    CHECK_THROWS_AS(parse_iso_date("2026-02-30"), FormatError);
    CHECK_THROWS_AS(parse_iso_date("2026-1-05"), FormatError);
}

TEST_CASE("IST wall times, parsed as timeutil.parse_hhmm parses them") {
    CHECK(ist_ns(20261103, "09:15") == k0915);
    CHECK(ist_ns(20261103, "9:15") == k0915);
    CHECK(ist_ns(20261103, " 9:15") == k0915);  // int(" 9") == 9
    CHECK(ist_ns(20261103, "+09:015") == k0915);
    CHECK(ist_ns(20261103, "15:30") == k1530);
    CHECK(ist_ns(20261103, "0:0") == kMidnight);
    CHECK(ist_ns(20261103, "23:59:59") == 1'793'730'599'000'000'000);
    for (const char* bad : {"24:00", "9", "9:60", "9:15:60", "09:15:00:00", "a:15", "", "-1:15", "9::15", "9:1_5_"}) {
        CAPTURE(bad);
        CHECK_THROWS_AS(ist_ns(20261103, bad), FormatError);
    }
}

TEST_CASE("phases come only from the bundle") {
    const SessionCalendar cal(days_json());
    REQUIRE(cal.days().size() == 2);
    CHECK(cal.phase_at("CM", k0915) == "CONTINUOUS");
    CHECK(cal.phase_at("CM", k0915 - 1) == "CLOSED");  // between PREOPEN's end and the open
    CHECK(cal.phase_at("CM", k0915 - 10 * kMinute) == "PREOPEN");
    CHECK(cal.phase_at("CM", k1530) == "CLOSED");  // the end is exclusive
    CHECK(cal.phase_at("CM", k0915 + 2 * kNsPerDay) == "CLOSED");  // not a bundle day
    CHECK(cal.continuous_start("CM", 20261103) == k0915);
    CHECK_FALSE(cal.continuous_start("CM", 20261105).has_value());
    CHECK(cal.day(20261104)->cutoff("CM") == k0915 + kNsPerDay + 365 * kMinute);
    CHECK_FALSE(cal.day(20261104)->cutoff("FO").has_value());
    CHECK(cal.last_day_end() == kMidnight + 2 * kNsPerDay - 1);
    auto cm = [](Date) { return std::string_view("CM"); };
    CHECK(cal.next_continuous_start(cm, k0915 + 1) == k0915 + kNsPerDay);  // after today's open: tomorrow's
    CHECK(cal.next_continuous_start(cm, k0915) == k0915);
    CHECK_FALSE(cal.next_continuous_start(cm, k0915 + kNsPerDay + 1).has_value());
    CHECK_THROWS_AS(cal.day(20261103)->group("XX"), FormatError);
}

TEST_CASE("specs are dated, and CM_CAS stocks change group by date (M3.4, G12)") {
    const SessionCalendar cal(days_json());
    Json rows = Json::array();
    rows.push_back({{"id", 7}, {"exchange", "NFO"}, {"kind", "FUT"}, {"tradingsymbol", "NIFTY26NOVFUT"},
                    {"underlying_id", 1}, {"expiry", "2026-11-24"}, {"session_group", "FO_FUT"},
                    {"specs", Json::array({{{"from", "2026-10-01"}, {"lot", 75}, {"tick_paise", 10}, {"freeze_qty", 1800}},
                                           {{"from", "2026-11-04"}, {"lot", 65}, {"tick_paise", 10}, {"freeze_qty", nullptr}}})}});
    rows.push_back({{"id", 9}, {"exchange", "NSE"}, {"kind", "EQ"}, {"tradingsymbol", "RELIANCE"},
                    {"session_group", "CM_CAS"}, {"cas_dates", Json::array({"2026-11-04"})},
                    {"specs", Json::array({{{"from", "2015-01-01"}, {"lot", 1}, {"tick_paise", 10}}})}});
    const InstrumentTable t(rows, cal);
    CHECK(t.spec_on(7, 20261103)->lot == 75);  // a bundle day: pre-resolved
    CHECK(t.spec_on(7, 20261103)->freeze_qty == 1800);
    CHECK(t.spec_on(7, 20261104)->lot == 65);  // mid-life lot change
    CHECK_FALSE(t.spec_on(7, 20261104)->freeze_qty.has_value());
    CHECK(t.spec_on(7, 20261110)->lot == 65);  // not a bundle day: scanned
    CHECK(t.spec_on(7, 20260930) == nullptr);  // before the first spec
    CHECK(t.spec_on(42, 20261103) == nullptr);
    CHECK(t.group_of(9, 20261103) == "CM");
    CHECK(t.group_of(9, 20261104) == "CM_CAS");
    CHECK(t.group_of(7, 20261104) == "FO_FUT");
    CHECK(t.at(7).last_day() == 20261124);
    CHECK(t.spec_on(9, 20261103)->row.dump() == R"({"from":"2015-01-01","lot":1,"tick_paise":10})");
}
