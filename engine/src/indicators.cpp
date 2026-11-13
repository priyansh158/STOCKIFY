#include "stocksim/indicators.hpp"

#include <algorithm>
#include <limits>

#include "stocksim/int128.hpp"

namespace stocksim {

namespace {

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

// Python's ints never overflow; here an out-of-range step is a NumericError, never a wrapped value.
std::int64_t checked_abs(std::int64_t x) { return x < 0 ? checked_sub(0, x) : x; }

}  // namespace

double sma(const std::vector<Paise>& values, std::int64_t n) {  // strategy.sma
    const auto len = static_cast<std::int64_t>(values.size());
    if (n < 1 || len < n) {
        return kNaN;
    }
    Int128 s = 0;  // the exact integer sum
    for (std::int64_t i = len - n; i < len; ++i) {
        s += values[static_cast<std::size_t>(i)];
    }
    return s.to_double() / static_cast<double>(n);
}

double ema(const std::vector<Paise>& values, std::int64_t n) {  // strategy.ema
    const auto len = static_cast<std::int64_t>(values.size());
    if (n < 1 || len < n) {
        return kNaN;
    }
    const double alpha = 2.0 / (static_cast<double>(n) + 1.0);
    Int128 s = 0;
    for (std::int64_t i = 0; i < n; ++i) {
        s += values[static_cast<std::size_t>(i)];
    }
    double e = s.to_double() / static_cast<double>(n);
    for (std::int64_t i = n; i < len; ++i) {
        e = e + alpha * (static_cast<double>(values[static_cast<std::size_t>(i)]) - e);
    }
    return e;
}

double rsi(const std::vector<Paise>& values, std::int64_t n) {  // strategy.rsi (Wilder)
    const auto len = static_cast<std::int64_t>(values.size());
    if (n < 1 || len < n + 1) {
        return kNaN;
    }
    std::vector<Paise> changes;
    changes.reserve(values.size());
    for (std::size_t i = 1; i < values.size(); ++i) {
        changes.push_back(checked_sub(values[i], values[i - 1]));
    }
    Int128 g = 0, lo = 0;
    for (std::int64_t i = 0; i < n; ++i) {
        const Paise x = changes[static_cast<std::size_t>(i)];
        if (x > 0) {
            g += x;
        } else {
            lo -= x;
        }
    }
    double avg_gain = g.to_double() / static_cast<double>(n);
    double avg_loss = lo.to_double() / static_cast<double>(n);
    const auto nn = static_cast<double>(n);
    for (std::size_t i = static_cast<std::size_t>(n); i < changes.size(); ++i) {
        const Paise x = changes[i];
        avg_gain = (avg_gain * (nn - 1) + static_cast<double>(x > 0 ? x : 0)) / nn;
        avg_loss = (avg_loss * (nn - 1) + (x < 0 ? -static_cast<double>(x) : 0.0)) / nn;
    }
    if (avg_loss == 0) {
        return 100.0;
    }
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss);
}

double atr(const std::vector<std::array<Paise, 3>>& bars, std::int64_t n) {  // strategy.atr (Wilder)
    const auto len = static_cast<std::int64_t>(bars.size());
    if (n < 1 || len < n + 1) {
        return kNaN;
    }
    std::vector<Paise> trs;
    for (std::size_t i = 1; i < bars.size(); ++i) {
        const Paise h = bars[i][0], l = bars[i][1], c_prev = bars[i - 1][2];
        trs.push_back(std::max({checked_sub(h, l), checked_abs(checked_sub(h, c_prev)), checked_abs(checked_sub(l, c_prev))}));
    }
    Int128 s = 0;
    for (std::int64_t i = 0; i < n; ++i) {
        s += trs[static_cast<std::size_t>(i)];
    }
    double a = s.to_double() / static_cast<double>(n);
    const auto nn = static_cast<double>(n);
    for (std::size_t i = static_cast<std::size_t>(n); i < trs.size(); ++i) {
        a = (a * (nn - 1) + static_cast<double>(trs[i])) / nn;
    }
    return a;
}

}  // namespace stocksim
