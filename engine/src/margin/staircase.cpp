// The margin staircase (MEZ 4.10): see staircase.hpp. Mirrors stockify/fno/staircase.py method by method, in its
// order (which leg is looked at first decides which reason a strict run fails with).
#include "stocksim/margin/staircase.hpp"

#include <algorithm>
#include <array>
#include <stdexcept>
#include <string_view>

#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/session.hpp"

namespace stocksim {

namespace {

constexpr std::array<std::string_view, 5> kSteps = {"B1_span_nse_elm", "B2_span_rule_elm", "B3_spanlite_calibrated",
                                                    "B4_spanlite_placeholder", "B5_flat"};
constexpr std::size_t kB1 = 0, kB2 = 1, kB3 = 2, kB4 = 3, kB5 = 4;
constexpr std::int64_t kPpb = 1'000'000'000;

std::size_t step_index(std::string_view step) {
    const auto it = std::find(kSteps.begin(), kSteps.end(), step);
    if (it == kSteps.end()) {
        throw RunError("unknown margin step '" + std::string(step) + "'");
    }
    return static_cast<std::size_t>(it - kSteps.begin());
}

// SPAN cannot price this underlying now (the oracle's _NoSpan); the message says why. Never leaves this file.
class NoSpan : public std::runtime_error {
 public:
    explicit NoSpan(const std::string& what) : std::runtime_error(what) {}
};

bool is_staircase_derivative(const Instrument& row) {  // staircase._DERIVATIVES
    return row.kind == "FUT" || row.kind == "CE" || row.kind == "PE" || row.kind == "FUT_CONT";
}

// A leg's SPAN contract key; none for a kind without one (FUT_CONT) or a row without an expiry.
std::optional<span::ContractKey> key_of(const Instrument& row) {
    if ((row.kind != "FUT" && row.kind != "CE" && row.kind != "PE") || !row.expiry) {
        return std::nullopt;
    }
    return span::ContractKey{row.kind, iso_date(*row.expiry),
                             row.kind == "FUT" ? std::nullopt : row.strike};
}

// paise × 10⁹ → paise, rounded up to the whole rupee (spanlite._rupee_up).
Paise rupee_up(const Int128& paise_ppb) { return ceil_to(paise_ppb.ceil_div(kPpb).to_int64(), 100); }

// Whether `s` is an ELM % in its plain bounded form (nse_margin_files.ELM_PCT, M-3): 1-3 ASCII digits, optionally
// '.' and 1-8 more; no sign, space or exponent.
bool plain_elm_pct(std::string_view s) {
    const auto digits = [&s](std::size_t from) {
        std::size_t i = from;
        while (i < s.size() && s[i] >= '0' && s[i] <= '9') {
            ++i;
        }
        return i - from;
    };
    const std::size_t n_int = digits(0);
    if (n_int < 1 || n_int > 3) {
        return false;
    }
    if (n_int == s.size()) {
        return true;
    }
    if (s[n_int] != '.') {
        return false;
    }
    const std::size_t n_frac = digits(n_int + 1);
    return n_frac >= 1 && n_frac <= 8 && n_int + 1 + n_frac == s.size();
}

// An ELM per-cent string ("2", "3.5") as an exact ppb fraction (staircase._elm_ppb); nullopt when absent or
// unusable (not in the plain bounded form, not positive, or not a whole number of ppb). Beyond int64 is
// std::overflow_error, as the oracle's OverflowError. Bundles carry strings; any other JSON value is unusable here.
std::optional<std::int64_t> elm_ppb(const Json& pct) {
    if (!pct.is_string() || !plain_elm_pct(pct.get_ref<const std::string&>())) {
        return std::nullopt;
    }
    const std::optional<span::SpanDecimal> value = span::SpanDecimal::parse(pct.get<std::string>());
    if (!value || value->sign() <= 0) {
        return std::nullopt;
    }
    const std::optional<Int128> ppb = value->scaled_integer(7);  // per cent / 100 × 10⁹
    if (!ppb) {
        return std::nullopt;
    }
    if (!ppb->fits_int64()) {
        throw std::overflow_error("ELM % '" + pct.get<std::string>() + "' does not fit in int64 ppb");
    }
    return ppb->to_int64();
}

bool is_placeholder(const Json& row) {  // spanlite.step_of
    return row.contains("calibration_id") && row.at("calibration_id").is_string() &&
           row.at("calibration_id").get<std::string>() == "placeholder";
}

}  // namespace

StaircaseMargin::StaircaseMargin(const Json& bundle_root, SpanLiteHost host, std::string model, bool strict)
    : model_(std::move(model)),
      strict_(strict),
      no_section_(!bundle_root.contains("span")),  // a hand-built bundle: labelled, steps down
      span_(bundle_root.contains("span") && bundle_root.at("span").is_object() ? bundle_root.at("span")
                                                                                 : Json::object()),
      elm_(bundle_root.contains("elm") && bundle_root.at("elm").is_object() ? bundle_root.at("elm") : Json::object()),
      spanlite_(bundle_root, host),
      flat_(bundle_root),
      host_(std::move(host)) {}

const std::set<std::string>& StaircaseMargin::used_labels() const {
    labels_ = flat_.used_labels();
    labels_.insert(spanlite_.used_labels().begin(), spanlite_.used_labels().end());
    if (no_section_) {
        labels_.insert(kNoSectionLabel);
    }
    for (const auto& [step, days] : step_days_) {
        labels_.insert("margin:" + step);
        labels_.insert("margin_steps:" + step + "=" + std::to_string(days.size()));
    }
    return labels_;
}

std::map<InstrumentId, StaircaseMargin::Legs> StaircaseMargin::group(const std::vector<MarginLeg>& legs,
                                                                      const InstrumentTable& instruments) const {
    std::map<InstrumentId, Legs> by_underlying;
    for (const MarginLeg& leg : legs) {
        const Instrument& row = instruments.at(leg.instrument);
        if (leg.units == 0 || leg.product != Product::NRML || !is_staircase_derivative(row) || !row.underlying_id ||
            *row.underlying_id == 0) {
            continue;
        }
        by_underlying[*row.underlying_id].emplace_back(&row, leg.units);
    }
    return by_underlying;
}

std::vector<UnderlyingMargin> StaircaseMargin::breakdown(const std::vector<MarginLeg>& legs,
                                                         const InstrumentTable& instruments, const Price& price,
                                                         const std::string& date) const {
    return priced(legs, instruments, price, date, true);
}

std::vector<UnderlyingMargin> StaircaseMargin::priced(const std::vector<MarginLeg>& legs,
                                                      const InstrumentTable& instruments, const Price& price,
                                                      const std::string& date, bool record) const {
    std::vector<UnderlyingMargin> out;
    for (const auto& [uid, group_legs] : group(legs, instruments)) {
        UnderlyingMargin b = one(uid, group_legs, instruments, price, date);
        if (record) {
            used(b.step, date);
        }
        out.push_back(std::move(b));
    }
    return out;
}

Paise StaircaseMargin::requirement(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                                   const Price& price, const std::string& date) const {
    Paise total = 0;
    for (const UnderlyingMargin& b : breakdown(legs, instruments, price, date)) {
        total = checked_add(total, b.total());
    }
    return total;
}

std::pair<Paise, Paise> StaircaseMargin::pair(const std::vector<MarginLeg>& before,
                                              const std::vector<MarginLeg>& after,
                                              const InstrumentTable& instruments, const Price& price_before,
                                              const Price& price_after, const std::string& date) const {
    const std::map<InstrumentId, Legs> gb = group(before, instruments), ga = group(after, instruments);
    std::set<InstrumentId> uids;
    for (const auto& [uid, legs] : gb) {
        uids.insert(uid);
    }
    for (const auto& [uid, legs] : ga) {
        uids.insert(uid);
    }
    Paise total_b = 0, total_a = 0;
    for (const InstrumentId uid : uids) {
        const auto ib = gb.find(uid), ia = ga.find(uid);
        std::optional<UnderlyingMargin> b, a;
        if (ib != gb.end()) {
            b = one(uid, ib->second, instruments, price_before, date);
        }
        if (ia != ga.end()) {
            a = one(uid, ia->second, instruments, price_after, date);
        }
        for (std::size_t pass = 0; pass < kSteps.size(); ++pass) {  // each pass only moves down; at most five
            std::size_t floor = 0;
            for (const auto* x : {&a, &b}) {
                if (*x) {
                    floor = std::max(floor, step_index((*x)->step));
                }
            }
            if (b && step_index(b->step) < floor) {
                b = one(uid, ib->second, instruments, price_before, date, floor);
            }
            if (a && step_index(a->step) < floor) {
                a = one(uid, ia->second, instruments, price_after, date, floor);
            }
            if (!a || !b || a->step == b->step) {
                break;  // one step for the underlying
            }
        }
        for (const auto* x : {&a, &b}) {
            if (*x) {
                used((*x)->step, date);
            }
        }
        total_b = checked_add(total_b, b ? b->total() : 0);
        total_a = checked_add(total_a, a ? a->total() : 0);
    }
    return {total_b, total_a};
}

UnderlyingMargin StaircaseMargin::one(InstrumentId uid, const Legs& legs, const InstrumentTable& instruments,
                                      const Price& price, const std::string& date, std::size_t floor) const {
    if (floor <= kB2) {
        try {
            return exact(uid, legs, price, date, floor == kB1);
        } catch (const NoSpan& e) {
            if (strict_) {
                throw RunError(std::string("span_exact: ") + e.what());
            }
        } catch (const span::SpanCalcError&) {  // a file this code cannot apply exactly
            if (strict_) {
                throw;
            }
        }
    }
    Legs priced;
    for (const auto& leg : legs) {
        if (leg.first->is_derivative()) {  // FUT, CE, PE (not FUT_CONT)
            priced.push_back(leg);
        }
    }
    const Json* row = spanlite_.find_params(*legs.front().first, date);
    if (row != nullptr && !priced.empty() && floor <= kB4) {
        UnderlyingMargin b = spanlite_.one(uid, priced, price, date);
        b.step = std::string(kSteps[is_placeholder(*row) ? kB4 : kB3]);
        return b;
    }
    std::vector<MarginLeg> flat_legs;
    for (const auto& [r, units] : legs) {
        flat_legs.push_back(MarginLeg{r->id, Product::NRML, units});
    }
    UnderlyingMargin out;
    out.underlying = uid;
    out.scan = flat_.requirement(flat_legs, instruments, price, date);
    out.step = std::string(kSteps[kB5]);
    return out;
}

std::pair<const span::Commodity*, std::string> StaircaseMargin::commodity(const std::optional<std::string>& und,
                                                                          const std::string& date) const {
    const std::string name = und.value_or("None");
    const TimeNs now = host_.now();
    const auto day = span_.find(date);
    std::optional<std::size_t> chosen;
    if (day != span_.end() && day->is_array()) {
        for (std::size_t n = 0; n < day->size(); ++n) {  // in created order; the last one created <= now
            if ((*day)[n].at("created_ns").get<TimeNs>() <= now) {
                chosen = n;
            }
        }
    }
    if (!chosen) {
        throw NoSpan("no SPAN file in force for " + name + " at " + std::to_string(now) + " on " + date);
    }
    const Json& entry = (*day)[*chosen];
    const std::string file = entry.at("file").get<std::string>();
    if (!und) {
        throw NoSpan(name + " is not in " + file);
    }
    auto key = std::make_tuple(date, *chosen, *und);
    auto it = cc_.find(key);
    if (it == cc_.end()) {
        std::optional<span::Commodity> cc;
        const Json& commodities = entry.at("commodities");
        const auto d = commodities.find(*und);
        if (d != commodities.end() && !d->is_null()) {
            cc = span::commodity_from_bundle(*und, *d);
        }
        it = cc_.emplace(std::move(key), std::move(cc)).first;
    }
    if (!it->second) {
        throw NoSpan(name + " is not in " + file);
    }
    return {&*it->second, file};
}

std::optional<Paise> StaircaseMargin::b1_elm(const std::optional<std::string>& und, InstrumentId uid,
                                             const Legs& legs, const Price& price, const std::string& date) const {
    const auto day = elm_.find(date);
    if (day == elm_.end()) {
        return std::nullopt;  // no ELM file for the day
    }
    const Json* pcts = nullptr;
    if (und && day->is_object()) {
        const auto p = day->find(*und);
        if (p != day->end() && p->is_object()) {
            pcts = &*p;
        }
    }
    const std::optional<Paise> und_paise = price(uid);
    Int128 raw = 0;  // paise × ppb
    for (const auto& [row, units] : legs) {
        if (row->kind != "FUT" && units > 0) {
            continue;  // bought option: no exposure
        }
        const span::ContractKey key = *key_of(*row);  // set: exact() checked every leg
        const std::string lookup =
            key.kind + "|" + key.expiry + "|" + (key.strike ? std::to_string(*key.strike) : std::string());
        std::optional<std::int64_t> ppb;
        if (pcts != nullptr) {
            const auto it = pcts->find(lookup);
            if (it != pcts->end()) {
                ppb = elm_ppb(*it);
            }
        }
        if (!ppb) {
            if (strict_) {
                throw RunError("span_exact: " + und.value_or("None") + " " + span::key_name(key) +
                               " has no ELM % in the ELM file for " + date);
            }
            return std::nullopt;
        }
        const std::optional<Paise> base = row->kind == "FUT" ? price(row->id) : und_paise;  // exact: no substitute
        if (!base) {
            return std::nullopt;
        }
        raw += Int128::mul(*ppb, *base) * (units < 0 ? checked_sub(0, units) : units);
    }
    return rupee_up(raw);
}

UnderlyingMargin StaircaseMargin::exact(InstrumentId uid, const Legs& legs, const Price& price,
                                        const std::string& date, bool allow_b1) const {
    const std::optional<std::string>& und = legs.front().first->underlying;
    const std::string name = und.value_or("None");
    std::vector<std::pair<span::ContractKey, Qty>> keyed;
    for (const auto& [row, units] : legs) {
        std::optional<span::ContractKey> key = key_of(*row);
        if (!key) {
            throw NoSpan((row->tradingsymbol.empty() ? std::to_string(row->id) : row->tradingsymbol) + " (" +
                         row->kind + ") has no SPAN contract");
        }
        keyed.emplace_back(std::move(*key), units);
    }
    const auto [cc, file] = commodity(und, date);
    const span::SpanResult res = span::span_requirement(*cc, keyed);
    if (!res.missing.empty()) {
        std::string names;
        for (const span::ContractKey& k : res.missing) {
            names += (names.empty() ? "" : ", ") + span::key_name(k);
        }
        throw NoSpan(name + " " + names + " not in the SPAN file in force (" + file + ") on " + date);
    }
    UnderlyingMargin out;
    out.underlying = uid;
    out.scan = span::span_paise(res);                                        // the whole SPAN requirement
    out.somc = checked_mul(res.som.ceiling().to_int64(), kPaisePerRupee);  // its SOM: rupees up → paise
    const std::optional<Paise> elm = allow_b1 ? b1_elm(und, uid, legs, price, date) : std::nullopt;
    if (elm) {
        out.elm = *elm;
        out.step = std::string(kSteps[kB1]);
        return out;
    }
    if (spanlite_.find_params(*legs.front().first, date) == nullptr) {  // no dated rule row: B2 cannot price it
        throw NoSpan("no NSE ELM % for every " + name + " leg and no spanlite margin row for " + name + " on " +
                     date);
    }
    std::tie(out.elm, out.addons) = spanlite_.rule_elm(legs, price, date, price(uid));
    out.step = std::string(kSteps[kB2]);
    return out;
}

}  // namespace stocksim
