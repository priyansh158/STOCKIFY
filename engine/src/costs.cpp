#include "stocksim/costs.hpp"

#include <algorithm>
#include <cctype>
#include <vector>

#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"

namespace stocksim {

namespace {

constexpr std::int64_t kE18 = 1'000'000'000'000'000'000;  // E: exact charge units, 10⁻¹⁸ paisa

std::string segment_of(std::string_view kind) { return kind == "FUT" || kind == "CE" || kind == "PE" ? "FO" : "CM"; }

// round_half_up_div(v, E) for v ≥ 0.
Paise round_e(const Int128& v) {
    if (v.negative()) {
        throw NumericError("round_half_up_div needs a >= 0");
    }
    return (v + Int128(kE18 / 2)).floor_div(kE18).to_int64();
}

bool matches(const Json& applies, const std::map<std::string, std::string>& attrs) {  // costs._matches
    if (!applies.is_object()) {
        return true;
    }
    for (const auto& [k, v] : applies.items()) {
        const auto it = attrs.find(k);
        if (it == attrs.end()) {
            return false;
        }
        if (v.is_array()) {
            bool found = false;
            for (const Json& x : v) {
                if (x.is_string() && x.get<std::string>() == it->second) {
                    found = true;
                    break;
                }
            }
            if (!found) {
                return false;
            }
        } else if (!v.is_string() || v.get<std::string>() != it->second) {
            return false;
        }
    }
    return true;
}

// The canonical form of an `applies` object (sorted keys), as json.dumps(…, sort_keys=True) groups rows.
// nlohmann::json keeps objects in a std::map, so re-parsing the ordered text sorts the keys.
std::string canonical(const Json& applies) { return nlohmann::json::parse(applies.dump()).dump(); }

// costs._rate: a side-specific rate (buy/sell) or a plain one (pct/rate); 0 when the row has none for this side.
Ppb rate_of(const Json& row, Side side) {
    const char* side_key = side == Side::Buy ? "buy_ppb" : "sell_ppb";
    if (row.contains(side_key)) {
        return row.at(side_key).get<Ppb>();
    }
    if (row.contains("buy_ppb") || row.contains("sell_ppb")) {
        return 0;
    }
    if (row.contains("pct_ppb")) {
        return row.at("pct_ppb").get<Ppb>();
    }
    return row.contains("rate_ppb") ? row.at("rate_ppb").get<Ppb>() : 0;
}

struct Exact {
    Int128 brokerage, stt, exchange_txn, sebi, stamp, gst;
    Int128 get(std::string_view name) const {
        if (name == "brokerage") return brokerage;
        if (name == "stt") return stt;
        if (name == "exchange_txn") return exchange_txn;
        if (name == "sebi") return sebi;
        if (name == "stamp") return stamp;
        if (name == "gst") return gst;
        return 0;
    }
};

ChargeParts parts_of(Paise turnover, const Exact& x) {  // costs._parts
    Int128 s = 0;
    for (const Int128& v : {x.brokerage, x.stt, x.exchange_txn, x.sebi, x.stamp, x.gst}) {
        s += v;
    }
    ChargeParts p;
    p.turnover = turnover;
    p.brokerage = round_e(x.brokerage);
    p.stt = round_e(x.stt);
    p.exchange = round_e(x.exchange_txn);
    p.sebi = round_e(x.sebi);
    p.stamp = round_e(x.stamp);
    p.gst = round_e(x.gst);
    p.total = round_e(s);
    return p;
}

}  // namespace

Paise ChargeParts::row_total() const {
    if (total) {
        return *total;
    }
    Paise t = 0;
    for (Paise v : {brokerage, stt, exchange, sebi, stamp, gst, dp, other}) {
        t = checked_add(t, v);
    }
    return t;
}

Costs::Costs(const Json& charges, const Json& costs_cfg) {
    const auto it = charges.find("components");
    components_ = it != charges.end() ? *it : Json::object();
    if (costs_cfg.is_object()) {
        if (const auto d = costs_cfg.find("dp_charges"); d != costs_cfg.end() && d->is_boolean()) {
            dp_charges_ = d->get<bool>();
        }
        if (const auto a = costs_cfg.find("auto_square_off_fee"); a != costs_cfg.end() && a->is_boolean()) {
            auto_square_off_fee_ = a->get<bool>();
        }
    }
}

const Json& Costs::rows(std::string_view component) const {
    static const Json empty = Json::array();
    const auto it = components_.find(std::string(component));
    return it == components_.end() ? empty : *it;
}

const Json* Costs::row_for(std::string_view component, const Attrs& attrs, const std::string& trade_date,
                           bool required) const {  // costs.row_for
    std::vector<std::pair<std::string, const Json*>> groups;  // canonical applies → row in force
    for (const Json& r : rows(component)) {
        const std::string from = r.at("effective_from").get<std::string>();
        const Json applies = r.contains("applies") ? r.at("applies") : Json::object();
        if (from > trade_date || !matches(applies, attrs)) {
            continue;
        }
        const std::string key = canonical(applies);
        auto g = std::find_if(groups.begin(), groups.end(), [&](const auto& x) { return x.first == key; });
        if (g == groups.end()) {
            groups.emplace_back(key, &r);
        } else if (from > g->second->at("effective_from").get<std::string>()) {
            g->second = &r;
        }
    }
    if (groups.size() > 1) {
        throw RunError(std::string(component) + ": " + std::to_string(groups.size()) + " rows match on " + trade_date);
    }
    if (groups.empty()) {
        if (required) {
            throw RunError(std::string(component) + ": no row matches on " + trade_date);
        }
        return nullptr;
    }
    return groups.front().second;
}

ChargeParts Costs::order(std::string_view kind, Product product, Side side, Paise turnover,
                         const std::string& trade_date) const {  // costs.Costs.order
    const Attrs attrs{{"segment", segment_of(kind)}, {"kind", std::string(kind)},
                      {"product", std::string(to_string(product))}, {"side", std::string(to_string(side))}};
    Exact x;
    const Json& b = *row_for("brokerage", attrs, trade_date, true);
    const std::string bkind = b.at("kind").get<std::string>();
    if (bkind == "zero") {
        x.brokerage = 0;
    } else if (bkind == "flat") {
        x.brokerage = Int128::mul(b.at("flat_paise").get<Paise>(), kE18);
    } else if (bkind == "min_flat_pct") {
        const Int128 flat = Int128::mul(b.at("flat_paise").get<Paise>(), kPpb);
        const Int128 pct = Int128::mul(turnover, b.at("pct_ppb").get<Ppb>());
        x.brokerage = (flat < pct ? flat : pct) * kPpb;
    } else {
        throw RunError("brokerage kind '" + bkind + "'");
    }
    x.stt = Int128::mul(turnover, rate_of(*row_for("stt", attrs, trade_date, true), side)) * kPpb;
    x.exchange_txn = Int128::mul(turnover, rate_of(*row_for("exchange_txn", attrs, trade_date, true), side)) * kPpb;
    x.sebi = Int128::mul(turnover, rate_of(*row_for("sebi", attrs, trade_date, true), side)) * kPpb;
    const Int128 stamp_raw = Int128::mul(turnover, rate_of(*row_for("stamp", attrs, trade_date, true), side));
    const Paise stamp_rupees = (stamp_raw + Int128(50 * kPpb)).floor_div(100 * kPpb).to_int64();
    x.stamp = Int128::mul(checked_mul(stamp_rupees, 100), kE18);
    // GST on the exact (unrounded) components named by its row (D-46).
    const Json& gst = *row_for("gst", {}, trade_date, true);
    Int128 base_e9 = 0;  // paise × 10⁹
    if (const auto on = gst.find("on"); on != gst.end()) {
        for (const Json& name : *on) {
            base_e9 += x.get(name.get<std::string>()).floor_div(kPpb);
        }
    }
    x.gst = base_e9 * gst.at("rate_ppb").get<Ppb>();
    return parts_of(turnover, x);
}

Paise Costs::gst_on(const std::string& trade_date, Paise amount) const {  // costs._gst_on
    const Json& row = *row_for("gst", {}, trade_date, true);
    const Int128 v = Int128::mul(amount, row.at("rate_ppb").get<Ppb>());
    return (v + Int128(kPpb / 2)).floor_div(kPpb).to_int64();
}

std::optional<ChargeParts> Costs::dp(const std::string& trade_date) const {
    if (!dp_charges_) {
        return std::nullopt;
    }
    const Json* row = row_for("dp", {{"segment", "CM"}, {"product", "CNC"}, {"side", "SELL"}}, trade_date, false);
    if (row == nullptr) {
        return std::nullopt;
    }
    ChargeParts p;
    p.dp = row->at("per_scrip_per_day_paise").get<Paise>();
    const bool includes_gst = row->contains("includes_gst") && row->at("includes_gst").get<bool>();
    p.gst = includes_gst ? 0 : gst_on(trade_date, p.dp);
    return p;
}

std::optional<ChargeParts> Costs::sqoff_fee(const std::string& trade_date) const {
    if (!auto_square_off_fee_) {
        return std::nullopt;
    }
    const Json* row = row_for("auto_square_off_fee", {}, trade_date, false);
    if (row == nullptr) {
        return std::nullopt;
    }
    ChargeParts p;
    p.other = row->at("per_order_paise").get<Paise>();
    const bool plus_gst = row->contains("plus_gst") && row->at("plus_gst").get<bool>();
    p.gst = plus_gst ? gst_on(trade_date, p.other) : 0;
    return p;
}

ChargeParts Costs::exercise(std::string_view kind, Paise intrinsic_value, const std::string& trade_date) const {
    const Attrs attrs{{"segment", "FO"}, {"kind", std::string(kind)}};
    const Json& stt_row = *row_for("exercise_stt", attrs, trade_date, true);
    const Json* brk_row = row_for("exercise_brokerage", attrs, trade_date, false);
    const Paise brokerage =
        brk_row != nullptr && brk_row->at("kind").get<std::string>() == "flat" ? brk_row->at("flat_paise").get<Paise>() : 0;
    Exact x;
    x.brokerage = Int128::mul(brokerage, kE18);
    x.stt = Int128::mul(intrinsic_value, stt_row.at("pct_of_intrinsic_ppb").get<Ppb>()) * kPpb;
    const Json& gst = *row_for("gst", {}, trade_date, true);
    Int128 base_e9 = 0;
    if (const auto on = gst.find("on"); on != gst.end()) {
        for (const Json& name : *on) {
            base_e9 += x.get(name.get<std::string>()).floor_div(kPpb);
        }
    }
    x.gst = base_e9 * gst.at("rate_ppb").get<Ppb>();
    return parts_of(intrinsic_value, x);
}

ChargeParts Costs::delivery(std::string_view kind, Paise value, Paise netted_value, Paise exercised_intrinsic,
                            const std::string& trade_date) const {  // costs.delivery
    const Attrs attrs{{"segment", "FO"}, {"kind", std::string(kind)}};
    const Json& row = *row_for("physical_delivery", attrs, trade_date, true);
    Exact x;
    x.stt = Int128::mul(value, row.at("stt_each_side_pct_of_value_ppb").get<Ppb>()) * kPpb;
    if (exercised_intrinsic != 0) {
        const Json& ex = *row_for("exercise_stt", attrs, trade_date, true);
        x.stt += Int128::mul(exercised_intrinsic, ex.at("pct_of_intrinsic_ppb").get<Ppb>()) * kPpb;
    }
    x.brokerage = (Int128::mul(checked_sub(value, netted_value), row.at("brokerage_pct_ppb").get<Ppb>()) +
                   Int128::mul(netted_value, row.at("netted_brokerage_pct_ppb").get<Ppb>())) *
                  kPpb;
    const Json& gst = *row_for("gst", {}, trade_date, true);
    Int128 base_e9 = 0;
    if (const auto on = gst.find("on"); on != gst.end()) {
        for (const Json& name : *on) {
            base_e9 += x.get(name.get<std::string>()).floor_div(kPpb);
        }
    }
    x.gst = base_e9 * gst.at("rate_ppb").get<Ppb>();
    return parts_of(value, x);
}

}  // namespace stocksim
