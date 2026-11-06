#include "stocksim/margin.hpp"

#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"

namespace stocksim {

const Json* row_in_force(const Json& rows, const std::string& date,
                         const std::vector<std::pair<std::string, std::string>>& applies) {
    const Json* best = nullptr;
    if (!rows.is_array()) {
        return nullptr;
    }
    for (const Json& r : rows) {
        const std::string from = r.at("effective_from").get<std::string>();
        if (from > date) {
            continue;
        }
        bool ok = true;
        for (const auto& [k, v] : applies) {
            const auto a = r.find("applies");
            if (a == r.end() || !a->is_object()) {
                ok = false;
                break;
            }
            const auto x = a->find(k);
            if (x == a->end() || !x->is_string() || x->get<std::string>() != v) {
                ok = false;
                break;
            }
        }
        if (ok && (best == nullptr || from > best->at("effective_from").get<std::string>())) {
            best = &r;
        }
    }
    return best;
}

namespace {

const Json& list_or_empty(const Json& obj, const char* key) {
    static const Json empty = Json::array();
    const auto it = obj.find(key);
    return it == obj.end() ? empty : *it;
}

}  // namespace

FlatMargin::FlatMargin(const Json& root)
    : flat_(list_or_empty(require(root, "margin_params", "bundle"), "flat")),
      mis_rows_(list_or_empty(require(root, "broker_policy", "bundle"), "mis_margin_pct")) {}

Ppb FlatMargin::rate(const Json& rows, std::string_view key, const std::string& date,
                     const std::vector<std::pair<std::string, std::string>>& applies) const {
    const Json* r = row_in_force(rows, date, applies);
    if (r == nullptr) {
        throw RunError("no margin row for " + std::string(key) + " on " + date);
    }
    return r->at(std::string(key) + "_ppb").get<Ppb>();
}

Ppb FlatMargin::mis_rate(std::string_view segment, const std::string& date) const {
    return rate(mis_rows_, "pct", date, {{"segment", std::string(segment)}});
}

Paise FlatMargin::requirement(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                              const std::function<std::optional<Paise>(InstrumentId)>& price,
                              const std::string& date) const {  // margin.FlatMargin.requirement
    Int128 raw = 0;  // paise × ppb
    for (const MarginLeg& leg : legs) {
        if (leg.units == 0) {
            continue;
        }
        const Instrument& row = instruments.at(leg.instrument);
        if (leg.product != Product::NRML) {
            continue;
        }
        Paise ref = 0;
        if (row.kind == "FUT") {
            ref = price(leg.instrument).value_or(0);
        } else if (row.is_option() && leg.units < 0) {
            // (price(underlying) if underlying_id else None) or strike or 0; a 0 price falls through like None.
            std::optional<Paise> u;
            if (row.underlying_id && *row.underlying_id != 0) {
                u = price(*row.underlying_id);
            }
            ref = u && *u != 0 ? *u : row.strike.value_or(0);
        } else {
            continue;
        }
        const Ppb pct = rate(flat_, row.underlying_class == std::optional<std::string>("INDEX") ? "index_pct" : "stock_pct",
                             date, {});
        const Qty units = leg.units < 0 ? -leg.units : leg.units;
        raw += Int128::mul(checked_mul(units, ref), pct);
    }
    return ceil_to(raw.ceil_div(kPpb).to_int64(), 100);
}

Paise FlatMargin::mis(std::string_view kind, Qty units, Paise price, const std::string& date) const {
    const bool deriv = kind == "FUT" || kind == "CE" || kind == "PE";
    const Ppb r = mis_rate(deriv ? "FO" : "CM", date);
    const Qty u = units < 0 ? -units : units;
    const Int128 x = Int128::mul(checked_mul(u, price), r);
    if (x.negative()) {
        throw NumericError("round_half_up_div needs a >= 0");
    }
    return (x + Int128(kPpb / 2)).floor_div(kPpb).to_int64();
}

}  // namespace stocksim
