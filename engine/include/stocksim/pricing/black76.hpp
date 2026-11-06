// Black-76: European options on a forward or futures price (M4.3, C-13).
//
// The engine's independent implementation (C-10); stockify/fno/black76.py is the oracle's. Both are written
// from the C-13 definitions and cross-checked on a large grid (A3 §3: C++ vs Python ≤ 1e-12 relative).
// Prices are in rupees as double; t in years (ACT/365); r a decimal rate. N(x) = erfc(−x/√2)/2.
#pragma once

#include <cstdint>
#include <optional>
#include <string_view>

#include "stocksim/types.hpp"

namespace stocksim::pricing {

inline constexpr double kSigmaMin = 0.001;
inline constexpr double kSigmaMax = 5.0;
inline constexpr double kPriceTol = 1e-10;  // absolute, rupees (A3 §3)
inline constexpr double kDefaultTick = 0.05;
inline constexpr TimeNs kMinTimeNs = kNsPerSecond;  // time to expiry floor
inline constexpr TimeNs kNsPerYear = 365LL * 86'400LL * kNsPerSecond;

// Why an implied volatility is NaN (C-13, D-56).
enum class IvReason : std::uint8_t { BelowIntrinsic, AboveForward, AboveStrike, BelowTick, AboveMaxVol, LowVega };

constexpr std::string_view to_string(IvReason r) {
    switch (r) {
        case IvReason::BelowIntrinsic: return "BELOW_INTRINSIC";
        case IvReason::AboveForward: return "ABOVE_FORWARD";
        case IvReason::AboveStrike: return "ABOVE_STRIKE";
        case IvReason::BelowTick: return "BELOW_TICK";
        case IvReason::AboveMaxVol: return "ABOVE_MAX_VOL";
        case IvReason::LowVega: return "LOW_VEGA";
    }
    return "?";
}

// Delta and gamma with respect to F; vega per 1.00 of volatility; theta per year; rho per 1.00 of rate.
struct Greeks {
    double price = 0, delta = 0, gamma = 0, vega = 0, theta = 0, rho = 0;
};

struct ImpliedVol {
    double sigma = 0;                // NaN when reason is set
    std::optional<IvReason> reason;  // nullopt: sigma is the answer
};

double norm_cdf(double x);
double norm_pdf(double x);

// std::invalid_argument unless F, K, t, sigma are finite and > 0.
double price(bool is_call, double f, double k, double t, double r, double sigma);
Greeks greeks(bool is_call, double f, double k, double t, double r, double sigma);

// Bounds and tick first, then Brent on [kSigmaMin, kSigmaMax] to kPriceTol, bisection if Brent stalls.
// std::invalid_argument unless F, K, t > 0 and the price is finite.
ImpliedVol implied_vol(bool is_call, double target, double f, double k, double t, double r,
                       double tick = kDefaultTick);

// ACT/365 from now to the last trading moment, floored at one second.
double year_fraction(TimeNs now, TimeNs last_trading);

}  // namespace stocksim::pricing
