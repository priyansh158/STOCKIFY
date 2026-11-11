// The live margin model (MEZ 5.1): see margin_live.hpp. Mirrors stockify/fno/live_margin.py; the refusal texts are
// the oracle's, check by check in its order.
#include "stocksim/margin_live.hpp"

#include <algorithm>
#include <array>
#include <limits>

#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/sha256.hpp"

namespace stocksim {

namespace {

constexpr std::array<std::string_view, 8> kKeys = {"additional_paise", "exposure_paise",   "legs",
                                                   "option_premium_paise", "portfolio_sha256", "requested_ns",
                                                   "source",           "span_paise"};
constexpr std::int64_t kMaxI64 = std::numeric_limits<std::int64_t>::max();
constexpr std::int64_t kMaxIid = std::numeric_limits<InstrumentId>::max();

[[noreturn]] void refuse(const std::string& why) { throw RunError("margin_requirement: " + why); }

// A JSON integer (never a float, a bool or a string): whole = it is one, fits = it is within an int64.
struct Whole {
    bool whole = false;
    bool fits = false;
    std::int64_t value = 0;
};

Whole whole_of(const Json& j) {
    if (!j.is_number_integer()) {
        return {};
    }
    if (j.is_number_unsigned()) {
        const auto u = j.get<std::uint64_t>();
        if (u > static_cast<std::uint64_t>(kMaxI64)) {
            return {true, false, 0};
        }
        return {true, true, static_cast<std::int64_t>(u)};
    }
    return {true, true, j.get<std::int64_t>()};
}

bool lower_hex(const std::string& s) {
    return s.size() == 64 && std::all_of(s.begin(), s.end(), [](char c) {
               return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
           });
}

Paise paise_field(const Json& payload, std::string_view key) {
    const Whole w = whole_of(payload.at(std::string(key)));
    if (!w.whole || !w.fits || w.value < 0 || w.value > kLiveMaxPaise) {
        refuse(std::string(key) + " must be a whole number of paise from 0 to " + std::to_string(kLiveMaxPaise));
    }
    return w.value;
}

}  // namespace

std::string live_portfolio_sha256(LiveLegs legs) {
    std::sort(legs.begin(), legs.end());
    std::string text = "[";
    for (std::size_t i = 0; i < legs.size(); ++i) {
        text += (i == 0 ? "[" : ",[") + std::to_string(legs[i].first) + "," + std::to_string(legs[i].second) + "]";
    }
    text += "]";
    return sha256_hex(std::string_view(text));
}

LiveAnswer check_live_payload(std::string_view json) {
    const Json p = Json::parse(json.begin(), json.end());
    if (!p.is_object()) {
        refuse("payload must be an object");
    }
    bool keys_ok = p.size() == kKeys.size();
    for (const std::string_view k : kKeys) {
        keys_ok = keys_ok && p.contains(std::string(k));
    }
    if (!keys_ok) {
        std::string listed;
        for (const std::string_view k : kKeys) {
            listed += (listed.empty() ? "" : ", ") + std::string(k);
        }
        refuse("payload must have exactly the keys " + listed);
    }
    LiveAnswer a;
    const Json& sha = p.at("portfolio_sha256");
    if (!sha.is_string() || !lower_hex(sha.get<std::string>())) {
        refuse("portfolio_sha256 must be 64 lowercase hex digits");
    }
    a.sha = sha.get<std::string>();
    const Json& legs = p.at("legs");
    std::vector<std::pair<Whole, Whole>> raw;
    bool shape_ok = legs.is_array();
    if (shape_ok) {
        for (const Json& leg : legs) {
            if (!leg.is_array() || leg.size() != 2 || !whole_of(leg[0]).whole || !whole_of(leg[1]).whole) {
                shape_ok = false;
                break;
            }
            raw.emplace_back(whole_of(leg[0]), whole_of(leg[1]));
        }
    }
    if (!shape_ok) {
        refuse("legs must be a list of [instrument, units] pairs of whole numbers");
    }
    for (const auto& [iid, units] : raw) {
        if (!iid.fits || iid.value < 1 || iid.value > kMaxIid || !units.fits || units.value == 0 ||
            units.value < -kMaxI64) {
            refuse("a leg needs an instrument id from 1 to " + std::to_string(kMaxIid) + " and units other than 0");
        }
    }
    for (std::size_t i = 1; i < raw.size(); ++i) {
        if (raw[i - 1].first.value >= raw[i].first.value) {
            refuse("legs must be sorted by instrument, one per instrument");
        }
    }
    for (const auto& [iid, units] : raw) {
        a.legs.emplace_back(static_cast<InstrumentId>(iid.value), units.value);
    }
    a.span = paise_field(p, "span_paise");
    a.exposure = paise_field(p, "exposure_paise");
    a.additional = paise_field(p, "additional_paise");
    a.option_premium = paise_field(p, "option_premium_paise");
    const Whole requested = whole_of(p.at("requested_ns"));
    if (!requested.whole || !requested.fits || requested.value < 0) {
        refuse("requested_ns must be a whole number of nanoseconds, at least 0");
    }
    a.requested_ns = requested.value;
    if (!p.at("source").is_string() || p.at("source").get<std::string>() != "kite_basket") {
        refuse("source must be 'kite_basket'");
    }
    if (a.sha != live_portfolio_sha256(a.legs)) {
        refuse("portfolio_sha256 is not the hash of legs");
    }
    return a;
}

void LiveAnswers::put(LiveAnswer answer, TimeNs t) {
    const std::string sha = answer.sha;
    by_sha_.insert_or_assign(sha, std::make_pair(t, std::move(answer)));
}

const LiveAnswer* LiveAnswers::fresh(const std::string& sha, TimeNs now, TimeNs fresh_ns) const {
    const auto it = by_sha_.find(sha);
    if (it == by_sha_.end()) {
        return nullptr;
    }
    const TimeNs age = checked_sub(now, it->second.first);
    return age >= 0 && age <= fresh_ns ? &it->second.second : nullptr;
}

LiveWithFallbackMargin::LiveWithFallbackMargin(const Json& bundle_root, SpanLiteHost host, const LiveAnswers& answers,
                                               TimeNs fresh_ns)
    : StaircaseMargin(bundle_root, host, "live_with_fallback", false),
      answers_(answers),
      fresh_ns_(fresh_ns),
      now_(host.now) {}

LiveLegs LiveWithFallbackMargin::live_legs(const std::vector<MarginLeg>& legs,
                                           const InstrumentTable& instruments) const {
    LiveLegs out;
    for (const auto& [uid, group_legs] : group(legs, instruments)) {
        for (const auto& [row, units] : group_legs) {
            out.emplace_back(row->id, units);
        }
    }
    std::sort(out.begin(), out.end());
    return out;
}

const LiveAnswer* LiveWithFallbackMargin::live(const std::vector<MarginLeg>& legs,
                                               const InstrumentTable& instruments) const {
    const LiveLegs book = live_legs(legs, instruments);
    if (book.empty()) {
        return nullptr;
    }
    return answers_.fresh(live_portfolio_sha256(book), now_(), fresh_ns_);
}

Paise LiveWithFallbackMargin::requirement(const std::vector<MarginLeg>& legs, const InstrumentTable& instruments,
                                          const Price& price, const std::string& date) const {
    if (const LiveAnswer* a = live(legs, instruments)) {
        apply(*a);
        return checked_add(checked_add(a->span, a->exposure), a->additional);
    }
    return StaircaseMargin::requirement(legs, instruments, price, date);
}

std::vector<UnderlyingMargin> LiveWithFallbackMargin::breakdown(const std::vector<MarginLeg>& legs,
                                                                const InstrumentTable& instruments,
                                                                const Price& price, const std::string& date) const {
    if (const LiveAnswer* a = live(legs, instruments)) {
        apply(*a);
        return {UnderlyingMargin{0, a->span, 0, a->exposure, a->additional, kLiveStep}};
    }
    return StaircaseMargin::breakdown(legs, instruments, price, date);
}

void LiveWithFallbackMargin::apply(const LiveAnswer& a) const {
    applied_.insert({a.sha, a.span, a.exposure, a.additional, a.option_premium, a.requested_ns});
}

const std::set<std::string>& LiveWithFallbackMargin::used_labels() const {
    labels_ = StaircaseMargin::used_labels();
    labels_.insert("margin_live:L1_answers=" + std::to_string(applied_.size()));
    return labels_;
}

}  // namespace stocksim
