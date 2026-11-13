// Fill-model decisions (A4 §4), the twin of stockify.oracle.matcher: where along a bar an order fills, and at
// what price. Positions along a bar path are (segment, exact fraction), so "which order fills first" never
// depends on floating point. The engine applies the fills; this module only decides.
#pragma once

#include <array>
#include <compare>
#include <optional>
#include <string>

#include "stocksim/int128.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/orders.hpp"
#include "stocksim/pcg32.hpp"
#include "stocksim/types.hpp"

namespace stocksim {

// An exact fraction n / d, d > 0, in lowest terms (Python's Fraction).
struct Frac {
    std::int64_t n = 0;
    std::int64_t d = 1;
    static Frac of(std::int64_t num, std::int64_t den);
    friend bool operator==(const Frac& a, const Frac& b) { return a.n == b.n && a.d == b.d; }
    friend std::strong_ordering operator<=>(const Frac& a, const Frac& b) {
        return Int128::mul(a.n, b.d) <=> Int128::mul(b.n, a.d);
    }
};

struct PathPos {
    int k = 0;  // segment index along the path
    Frac f;     // fraction of the segment
    friend bool operator==(const PathPos&, const PathPos&) = default;
    friend std::strong_ordering operator<=>(const PathPos& a, const PathPos& b) {
        if (a.k != b.k) {
            return a.k <=> b.k;
        }
        return a.f <=> b.f;
    }
};

inline constexpr PathPos kOpenPos{0, Frac{0, 1}};

using PathPoints = std::array<Paise, 4>;

struct Opportunity {
    PathPos pos;
    std::optional<Paise> price;  // fill price before slippage; nullopt: only a trigger happens
    std::string evidence;
    bool slip = false;            // MARKET-like: adverse slippage applies
    std::optional<Paise> cap;     // the limit the fill may not be worse than
    bool trigger = false;         // a stop triggers at pos
    std::optional<Paise> new_limit;
    bool becomes_market = false;
};

// A4 §3.3: buy floor, sell ceil, on the tick grid.
Paise protection_limit(Side side, Paise ref, Bp bp, Paise tick);
// FW 6.2: run.fill_model.slippage.extra_ticks, 0 when absent. A whole number from 0 to kMaxExtraTicks, else RunError
// naming fill_model.slippage.extra_ticks (a hand-edited bundle is not trusted: no float, bool, string or wrapped uint64).
inline constexpr std::int64_t kMaxExtraTicks = 1000;
std::int64_t extra_ticks_of(const Json& slip);
// Adverse slippage (A4 §4.4), never beyond cap. `slip` is run.fill_model.slippage.
Paise slipped(Side side, Paise price, Paise tick, const Json& slip, Pcg32& rng, std::optional<Paise> cap);
// A4 §4.0: floor_to(floor_div(volume · ppm, 10⁶), lot).
Qty participation_cap(Qty volume, Ppm ppm, Qty lot);
// P_HL = O→H→L→C, P_LH = O→L→H→C (A4 §4.1).
PathPoints path_points(Paise o, Paise h, Paise l, Paise c, bool high_first);
// `protect(side, ref)` gives the protection limit of an SL-M triggered at ref.
template <typename Protect>
std::optional<Opportunity> bar_opportunity(const Order& order, const PathPoints& points, bool touch,
                                           const Protect& protect);

// ── internals, exposed for the template and for tests ───────────────────────

// The value of the path at pos, as an exact rational num / den.
struct PathValue {
    Int128 num;
    std::int64_t den = 1;
    std::strong_ordering cmp(Paise x) const { return num <=> Int128::mul(x, den); }
    std::int64_t trunc() const;  // Python int(Fraction): toward zero
};
PathValue path_value(const PathPoints& points, const PathPos& pos);
std::optional<PathPos> first_reach(const PathPoints& points, Paise x, bool down, PathPos after = kOpenPos);
struct LimitHit {
    PathPos pos;
    Paise price = 0;
    std::string evidence;
};
std::optional<LimitHit> as_limit(const PathPoints& points, Side side, Paise p, PathPos after, bool touch,
                                 bool open_price);

template <typename Protect>
std::optional<Opportunity> bar_opportunity(const Order& order, const PathPoints& points, bool touch,
                                           const Protect& protect) {  // matcher.bar_opportunity
    const Paise o = points[0];
    const bool buy = order.side == Side::Buy;
    if (order.status == OrderStatus::Open) {
        const Paise lim = *order.limit;
        if (order.from_market && !order.first_eligible_done && (buy ? o <= lim : o >= lim)) {
            Opportunity x;
            x.pos = kOpenPos;
            x.price = o;
            x.evidence = "bar_open";
            x.slip = true;
            x.cap = lim;
            return x;
        }
        const auto hit = as_limit(points, order.side, lim, kOpenPos, touch, true);
        if (!hit) {
            return std::nullopt;
        }
        Opportunity x;
        x.pos = hit->pos;
        x.price = hit->price;
        x.evidence = hit->evidence;
        x.cap = lim;
        return x;
    }
    // TRIGGER_PENDING (A4 §3.4): a buy stop triggers at the open if O ≥ s, else where the path reaches s.
    const Paise s = order.trigger;
    const bool gap = buy ? o >= s : o <= s;
    const std::optional<PathPos> pos = gap ? std::optional<PathPos>(kOpenPos) : first_reach(points, s, !buy);
    if (!pos) {
        return std::nullopt;
    }
    if (order.type == OrderType::SLM) {
        const Paise ref = gap ? o : s;  // the first price beyond the trigger
        const Paise cap = protect(order.side, ref);
        Opportunity x;
        x.pos = *pos;
        x.price = ref;
        x.evidence = gap ? "bar_open" : "bar_path";
        x.slip = true;
        x.cap = cap;
        x.trigger = true;
        x.new_limit = cap;
        x.becomes_market = true;
        return x;
    }
    const Paise p = order.price;  // SL: a limit from the trigger point on
    const auto hit = as_limit(points, order.side, p, *pos, touch, true);
    Opportunity x;
    x.trigger = true;
    x.new_limit = p;
    if (!hit) {
        x.pos = *pos;
        return x;
    }
    x.pos = hit->pos;
    x.price = hit->price;
    x.evidence = hit->evidence;
    x.cap = p;
    return x;
}

}  // namespace stocksim
