// FW 6.2: slippage.extra_ticks adds k adverse ticks after the kind's own move, before the cap (A4 §4.4).
#include <string>

#include "doctest.h"
#include "stocksim/errors.hpp"
#include "stocksim/matcher.hpp"
#include "stocksim/pcg32.hpp"

using stocksim::Json;
using stocksim::Pcg32;
using stocksim::Side;

namespace {
stocksim::Paise slip(Side side, const char* spec, std::optional<stocksim::Paise> cap = std::nullopt,
                     std::uint64_t seed = 42) {
    Pcg32 rng(seed, stocksim::kSlippageStream);
    return stocksim::slipped(side, 10'000, 5, Json::parse(spec), rng, cap);
}
}  // namespace

TEST_CASE("extra_ticks adds to the move of every kind (buy 10000, tick 5)") {
    CHECK(slip(Side::Buy, R"({"kind":"none","extra_ticks":2})") == 10'010);
    CHECK(slip(Side::Buy, R"({"kind":"fixed_ticks","k":1,"extra_ticks":2})") == 10'015);
    CHECK(slip(Side::Buy, R"({"kind":"bps","bp":10,"extra_ticks":2})") == 10'020);  // ceil_to(10, 5) = 10, plus 10
    CHECK(slip(Side::Buy, R"({"kind":"bps","bp":10})") == 10'010);
    CHECK(slip(Side::Buy, R"({"kind":"random_ticks","k":3,"extra_ticks":2})") ==
          slip(Side::Buy, R"({"kind":"random_ticks","k":3})") + 10);
}

TEST_CASE("extra_ticks moves a sell down and the cap still binds") {
    CHECK(slip(Side::Sell, R"({"kind":"fixed_ticks","k":1,"extra_ticks":2})") == 9'985);
    CHECK(slip(Side::Buy, R"({"kind":"fixed_ticks","k":1,"extra_ticks":2})", 10'010) == 10'010);
    CHECK(slip(Side::Sell, R"({"kind":"fixed_ticks","k":1,"extra_ticks":2})", 9'990) == 9'990);
}

TEST_CASE("extra_ticks leaves the random_ticks stream where it was") {
    Pcg32 a(7, stocksim::kSlippageStream);
    Pcg32 b(7, stocksim::kSlippageStream);
    (void)stocksim::slipped(Side::Buy, 10'000, 5, Json::parse(R"({"kind":"random_ticks","k":3})"), a, std::nullopt);
    (void)stocksim::slipped(Side::Buy, 10'000, 5, Json::parse(R"({"kind":"random_ticks","k":3,"extra_ticks":4})"), b,
                            std::nullopt);
    CHECK(a.next_u32() == b.next_u32());
}

TEST_CASE("a hand-edited bundle's extra_ticks outside 0 to 1000, or not a whole number, is refused") {
    for (const char* bad : {"-1", "1001", "2.5", "true", "\"2\"", "null", "18446744073709551615"}) {
        const Json slip = Json::parse(std::string(R"({"kind":"none","extra_ticks":)") + bad + "}");
        CAPTURE(bad);
        CHECK_THROWS_WITH_AS(stocksim::extra_ticks_of(slip), doctest::Contains("fill_model.slippage.extra_ticks"),
                             stocksim::RunError);
        Pcg32 rng(42, stocksim::kSlippageStream);
        CHECK_THROWS_AS(stocksim::slipped(Side::Buy, 10'000, 5, slip, rng, std::nullopt), stocksim::RunError);
    }
    CHECK(stocksim::extra_ticks_of(Json::parse(R"({"kind":"none"})")) == 0);
    CHECK(stocksim::extra_ticks_of(Json::parse(R"({"kind":"none","extra_ticks":1000})")) == 1000);
    CHECK(stocksim::extra_ticks_of(Json::parse(R"({"kind":"none","extra_ticks":0})")) == 0);
}

// CH 6.8: the other slippage reads are checked as extra_ticks is (a hand-edited bundle is not trusted).
TEST_CASE("slippage k and bp are whole numbers in range, else RunError naming the key") {
    using stocksim::RunError;
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":"random_ticks","k":4294967295})"), RunError);  // k + 1 would wrap to 0
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":"random_ticks","k":4294967296})"), RunError);  // ... to 1
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":"random_ticks","k":-1})"), RunError);
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":"random_ticks","k":18446744073709551615})"), RunError);
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":"random_ticks","k":"3"})"), RunError);
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":"random_ticks"})"), RunError);
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":"fixed_ticks","k":-1})"), RunError);
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":"fixed_ticks","k":2.5})"), RunError);
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":"fixed_ticks","k":true})"), RunError);
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":"bps","bp":-1})"), RunError);
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":"bps","bp":"10"})"), RunError);
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":"bps"})"), RunError);
    CHECK_THROWS_AS(slip(Side::Buy, R"({"kind":7})"), RunError);
    CHECK_THROWS_WITH_AS(slip(Side::Buy, R"({"kind":"random_ticks","k":4294967295})"),
                         doctest::Contains("fill_model.slippage.k"), RunError);
    CHECK(slip(Side::Buy, R"({"kind":"random_ticks","k":4294967294})") >= 10'000);  // the largest k is still drawn
    CHECK(slip(Side::Buy, R"({"kind":"random_ticks","k":0})") == 10'000);
}
