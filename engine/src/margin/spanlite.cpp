// T1 SPAN-lite margin (M4.8, C-12): see spanlite.hpp. Mirrors stockify/fno/spanlite.py step by step.
#include "stocksim/margin/spanlite.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/pricing/black76.hpp"
#include "stocksim/session.hpp"

namespace stocksim {

namespace {

constexpr double kPpb = 1'000'000'000.0;
constexpr std::int64_t kPpbInt = 1'000'000'000;
constexpr double kMoves[] = {0.0, 1.0 / 3, -1.0 / 3, 2.0 / 3, -2.0 / 3, 1.0, -1.0};

// paise × 10⁹ (as Int128) → paise, rounded up to the whole rupee.
Paise rupee_up(const Int128& paise_ppb) {
    const Int128 paise = paise_ppb.ceil_div(kPpbInt);
    return ceil_to(paise.to_int64(), 100);
}

double ppb_field(const Json& row, const char* key) {
    return static_cast<double>(row.at(std::string(key) + "_ppb").get<std::int64_t>()) / kPpb;
}

}  // namespace

SpanLiteMargin::SpanLiteMargin(const Json& bundle_root, SpanLiteHost host)
    : flat_(bundle_root), host_(std::move(host)) {
    const Json& params = require(bundle_root, "margin_params", "bundle");
    rows_ = params.contains("spanlite") && params.at("spanlite").is_array() ? params.at("spanlite") : Json::array();
    addon_rows_ = params.contains("expiry_day_short_index_option_addon") &&
                          params.at("expiry_day_short_index_option_addon").is_array()
                      ? params.at("expiry_day_short_index_option_addon")
                      : Json::array();
}

const Json& SpanLiteMargin::params(const Instrument& row, const std::string& date) const {  // spanlite.params
    const Json* found = nullptr;
    if (row.underlying) {
        found = row_in_force(rows_, date, {{"underlying", *row.underlying}});
    }
    if (found == nullptr) {
        found = row_in_force(rows_, date, {{"underlying_class", row.underlying_class.value_or("INDEX")}});
    }
    if (found == nullptr) {
        throw RunError("no spanlite margin row for " + row.underlying.value_or("?") + " on " + date);
    }
    return *found;
}

double SpanLiteMargin::iv(const Instrument& row, const Context::ChainView* chain, const std::string& date) const {
    if (chain != nullptr) {
        for (const auto& r : chain->rows) {
            if (r.instrument == row.id) {
                if (r.iv_mid) {
                    last_iv_[row.id] = {date, *r.iv_mid};
                    return *r.iv_mid;
                }
                break;
            }
        }
    }
    const auto last = last_iv_.find(row.id);
    if (last != last_iv_.end() && last->second.first == date) {
        return last->second.second;
    }
    host_.add_label(kIvFallbackLabel);
    if (chain != nullptr && chain->atm_strike) {
        for (const auto& r : chain->rows) {  // CE before PE at a strike: the call's when both have one
            if (r.strike == *chain->atm_strike && r.iv_mid) {
                return *r.iv_mid;
            }
        }
    }
    return ppb_field(params(row, date), "default_iv");
}

std::vector<UnderlyingMargin> SpanLiteMargin::breakdown(const std::vector<MarginLeg>& legs,
                                                        const InstrumentTable& instruments,
                                                        const std::function<std::optional<Paise>(InstrumentId)>& price,
                                                        const std::string& date) const {
    std::map<InstrumentId, std::vector<std::pair<const Instrument*, Qty>>> by_underlying;
    for (const MarginLeg& leg : legs) {
        const Instrument& row = instruments.at(leg.instrument);
        if (leg.units == 0 || leg.product != Product::NRML || !row.is_derivative() || !row.underlying_id ||
            *row.underlying_id == 0) {
            continue;
        }
        by_underlying[*row.underlying_id].emplace_back(&row, leg.units);
    }
    std::vector<UnderlyingMargin> out;
    for (const auto& [uid, group] : by_underlying) {
        out.push_back(one(uid, group, price, date));
    }
    return out;
}

UnderlyingMargin SpanLiteMargin::one(InstrumentId uid, const std::vector<std::pair<const Instrument*, Qty>>& legs,
                                     const std::function<std::optional<Paise>(InstrumentId)>& price,
                                     const std::string& date) const {
    const Json& p = params(*legs.front().first, date);
    const double psr = ppb_field(p, "psr_pct");
    const double psr_fut = p.contains("psr_futures_pct_ppb") ? ppb_field(p, "psr_futures_pct") : psr;  // D-62
    const double vsr = ppb_field(p, "vsr_pct");
    const double ext_mult = static_cast<double>(p.at("extreme_multiple").get<std::int64_t>());
    const double ext_weight = ppb_field(p, "extreme_weight");
    const std::int64_t elm_ppb = p.at("elm_pct_ppb").get<std::int64_t>();
    const std::optional<Paise> und = price(uid);
    const double rate = host_.rate(date);
    const Date day = parse_iso_date(date);
    const TimeNs now = host_.now();

    struct Valued {
        Qty units;
        bool future;
        double f, k, t, sigma;
        bool call;
    };
    std::vector<Valued> valued;
    std::int64_t somc_lots = 0;
    Int128 elm_raw = 0, addon_raw = 0;
    const Json* addon = row_in_force(addon_rows_, date);
    for (const auto& [row, units] : legs) {
        const Qty size = units < 0 ? -units : units;
        if (row->kind == "FUT") {
            const std::optional<Paise> own = price(row->id);
            const std::optional<Paise> f = own && *own != 0 ? own : und;  // `price(iid) or underlying`
            if (!f) {
                continue;  // nothing to value it at yet
            }
            valued.push_back(Valued{units, true, static_cast<double>(*f) / 100.0, 0.0, 0.0, 0.0, false});
            elm_raw += Int128::mul(checked_mul(elm_ppb, *f), size);
            continue;
        }
        std::optional<Context::ChainView> chain;
        if (row->underlying) {
            chain = host_.chain(*row->underlying, iso_date(row->expiry.value_or(0)));
        }
        const double f_paise = chain && chain->forward ? *chain->forward
                               : und                    ? static_cast<double>(*und)
                                                        : static_cast<double>(row->strike.value_or(0));
        const double t = pricing::year_fraction(now, row->last_trading_ns.value());  // set: checked at load (D-61)
        const double sigma = iv(*row, chain ? &*chain : nullptr, date);
        valued.push_back(Valued{units, false, f_paise / 100.0, static_cast<double>(row->strike.value_or(0)) / 100.0,
                                t, sigma, row->kind == "CE"});
        if (units < 0) {
            const Spec* spec = host_.spec(row->id, day);
            const Qty lot = spec != nullptr ? spec->lot : 1;
            somc_lots += ceil_div(size, lot);
            const Paise base = und ? *und : static_cast<Paise>(f_paise);
            const Int128 notional = Int128::mul(base, size);
            elm_raw += notional * elm_ppb;
            if (row->last_day() == day && addon != nullptr && row->underlying_class == std::optional<std::string>("INDEX")) {
                addon_raw += notional * addon->at("pct_ppb").get<std::int64_t>();
            }
        }
    }

    std::vector<double> base;
    for (const Valued& v : valued) {
        base.push_back(v.future ? 0.0 : pricing::price(v.call, v.f, v.k, v.t, rate, v.sigma));
    }
    struct Scenario {
        double move, vol, weight;
    };
    std::vector<Scenario> scenarios;
    for (const double m : kMoves) {
        for (const double vol : {1.0, -1.0}) {
            scenarios.push_back({m, vol, 1.0});
        }
    }
    scenarios.push_back({ext_mult, 0.0, ext_weight});
    scenarios.push_back({-ext_mult, 0.0, ext_weight});
    double worst = 0.0;
    for (const Scenario& s : scenarios) {
        double pnl = 0.0;
        for (std::size_t i = 0; i < valued.size(); ++i) {
            const Valued& v = valued[i];
            const double shifted = v.f * (1.0 + s.move * (v.future ? psr_fut : psr));
            if (v.future) {
                pnl += static_cast<double>(v.units) * (shifted - v.f);
            } else {
                pnl += static_cast<double>(v.units) *
                       (pricing::price(v.call, shifted, v.k, v.t, rate, v.sigma * (1.0 + s.vol * vsr)) - base[i]);
            }
        }
        worst = std::max(worst, -pnl * s.weight);
    }
    const bool long_options_only =
        !valued.empty() && std::all_of(valued.begin(), valued.end(),
                                       [](const Valued& v) { return v.units > 0 && !v.future; });
    if (long_options_only) {
        worst = 0.0;  // MEZ 4.5: only bought options, whose premium the run already paid in cash
    }
    UnderlyingMargin out;
    out.underlying = uid;
    out.scan = checked_mul(static_cast<std::int64_t>(std::ceil(worst)), 100);
    out.somc = ceil_to(checked_mul(p.at("somc_per_lot_paise").get<std::int64_t>(), somc_lots), 100);
    out.elm = rupee_up(elm_raw);
    out.addons = rupee_up(addon_raw);
    return out;
}

Paise SpanLiteMargin::requirement(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                                  const std::function<std::optional<Paise>(InstrumentId)>& price,
                                  const std::string& date) const {
    Paise total = 0;
    for (const UnderlyingMargin& b : breakdown(legs, instruments, price, date)) {
        total = checked_add(total, b.total());
    }
    return total;
}

}  // namespace stocksim
