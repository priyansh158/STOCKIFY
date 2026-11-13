#include "stocksim/matcher.hpp"

#include <algorithm>
#include <cstdint>
#include <limits>
#include <numeric>
#include <string>

#include "stocksim/errors.hpp"

namespace stocksim {

Frac Frac::of(std::int64_t num, std::int64_t den) {
    if (den == 0) {
        throw NumericError("Fraction with a zero denominator");
    }
    if (den < 0) {
        num = -num;
        den = -den;
    }
    const std::int64_t g = std::gcd(num < 0 ? -num : num, den);
    return Frac{num / g, den / g};
}

std::int64_t PathValue::trunc() const {
    // Python int(Fraction) truncates toward zero; floor for a non-negative value, ceiling for a negative one.
    return (num.negative() ? num.ceil_div(den) : num.floor_div(den)).to_int64();
}

Paise protection_limit(Side side, Paise ref, Bp bp, Paise tick) {  // matcher.protection_limit
    if (side == Side::Buy) {
        return checked_mul(mul_div_floor(ref, 10'000 + bp, checked_mul(10'000, tick)), tick);
    }
    // ceil_div(ref·(10000 − bp), 10000·tick)·tick = −floor_div(−ref·(10000 − bp), 10000·tick)·tick
    return checked_mul(-mul_div_floor(-ref, 10'000 - bp, checked_mul(10'000, tick)), tick);
}

std::int64_t extra_ticks_of(const Json& slip) {  // matcher.extra_ticks_of
    const auto it = slip.find("extra_ticks");
    if (it == slip.end()) {
        return 0;
    }
    // nlohmann holds a non-negative integer as unsigned: read it as such and clamp, so 2^64 - 1 never wraps to -1.
    const bool whole = it->is_number_integer();
    const std::int64_t n = !whole ? -1
                           : it->is_number_unsigned()
                               ? static_cast<std::int64_t>(std::min<std::uint64_t>(it->get<std::uint64_t>(), kMaxExtraTicks + 1))
                               : it->get<std::int64_t>();
    if (n < 0 || n > kMaxExtraTicks) {
        throw RunError("fill_model.slippage.extra_ticks: must be a whole number from 0 to " +
                       std::to_string(kMaxExtraTicks));
    }
    return n;
}

namespace {
// CH 6.8: a whole number of the slippage object in [0, hi], else RunError naming the key (the bundle is not trusted:
// no float, bool, string, negative value or uint64 past int64; nlohmann would otherwise wrap or throw its own type).
std::int64_t slip_whole(const Json& slip, const char* key, std::int64_t hi) {
    const auto it = slip.find(key);
    const bool ok = it != slip.end() && it->is_number_integer() &&
                    !(it->is_number_unsigned() && it->get<std::uint64_t>() > static_cast<std::uint64_t>(hi));
    const std::int64_t n = ok ? it->get<std::int64_t>() : -1;
    if (n < 0 || n > hi) {
        throw RunError(std::string("fill_model.slippage.") + key + ": must be a whole number from 0 to " +
                       std::to_string(hi));
    }
    return n;
}

constexpr std::int64_t kMaxRandomTicksK = 0xFFFF'FFFE;  // rng.below takes k + 1 as a uint32
}  // namespace

