#include "stocksim/session.hpp"

#include <algorithm>
#include <cstdio>

#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"

namespace stocksim {

// Howard Hinnant's civil-date algorithms (proleptic Gregorian).
std::int64_t days_from_civil(int y, int m, int d) {
    y -= m <= 2 ? 1 : 0;
    const std::int64_t era = (y >= 0 ? y : y - 399) / 400;
    const auto yoe = static_cast<unsigned>(y - era * 400);
    const auto mp = static_cast<unsigned>(m + (m > 2 ? -3 : 9));
    const unsigned doy = (153 * mp + 2) / 5 + static_cast<unsigned>(d) - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + static_cast<std::int64_t>(doe) - 719468;
}

Date civil_from_days(std::int64_t z) {
    z += 719468;
    const std::int64_t era = (z >= 0 ? z : z - 146096) / 146097;
    const auto doe = static_cast<unsigned>(z - era * 146097);
    const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const std::int64_t y0 = static_cast<std::int64_t>(yoe) + era * 400;
    const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const unsigned mp = (5 * doy + 2) / 153;
    const unsigned d = doy - (153 * mp + 2) / 5 + 1;
    const unsigned m = mp < 10 ? mp + 3 : mp - 9;
    const std::int64_t y = y0 + (m <= 2 ? 1 : 0);
    return static_cast<Date>(y * 10000 + m * 100 + d);
}

Date ist_date(TimeNs t) { return civil_from_days(floor_div(checked_add(t, kIstOffsetNs), kNsPerDay)); }

Date parse_iso_date(std::string_view s) {
    auto digit = [&](std::size_t i) { return s[i] >= '0' && s[i] <= '9'; };
    if (s.size() != 10 || s[4] != '-' || s[7] != '-' || !digit(0) || !digit(1) || !digit(2) || !digit(3) ||
        !digit(5) || !digit(6) || !digit(8) || !digit(9)) {
        throw FormatError("not an ISO date: '" + std::string(s) + "'");
    }
    auto num = [&](std::size_t a, std::size_t n) {
        int v = 0;
        for (std::size_t i = a; i < a + n; ++i) {
            v = v * 10 + (s[i] - '0');
        }
        return v;
    };
    const int y = num(0, 4), m = num(5, 2), d = num(8, 2);
    if (m < 1 || m > 12 || d < 1 || civil_from_days(days_from_civil(y, m, d)) != y * 10000 + m * 100 + d) {
        throw FormatError("not a calendar date: '" + std::string(s) + "'");
    }
    return y * 10000 + m * 100 + d;
}

std::string iso_date(Date d) {
    char buf[16];
    std::snprintf(buf, sizeof buf, "%04d-%02d-%02d", d / 10000, d / 100 % 100, d % 100);
    return buf;
}

Date next_date(Date d) { return civil_from_days(days_from_civil(d / 10000, d / 100 % 100, d % 100) + 1); }

int weekday(Date d) {
    const std::int64_t days = days_from_civil(d / 10000, d / 100 % 100, d % 100);  // 1970-01-01 was a Thursday
    return static_cast<int>(floor_mod(days + 3, 7));
}

const std::vector<PhaseSpan>& DaySchedule::group(std::string_view g) const {
    for (const auto& [name, phases] : groups) {
        if (name == g) {
            return phases;
        }
    }
    throw FormatError("bundle day " + iso_date(date) + " has no session group " + std::string(g));
}

std::optional<TimeNs> DaySchedule::cutoff(std::string_view segment) const {
    for (const auto& [name, t] : mis_cutoff) {
        if (name == segment) {
            return t;
        }
    }
    return std::nullopt;
}

SessionCalendar::SessionCalendar(const Json& days) {
    if (!days.is_array()) {
        throw FormatError("bundle: days is not a list");
    }
    for (std::size_t i = 0; i < days.size(); ++i) {
        const Json& d = days[i];
        const std::string where = "days[" + std::to_string(i) + "]";
        DaySchedule day;
        day.date = parse_iso_date(require_string(d, "date", where));
        for (const auto& [g, phases] : require(d, "groups", where).items()) {
            std::vector<PhaseSpan> spans;
            for (const Json& p : phases) {
                spans.push_back(PhaseSpan{require_string(p, "phase", where + ".groups." + g),
                                          require_int(p, "start_ns", where + ".groups." + g),
                                          require_int(p, "end_ns", where + ".groups." + g)});
            }
            day.groups.emplace_back(g, std::move(spans));
        }
        if (const auto it = d.find("mis_cutoff_ns"); it != d.end() && it->is_object()) {
            for (const auto& [seg, t] : it->items()) {
                day.mis_cutoff.emplace_back(seg, t.is_null() ? std::nullopt : std::optional<TimeNs>(t.get<TimeNs>()));
            }
        }
        day.day_end_ns = require_int(d, "day_end_ns", where);
        if (!days_.empty() && day.date <= days_.back().date) {
            throw FormatError("bundle: days are not in increasing date order at " + where);
        }
        days_.push_back(std::move(day));
    }
}

const DaySchedule* SessionCalendar::day(Date d) const {
    const auto it = std::lower_bound(days_.begin(), days_.end(), d,
                                     [](const DaySchedule& s, Date x) { return s.date < x; });
    return it != days_.end() && it->date == d ? &*it : nullptr;
}

std::string_view SessionCalendar::phase_at(std::string_view group, TimeNs t) const {
    const DaySchedule* d = day(ist_date(t));
    if (d == nullptr) {
        return "CLOSED";
    }
    for (const PhaseSpan& p : d->group(group)) {
        if (p.start_ns <= t && t < p.end_ns) {
            return p.phase;
        }
    }
    return "CLOSED";
}

std::optional<TimeNs> SessionCalendar::continuous_start(std::string_view group, Date d) const {
    const DaySchedule* s = day(d);
    if (s == nullptr) {
        return std::nullopt;
    }
    std::optional<TimeNs> best;
    for (const PhaseSpan& p : s->group(group)) {
        if (p.phase == "CONTINUOUS" && (!best || p.start_ns < *best)) {
            best = p.start_ns;
        }
    }
    return best;
}

std::optional<TimeNs> SessionCalendar::next_continuous_start(
    const std::function<std::string_view(Date)>& group_of_day, TimeNs t) const {
    auto it = std::lower_bound(days_.begin(), days_.end(), t,
                               [](const DaySchedule& s, TimeNs x) { return s.day_end_ns < x; });
    for (; it != days_.end(); ++it) {
        for (const PhaseSpan& p : it->group(group_of_day(it->date))) {
            if (p.phase == "CONTINUOUS" && p.start_ns >= t) {
                return p.start_ns;
            }
        }
    }
    return std::nullopt;
}

}  // namespace stocksim
