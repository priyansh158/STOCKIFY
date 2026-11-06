#include "stocksim/matcher.hpp"

#include <numeric>

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

Paise slipped(Side side, Paise price, Paise tick, const Json& slip, Pcg32& rng, std::optional<Paise> cap) {
    const std::string kind = slip.contains("kind") ? slip.at("kind").get<std::string>() : "none";
    Paise move = 0;
    if (kind == "none") {
        move = 0;
    } else if (kind == "fixed_ticks") {
        move = checked_mul(slip.at("k").get<std::int64_t>(), tick);
    } else if (kind == "random_ticks") {
        move = checked_mul(rng.below(static_cast<std::uint32_t>(slip.at("k").get<std::int64_t>() + 1)), tick);
    } else if (kind == "bps") {
        move = ceil_to(ceil_div(checked_mul(price, slip.at("bp").get<std::int64_t>()), 10'000), tick);
    } else {
        throw RunError("slippage '" + kind + "'");
    }
    Paise out = side == Side::Buy ? price + move : price - move;
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