Paise slipped(Side side, Paise price, Paise tick, const Json& slip, Pcg32& rng, std::optional<Paise> cap) {
    std::string kind = "none";
    if (const auto kit = slip.find("kind"); kit != slip.end()) {
        if (!kit->is_string()) {
            throw RunError("fill_model.slippage.kind: must be text");
        }
        kind = kit->get<std::string>();
    }
    Paise move = 0;
    if (kind == "none") {
        move = 0;
    } else if (kind == "fixed_ticks") {
        move = checked_mul(slip_whole(slip, "k", std::numeric_limits<std::int64_t>::max()), tick);
    } else if (kind == "random_ticks") {
        move = checked_mul(rng.below(static_cast<std::uint32_t>(slip_whole(slip, "k", kMaxRandomTicksK) + 1)), tick);
    } else if (kind == "bps") {
        move = ceil_to(ceil_div(checked_mul(price, slip_whole(slip, "bp", std::numeric_limits<std::int64_t>::max())),
                                10'000),
                       tick);
    } else {
        throw RunError("slippage '" + kind + "'");
    }
    // FW 6.2: after the kind's own draw, before the sign and the cap
    move = checked_add(move, checked_mul(extra_ticks_of(slip), tick));
    Paise out = side == Side::Buy ? checked_add(price, move) : checked_sub(price, move);
    if (cap) {
        out = side == Side::Buy ? std::min(out, *cap) : std::max(out, *cap);
    }
    return out;
}

Qty participation_cap(Qty volume, Ppm ppm, Qty lot) {
    return floor_to(mul_div_floor(volume, ppm, 1'000'000), lot);
}

PathPoints path_points(Paise o, Paise h, Paise l, Paise c, bool high_first) {
    return high_first ? PathPoints{o, h, l, c} : PathPoints{o, l, h, c};
}

PathValue path_value(const PathPoints& points, const PathPos& pos) {  // matcher._value
    const Paise a = points[static_cast<std::size_t>(pos.k)];
    const Paise b = points[static_cast<std::size_t>(pos.k + 1)];
    // a + (b − a)·n/d = (a·d + (b − a)·n) / d
    return PathValue{Int128::mul(a, pos.f.d) + Int128::mul(b - a, pos.f.n), pos.f.d};
}

std::optional<PathPos> first_reach(const PathPoints& points, Paise x, bool down, PathPos after) {
    auto ok_value = [&](const PathValue& v) { return down ? v.cmp(x) <= 0 : v.cmp(x) >= 0; };
    auto ok_int = [&](Paise v) { return down ? v <= x : v >= x; };
    for (int k = after.k; k < static_cast<int>(points.size()) - 1; ++k) {
        const Frac start = k == after.k ? after.f : Frac{0, 1};
        if (ok_value(path_value(points, PathPos{k, start}))) {
            return PathPos{k, start};
        }
        const Paise a = points[static_cast<std::size_t>(k)];
        const Paise b = points[static_cast<std::size_t>(k + 1)];
        if (a != b && ok_int(b)) {
            const Frac f = Frac::of(a - x, a - b);
            if (f >= start) {
                return PathPos{k, f};
            }
        }
    }
    return std::nullopt;
}

namespace {

// min/max over [value at pos, the later points] (matcher._extreme_after), as an exact rational.
PathValue extreme_after(const PathPoints& points, const PathPos& pos, bool low) {
    PathValue best = path_value(points, pos);
    for (int k = pos.k + 1; k < static_cast<int>(points.size()); ++k) {
        const Paise v = points[static_cast<std::size_t>(k)];
        const auto c = best.cmp(v);
        if (low ? c > 0 : c < 0) {
            best = PathValue{Int128(v), 1};
        }
    }
    return best;
}

}  // namespace

std::optional<LimitHit> as_limit(const PathPoints& points, Side side, Paise p, PathPos after, bool touch,
                                 bool open_price) {  // matcher._as_limit
    const bool buy = side == Side::Buy;
    const PathValue here = path_value(points, after);
    if (buy ? here.cmp(p) <= 0 : here.cmp(p) >= 0) {
        return LimitHit{after, open_price ? here.trunc() : p, after == kOpenPos ? "bar_open" : "bar_path"};
    }
    const PathValue extreme = extreme_after(points, after, buy);
    const auto c = extreme.cmp(p);
    const bool reached = buy ? (touch ? c <= 0 : c < 0) : (touch ? c >= 0 : c > 0);
    if (!reached) {
        return std::nullopt;
    }
    const std::optional<PathPos> pos = first_reach(points, p, buy, after);
    if (!pos) {
        return std::nullopt;
    }
    return LimitHit{*pos, p, "bar_path"};
}

}  // namespace stocksim
