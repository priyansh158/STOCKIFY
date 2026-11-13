// M4.3: the engine's Black-76 against the A3 G17 vectors (tests/fixtures/black76_vectors.json, shared with the
// Python tests) and the U-08 edge cases.
#include <ostream>  // doctest prints string_view through operator<<

#include <algorithm>
#include <cmath>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>

#include "doctest.h"
#include "stocksim/json_bundle.hpp"
#include "stocksim/pricing/black76.hpp"

using namespace stocksim;
using namespace stocksim::pricing;

namespace {

Json load_vectors() {
    std::ifstream in(std::string(STOCKSIM_FIXTURES_DIR) + "/black76_vectors.json", std::ios::binary);
    REQUIRE(in.good());
    std::stringstream text;
    text << in.rdbuf();
    return Json::parse(text.str());
}

// rel·|value| or half a unit in the last printed digit (or floor_abs), whichever is wider.
double tolerance(const std::string& printed, double rel, double floor_abs = 0.0) {
    const auto dot = printed.find('.');
    const int decimals = dot == std::string::npos ? 0 : static_cast<int>(printed.size() - dot - 1);
    const double half_unit = 0.5 * std::pow(10.0, -decimals);
    return std::max({rel * std::fabs(std::stod(printed)), half_unit, floor_abs});
}

double years(const Json& v) {
    return v.contains("t") ? v.at("t").get<double>() : v.at("days").get<double>() / 365.0;
}

}  // namespace

TEST_CASE("G17: Black-76 reference vectors") {
    for (const Json& v : load_vectors().at("vectors")) {
        const std::string source = v.at("source").get<std::string>();
        CAPTURE(source);
        const bool call = v.at("is_call").get<bool>();
        const Greeks g = greeks(call, v.at("f").get<double>(), v.at("k").get<double>(), years(v),
                                v.at("r").get<double>(), v.at("sigma").get<double>());
        const std::string p = v.at("price").get<std::string>();
        CHECK(std::fabs(g.price - std::stod(p)) <= tolerance(p, 0.0, 1e-6));
        for (const char* name : {"delta", "gamma", "vega"}) {
            if (v.contains(name)) {
                const std::string want = v.at(name).get<std::string>();
                const double got = std::string(name) == "delta" ? g.delta : std::string(name) == "gamma" ? g.gamma : g.vega;
                CAPTURE(name);
                CHECK(std::fabs(got - std::stod(want)) <= tolerance(want, 1e-9));
            }
        }
    }
}

TEST_CASE("put-call parity and matching gamma/vega") {
    const double f = 24'000, k = 24'500, t = 21.0 / 365, r = 0.065, s = 0.18;
    const Greeks c = greeks(true, f, k, t, r, s), p = greeks(false, f, k, t, r, s);
    CHECK(std::fabs((c.price - p.price) - std::exp(-r * t) * (f - k)) < 1e-8);
    CHECK(c.gamma == doctest::Approx(p.gamma).epsilon(1e-12));
    CHECK(c.vega == doctest::Approx(p.vega).epsilon(1e-12));
    CHECK(c.rho == doctest::Approx(-t * c.price).epsilon(1e-15));
}

TEST_CASE("IV: bounds, tick, bracket and vega") {
    const double f = 25'000, k = 24'000, t = 30.0 / 365, r = 0.065;
    const double d = std::exp(-r * t);
    CHECK(implied_vol(true, d * (f - k) - 1.0, f, k, t, r).reason == IvReason::BelowIntrinsic);
    CHECK(implied_vol(false, d * (25'500 - f) - 1.0, f, 25'500, t, r).reason == IvReason::BelowIntrinsic);
    CHECK(implied_vol(true, d * f + 0.01, f, k, t, r).reason == IvReason::AboveForward);
    CHECK(implied_vol(false, d * k + 0.01, f, k, t, r).reason == IvReason::AboveStrike);
    CHECK(implied_vol(true, 0.04, f, 30'000, t, r).reason == IvReason::BelowTick);
    CHECK(implied_vol(true, 0.8 * d * f, f, f, t, r).reason == IvReason::AboveMaxVol);
    const double deep = price(true, f, 20'000, 1.0 / 365, r, 0.05);
    CHECK(implied_vol(true, deep, f, 20'000, 1.0 / 365, r).reason == IvReason::LowVega);
    for (const double s : {0.05, 0.15, 0.6, kSigmaMax}) {
        const ImpliedVol iv = implied_vol(true, price(true, f, f, t, r, s), f, f, t, r);
        CHECK_FALSE(iv.reason.has_value());
        CHECK(std::fabs(iv.sigma - s) < 1e-8);
    }
    CHECK(std::isnan(implied_vol(false, d * k + 1, f, k, t, r).sigma));
    CHECK(to_string(IvReason::AboveMaxVol) == "ABOVE_MAX_VOL");
}

TEST_CASE("time to expiry: ACT/365, floored at one second") {
    CHECK(year_fraction(0, 86'400LL * kNsPerSecond) == 1.0 / 365);
    CHECK(year_fraction(10, 10) == 1.0 / (365.0 * 86'400));
    CHECK(year_fraction(5 * kNsPerSecond, 0) == 1.0 / (365.0 * 86'400));
}

TEST_CASE("bad inputs are refused") {
    CHECK_THROWS_AS(price(true, 0, 1, 1, 0.05, 0.1), std::invalid_argument);
    CHECK_THROWS_AS(price(true, 1, 1, 0, 0.05, 0.1), std::invalid_argument);
    CHECK_THROWS_AS(price(true, 1, 1, 1, 0.05, std::nan("")), std::invalid_argument);
    CHECK_THROWS_AS(implied_vol(true, std::nan(""), 1, 1, 1, 0.05), std::invalid_argument);
}
