#include "stocksim/instruments.hpp"

#include <algorithm>

#include "stocksim/errors.hpp"

namespace stocksim {

namespace {

std::optional<std::string> opt_string(const Json& row, const char* key) {
    const auto it = row.find(key);
    if (it == row.end() || it->is_null()) {
        return std::nullopt;
    }
    return it->get<std::string>();
}

std::optional<std::int64_t> opt_int(const Json& row, const char* key) {
    const auto it = row.find(key);
    if (it == row.end() || it->is_null()) {
        return std::nullopt;
    }
    return it->get<std::int64_t>();
}

std::optional<Date> opt_date(const Json& row, const char* key) {
    const auto s = opt_string(row, key);
    return s ? std::optional<Date>(parse_iso_date(*s)) : std::nullopt;
}

int resolve_spec(const std::vector<Spec>& specs, Date d) {
    int best = -1;
    for (std::size_t i = 0; i < specs.size(); ++i) {
        if (specs[i].from <= d) {
            best = static_cast<int>(i);  // the last match in bundle order, as in the oracle
        }
    }
    return best;
}

}  // namespace

InstrumentTable::InstrumentTable(const Json& rows, const SessionCalendar& calendar) {
    if (!rows.is_array()) {
        throw FormatError("bundle: instruments is not a list");
    }
    for (std::size_t i = 0; i < rows.size(); ++i) {
        const Json& r = rows[i];
        const std::string where = "instruments[" + std::to_string(i) + "]";
        Instrument inst;
        inst.id = static_cast<InstrumentId>(require_int(r, "id", where));
        inst.exchange = opt_string(r, "exchange").value_or("");
        inst.kind = require_string(r, "kind", where);
        inst.tradingsymbol = opt_string(r, "tradingsymbol").value_or("");
        inst.underlying = opt_string(r, "underlying");
        if (const auto u = opt_int(r, "underlying_id")) {
            inst.underlying_id = static_cast<InstrumentId>(*u);
        }
        inst.expiry = opt_date(r, "expiry");
        inst.actual_expiry = opt_date(r, "actual_expiry");
        inst.strike = opt_int(r, "strike_paise");
        inst.underlying_class = opt_string(r, "underlying_class");
        inst.settlement = opt_string(r, "settlement");
        inst.last_trading_ns = opt_int(r, "last_trading_ns");
        if (const auto it = r.find("cas_dates"); it != r.end() && it->is_array()) {
            for (const Json& d : *it) {
                inst.cas_dates.insert(parse_iso_date(d.get<std::string>()));
            }
        }
        inst.session_group = require_string(r, "session_group", where);
        if (const auto it = r.find("specs"); it != r.end() && it->is_array()) {
            for (const Json& s : *it) {
                Spec spec;
                spec.from = parse_iso_date(require_string(s, "from", where + ".specs"));
                spec.lot = require_int(s, "lot", where + ".specs");
                spec.tick = require_int(s, "tick_paise", where + ".specs");
                spec.freeze_qty = opt_int(s, "freeze_qty");
                spec.row = s;
                inst.specs.push_back(std::move(spec));
            }
        }
        if (index_.count(inst.id) != 0) {
            throw FormatError("bundle: instrument " + std::to_string(inst.id) + " is listed twice");
        }
        index_[inst.id] = rows_.size();
        rows_.push_back(std::move(inst));
    }
    for (const DaySchedule& d : calendar.days()) {
        day_dates_.push_back(d.date);
    }
    spec_by_day_.resize(rows_.size());
    for (std::size_t i = 0; i < rows_.size(); ++i) {
        spec_by_day_[i].reserve(day_dates_.size());
        for (Date d : day_dates_) {
            spec_by_day_[i].push_back(resolve_spec(rows_[i].specs, d));
        }
    }
}

const Instrument* InstrumentTable::find(InstrumentId id) const {
    const auto it = index_.find(id);
    return it == index_.end() ? nullptr : &rows_[it->second];
}

const Instrument& InstrumentTable::at(InstrumentId id) const {
    const Instrument* i = find(id);
    if (i == nullptr) {
        throw FormatError("instrument " + std::to_string(id) + " is not in the bundle");
    }
    return *i;
}

const Spec* InstrumentTable::spec_on(InstrumentId id, Date d) const {
    const auto it = index_.find(id);
    if (it == index_.end()) {
        return nullptr;
    }
    const Instrument& inst = rows_[it->second];
    const auto day = std::lower_bound(day_dates_.begin(), day_dates_.end(), d);
    int k = 0;
    if (day != day_dates_.end() && *day == d) {
        k = spec_by_day_[it->second][static_cast<std::size_t>(day - day_dates_.begin())];
    } else {
        k = resolve_spec(inst.specs, d);
    }
    return k < 0 ? nullptr : &inst.specs[static_cast<std::size_t>(k)];
}

std::string_view InstrumentTable::group_of(InstrumentId id, Date d) const {
    const Instrument& inst = at(id);
    if (inst.session_group == "CM_CAS") {
        return inst.cas_dates.count(d) != 0 ? std::string_view("CM_CAS") : std::string_view("CM");
    }
    return inst.session_group;
}

}  // namespace stocksim
