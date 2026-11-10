#include "stocksim/margin.hpp"

#include <algorithm>

#include "stocksim/decimal.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/session.hpp"

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
      mis_rows_(list_or_empty(require(root, "broker_policy", "bundle"), "mis_margin_pct")),
      addon_(list_or_empty(require(root, "margin_params", "bundle"), "expiry_day_short_index_option_addon")),
      schedule_(list_or_empty(require(root, "broker_policy", "bundle"), "delivery_margin_schedule")) {
    // the bundle's days with its prev_days and next_days (VOL 5.2, A1 §6.2): a one-day paper bundle counts trading
    // days (D-60) as the whole run does
    for (const char* key : {"prev_days", "days", "next_days"}) {
        const auto rows = root.find(key);
        if (rows == root.end() || !rows->is_array()) {
            continue;
        }
        for (const Json& d : *rows) {
            days_.push_back(d.at("date").get<std::string>());
        }
    }
}

Ppb FlatMargin::addon(const std::string& date) const {  // margin.FlatMargin._addon
    const Json* r = row_in_force(addon_, date, {});
    return r == nullptr ? 0 : r->at("pct_ppb").get<Ppb>();
}

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
    Int128 raw = 0;  // paise × 10¹⁸
    for (const MarginLeg& leg : legs) {
        if (leg.units == 0) {
            continue;
        }
        const Instrument& row = instruments.at(leg.instrument);
        if (leg.product != Product::NRML) {
            continue;
        }
        std::optional<Paise> u;  // the underlying's price
        if (row.underlying_id && *row.underlying_id != 0) {
            u = price(*row.underlying_id);
        }
        const std::optional<Paise> spot = u && *u != 0 ? u : std::nullopt;
        Paise ref = 0;
        if (row.kind == "FUT") {
            ref = price(leg.instrument).value_or(0);
        } else if (row.is_option() && leg.units < 0) {
            // (price(underlying) if underlying_id else None) or strike or 0; a 0 price falls through like None.
            ref = spot ? *spot : row.strike.value_or(0);
        } else if (row.is_option()) {
            raw += long_option_delivery(row, leg.units, spot, date);  // long options: premium, or Z-4
            continue;
        } else {
            continue;
        }
        const Ppb pct = rate(flat_, row.underlying_class == std::optional<std::string>("INDEX") ? "index_pct" : "stock_pct",
                             date, {});
        const Qty units = leg.units < 0 ? -leg.units : leg.units;
        Int128 normal = Int128::mul(checked_mul(units, ref), pct) * kPpb;
        if (row.is_option() && row.underlying_class == std::optional<std::string>("INDEX") && row.last_day() &&
            iso_date(*row.last_day()) == date) {
            normal += Int128::mul(checked_mul(units, ref), addon(date)) * kPpb;  // expiry-day ELM, underlying × qty
        }
        raw += short_delivery(row, leg.units, spot, date, normal);
    }
    return ceil_to(raw.ceil_div(kPpb * kPpb).to_int64(), 100);  // 10¹⁸ fits in int64
}

std::optional<int> FlatMargin::offset(const Instrument& row, const std::string& date) const {  // margin._offset
    if (row.settlement != std::optional<std::string>("physical") || !row.last_day()) {
        return std::nullopt;
    }
    const std::string expiry = iso_date(*row.last_day());
    const auto a = std::find(days_.begin(), days_.end(), date);
    const auto b = std::find(days_.begin(), days_.end(), expiry);
    if (a == days_.end() || b == days_.end()) {
        return std::nullopt;
    }
    return static_cast<int>((a - days_.begin()) - (b - days_.begin()));
}

const Json* FlatMargin::schedule_row(const std::string& date, int off, std::string_view applies_to) const {  // _rows
    const Json* best = row_in_force(schedule_, date, {});
    if (best == nullptr) {
        return nullptr;
    }
    const auto rows = best->find("rows");
    if (rows == best->end()) {
        return nullptr;
    }
    for (const Json& x : *rows) {
        if (x.at("day_offset").get<int>() == off && x.at("applies_to").get<std::string>() == applies_to) {
            return &x;
        }
    }
    return nullptr;
}

Int128 FlatMargin::short_delivery(const Instrument& row, Qty units, const std::optional<Paise>& spot,
                                  const std::string& date, const Int128& normal) const {  // margin._short_delivery
    if (offset(row, date) != std::optional<int>(0)) {
        return normal;
    }
    const Json* x = schedule_row(date, 0, "futures_and_short_options");
    if (!spot || x == nullptr) {
        return normal;  // D-60: no stock price, the normal margin
    }
    const Qty u = units < 0 ? -units : units;
    const Int128 by_value = Int128::mul(checked_mul(u, *spot), to_ppb(x->at("pct").get<std::string>())) * kPpb;
    const Int128 by_nrml = (normal * to_ppb(x->at("nrml_multiple").get<std::string>())).floor_div(kPpb);
    return by_value < by_nrml ? by_value : by_nrml;
}

Int128 FlatMargin::long_option_delivery(const Instrument& row, Qty units, const std::optional<Paise>& spot,
                                        const std::string& date) const {  // margin._long_option_delivery
    const std::optional<int> off = offset(row, date);
    if (!off || !spot) {
        return 0;
    }
    const Qty u = units < 0 ? -units : units;
    const Paise cv = checked_mul(u, *spot);
    const Paise k = row.strike.value_or(0);
    const bool itm = row.kind == "CE" ? *spot > k : *spot < k;
    const Json* x = nullptr;
    if (itm) {
        x = schedule_row(date, *off, "long_itm_options");
    } else {
        const Paise gap = *spot > k ? *spot - k : k - *spot;
        const bool near = *spot >= 10'000 ? checked_mul(gap, 100) <= *spot : gap <= 100;  // 1 % or ₹1 (Z-4)
        if (near) {
            x = schedule_row(date, *off, "long_otm_near_spot");
        }
    }
    if (x == nullptr) {
        return 0;
    }
    const Ppb pct = to_ppb(x->at("pct").get<std::string>());
    if (x->at("basis").get<std::string>() == "var_elm_adhoc") {
        used_labels_.insert("delivery_margin_approx");
        return Int128::mul(cv, rate(flat_, "stock_pct", date, {})) * pct;
    }
    return Int128::mul(cv, pct) * kPpb;
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
