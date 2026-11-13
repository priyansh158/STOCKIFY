// Black-76 for the engine (M4.3, C-13), written from the definitions:
//   D = e^{−rt}, d1 = (ln(F/K) + σ²t/2)/(σ√t), d2 = d1 − σ√t
//   call = D(F·N(d1) − K·N(d2)), put = D(K·N(−d2) − F·N(−d1))
//   Δ = D·N(d1) (call), −D·N(−d1) (put); Γ = D·n(d1)/(F·σ·√t); vega = D·F·n(d1)·√t
//   θ = −D·F·n(d1)·σ/(2√t) + r·V; ρ = −t·V (with F given, only the discount factor depends on r)
#include "stocksim/pricing/black76.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <numbers>
#include <stdexcept>
#include <string>

namespace stocksim::pricing {

namespace {

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();
constexpr int kMaxIter = 200;

bool positive_finite(double x) { return std::isfinite(x) && x > 0.0; }

void require_inputs(double f, double k, double t, double sigma) {
    if (!positive_finite(f) || !positive_finite(k) || !positive_finite(t) || !positive_finite(sigma)) {
        throw std::invalid_argument("Black-76 needs F, K, t, sigma > 0 and finite (F=" + std::to_string(f) +
                                    ", K=" + std::to_string(k) + ", t=" + std::to_string(t) +
                                    ", sigma=" + std::to_string(sigma) + ")");
    }
}

struct D12 {
    double d1, d2;
};

D12 d12(double f, double k, double t, double sigma) {
    const double spread = sigma * std::sqrt(t);  // σ√t
    const double d1 = (std::log(f / k) + 0.5 * sigma * sigma * t) / spread;
    return {d1, d1 - spread};
}

double value_of(bool is_call, double f, double k, double disc, const D12& d) {
    return is_call ? disc * (f * norm_cdf(d.d1) - k * norm_cdf(d.d2))
                   : disc * (k * norm_cdf(-d.d2) - f * norm_cdf(-d.d1));
}

// Brent's method (Brent 1973, ch. 4) for g(σ) = model(σ) − target with g(lo), g(hi) of opposite signs.
// A step is interpolated (inverse quadratic, or secant when two of the last three values coincide) only when
// it falls between (3a + b)/4 and b and shrinks faster than bisection; otherwise the bracket is halved.
template <class G>
std::optional<double> brent(const G& g, double a, double b, double ga, double gb) {
    if (std::fabs(ga) < std::fabs(gb)) {
        std::swap(a, b);
        std::swap(ga, gb);
    }
    double c = a, gc = ga, prev = c;
    bool last_was_bisection = true;
    constexpr double kMinStep = 1e-15;
    for (int i = 0; i < kMaxIter; ++i) {
        if (std::fabs(gb) <= kPriceTol) {
            return b;
        }
        double s = 0;
        if (ga != gc && gb != gc) {
            s = a * gb * gc / ((ga - gb) * (ga - gc)) + b * ga * gc / ((gb - ga) * (gb - gc)) +
                c * ga * gb / ((gc - ga) * (gc - gb));
        } else {
            s = b - gb * (b - a) / (gb - ga);
        }
        const double edge = (3.0 * a + b) / 4.0;
        const bool outside = !(std::min(edge, b) < s && s < std::max(edge, b));
        const bool slow = last_was_bisection ? std::fabs(s - b) >= std::fabs(b - c) / 2.0
                                             : std::fabs(s - b) >= std::fabs(c - prev) / 2.0;
        const bool tiny = last_was_bisection ? std::fabs(b - c) < kMinStep : std::fabs(c - prev) < kMinStep;
        last_was_bisection = outside || slow || tiny;
        if (last_was_bisection) {
            s = 0.5 * (a + b);
        }
        const double gs = g(s);
        prev = c;
        c = b;
        gc = gb;
        if ((ga < 0) != (gs < 0)) {
            b = s;
            gb = gs;
        } else {
            a = s;
            ga = gs;
        }
        if (std::fabs(ga) < std::fabs(gb)) {
            std::swap(a, b);
            std::swap(ga, gb);
        }
    }
    return std::nullopt;
}

template <class G>
double bisection(const G& g, double a, double b, double ga) {
    double m = 0.5 * (a + b);
    for (int i = 0; i < 2000; ++i) {
        m = 0.5 * (a + b);
        const double gm = g(m);
        if (std::fabs(gm) <= kPriceTol || m == a || m == b) {
            return m;
        }
        if ((ga < 0) != (gm < 0)) {
            b = m;
        } else {
            a = m;
            ga = gm;
        }
    }
    return m;
}

}  // namespace

double norm_cdf(double x) { return 0.5 * std::erfc(-x / std::numbers::sqrt2); }

// The same operations as the oracle's (a constant 1/√(2π), then one product): the chain view logs these numbers,
// and parity there needs them bit-equal (A3 §3 allows 1e-12, the logs allow nothing).
double norm_pdf(double x) {
    static const double inv_sqrt_2pi = 1.0 / std::sqrt(2.0 * std::numbers::pi);
    return inv_sqrt_2pi * std::exp(-0.5 * x * x);
}

double price(bool is_call, double f, double k, double t, double r, double sigma) {
    require_inputs(f, k, t, sigma);
    return value_of(is_call, f, k, std::exp(-r * t), d12(f, k, t, sigma));
}

Greeks greeks(bool is_call, double f, double k, double t, double r, double sigma) {
    require_inputs(f, k, t, sigma);
    const double disc = std::exp(-r * t);
    const double root_t = std::sqrt(t);
    const D12 d = d12(f, k, t, sigma);
    const double density = norm_pdf(d.d1);
    Greeks g;
    g.price = value_of(is_call, f, k, disc, d);
    g.delta = is_call ? disc * norm_cdf(d.d1) : -disc * norm_cdf(-d.d1);
    g.gamma = disc * density / (f * sigma * root_t);
    g.vega = disc * f * density * root_t;
    g.theta = -disc * f * density * sigma / (2.0 * root_t) + r * g.price;
    g.rho = -t * g.price;
    return g;
}

ImpliedVol implied_vol(bool is_call, double target, double f, double k, double t, double r, double tick) {
    if (!positive_finite(f) || !positive_finite(k) || !positive_finite(t) || !std::isfinite(target)) {
        throw std::invalid_argument("implied_vol needs F, K, t > 0 and a finite price");
    }
    const double disc = std::exp(-r * t);
    const double floor_value = disc * std::max(is_call ? f - k : k - f, 0.0);
    const double cap = disc * (is_call ? f : k);
    if (target < floor_value - kPriceTol) {
        return {kNaN, IvReason::BelowIntrinsic};
    }
    if (target > cap) {
        return {kNaN, is_call ? IvReason::AboveForward : IvReason::AboveStrike};
    }
    if (target < tick) {
        return {kNaN, IvReason::BelowTick};
    }
    const auto g = [&](double sigma) { return price(is_call, f, k, t, r, sigma) - target; };
    const double g_lo = g(kSigmaMin);
    const double g_hi = g(kSigmaMax);
    double sigma = 0;
    if (std::fabs(g_lo) <= kPriceTol) {
        sigma = kSigmaMin;
    } else if (std::fabs(g_hi) <= kPriceTol) {
        sigma = kSigmaMax;
    } else if (g_lo > 0) {
        return {kNaN, IvReason::LowVega};  // no time value left to read a volatility from
    } else if (g_hi < 0) {
        return {kNaN, IvReason::AboveMaxVol};
    } else {
        const std::optional<double> root = brent(g, kSigmaMin, kSigmaMax, g_lo, g_hi);
        sigma = root ? *root : bisection(g, kSigmaMin, kSigmaMax, g_lo);
    }
    // Half a tick of price error moving σ by more than 1.00: the price does not identify σ.
    if (greeks(is_call, f, k, t, r, sigma).vega < tick / 2.0) {
        return {kNaN, IvReason::LowVega};
    }
    return {sigma, std::nullopt};
}

double year_fraction(TimeNs now, TimeNs last_trading) {
    const TimeNs diff = checked_sub(last_trading, now);
    const TimeNs left = diff > kMinTimeNs ? diff : kMinTimeNs;
    return static_cast<double>(left) / static_cast<double>(kNsPerYear);  // the oracle divides the same two doubles
}

}  // namespace stocksim::pricing
