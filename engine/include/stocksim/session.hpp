// Dates in IST and the session calendar of a bundle (A4 §1.9, A1 §6.2).
//
// Time is UTC epoch ns everywhere; IST (UTC+05:30, no daylight saving, the only offset Asia/Kolkata has had
// since 1945) is used only to name trading dates. Phases come only from the bundle: any time not inside a
// listed phase is CLOSED.
#pragma once

#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "stocksim/json_bundle.hpp"
#include "stocksim/types.hpp"

namespace stocksim {

inline constexpr TimeNs kIstOffsetNs = 19'800LL * kNsPerSecond;
inline constexpr TimeNs kNsPerDay = 86'400LL * kNsPerSecond;

std::int64_t days_from_civil(int y, int m, int d);  // days since 1970-01-01
Date civil_from_days(std::int64_t days);            // YYYYMMDD
Date ist_date(TimeNs t);                            // the IST calendar date of t
Date parse_iso_date(std::string_view text);         // "2026-11-03" → 20261103; FormatError otherwise
std::string iso_date(Date d);                       // 20261103 → "2026-11-03"
Date next_date(Date d);                             // the calendar day after d
int weekday(Date d);                                // Monday 0 … Sunday 6
TimeNs ist_ns(Date d, std::string_view hhmm);       // "09:15" / "23:59:59" IST on d → UTC ns (timeutil.ist_ns)

// The MIS cut-off an instrument's session group follows (A4 §6.3; runner._cutoff_key): FO for derivatives,
// CM_CAS for a CAS stock on its CAS day (Z-5), CM otherwise.
inline std::string_view cutoff_key(std::string_view group) {
    if (group.substr(0, 2) == "FO") {
        return "FO";
    }
    return group == "CM_CAS" ? "CM_CAS" : "CM";
}

struct PhaseSpan {
    std::string phase;
    TimeNs start_ns = 0;
    TimeNs end_ns = 0;
};

struct DaySchedule {
    Date date = 0;
    std::vector<std::pair<std::string, std::vector<PhaseSpan>>> groups;       // bundle order
    std::vector<std::pair<std::string, std::optional<TimeNs>>> mis_cutoff;    // bundle order; null → nullopt
    TimeNs day_end_ns = 0;

    const std::vector<PhaseSpan>& group(std::string_view g) const;  // FormatError for an unknown group
    std::optional<TimeNs> cutoff(std::string_view segment) const;
};

class SessionCalendar {
 public:
    explicit SessionCalendar(const Json& days);

    const std::vector<DaySchedule>& days() const { return days_; }
    const DaySchedule* day(Date d) const;  // nullptr when d is not a bundle day

    std::string_view phase_at(std::string_view group, TimeNs t) const;
    std::optional<TimeNs> continuous_start(std::string_view group, Date d) const;  // earliest CONTINUOUS start
    // The first CONTINUOUS start at or after t, scanning the days from the first whose day end is at or after t;
    // group_of_day names the instrument's group on each day (CM_CAS stocks change group by date).
    std::optional<TimeNs> next_continuous_start(const std::function<std::string_view(Date)>& group_of_day,
                                                TimeNs t) const;
    TimeNs last_day_end() const { return days_.empty() ? 0 : days_.back().day_end_ns; }

 private:
    std::vector<DaySchedule> days_;
};

}  // namespace stocksim
