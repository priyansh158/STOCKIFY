// The bundle's instruments (A1 §6.2) with their dated specs (A4 §3.2, M3.4).
//
// spec_on(id, date) is the oracle's Engine.spec: the last spec in bundle order whose `from` is on or before
// the date. It is pre-resolved for every bundle day, so a lookup on a trading day is O(1); other dates scan
// the instrument's few specs.
#pragma once

#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <vector>

#include "stocksim/json_bundle.hpp"
#include "stocksim/session.hpp"
#include "stocksim/types.hpp"

namespace stocksim {

struct Spec {
    Date from = 0;
    Qty lot = 0;
    Paise tick = 0;
    std::optional<Qty> freeze_qty;
    Json row;  // the bundle row as written (what ctx.spec() returns to a strategy)
};

struct Instrument {
    InstrumentId id = 0;
    std::string exchange;
    std::string kind;  // EQ, INDEX, FUT, CE, PE, …
    std::string tradingsymbol;
    std::optional<std::string> underlying;
    std::optional<InstrumentId> underlying_id;
    std::optional<Date> expiry;
    std::optional<Date> actual_expiry;
    std::optional<Paise> strike;
    std::optional<std::string> underlying_class;  // INDEX | STOCK
    std::optional<std::string> settlement;        // cash | physical
    std::optional<TimeNs> last_trading_ns;
    std::set<Date> cas_dates;
    std::string session_group;  // CM, CM_CAS, FO_FUT, FO_OPT
    std::vector<Spec> specs;

    bool is_derivative() const { return kind == "FUT" || kind == "CE" || kind == "PE"; }
    bool is_option() const { return kind == "CE" || kind == "PE"; }
    std::optional<Date> last_day() const { return actual_expiry ? actual_expiry : expiry; }
};

class InstrumentTable {
 public:
    InstrumentTable(const Json& rows, const SessionCalendar& calendar);

    const Instrument* find(InstrumentId id) const;
    const Instrument& at(InstrumentId id) const;  // FormatError for an id not in the bundle
    bool contains(InstrumentId id) const { return find(id) != nullptr; }
    const std::vector<Instrument>& rows() const { return rows_; }  // bundle order

    const Spec* spec_on(InstrumentId id, Date d) const;
    // CM_CAS stocks are in CM_CAS only on their CAS dates (A4 §4.8); every other group is fixed.
    std::string_view group_of(InstrumentId id, Date d) const;

 private:
    std::vector<Instrument> rows_;
    std::map<InstrumentId, std::size_t> index_;
    std::vector<Date> day_dates_;                       // the bundle's trading days
    std::vector<std::vector<int>> spec_by_day_;         // [row][day] → spec index or -1
};

}  // namespace stocksim
