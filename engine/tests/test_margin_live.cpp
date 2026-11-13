// MEZ 5.1: the live margin answer's hash, payload check and freshness, against stockify/fno/live_margin.py.
#include <algorithm>
#include <memory>
#include <ostream>  // doctest prints string_view through operator<<
#include <string>

#include "doctest.h"
#include "stocksim/engine.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/margin_live.hpp"
#include "stocksim/strategy.hpp"

using namespace stocksim;

namespace {

std::string payload_for(const LiveLegs& legs, const std::string& legs_text, const std::string& extra = "") {
    return "{\"additional_paise\":30000,\"exposure_paise\":700000,\"legs\":" + legs_text +
           ",\"option_premium_paise\":999,\"portfolio_sha256\":\"" + live_portfolio_sha256(legs) +
           "\",\"requested_ns\":5,\"source\":\"kite_basket\",\"span_paise\":5000000" + extra + "}";
}

}  // namespace

TEST_CASE("the portfolio hash is the SHA-256 of the canonical sorted legs (the oracle's value)") {
    CHECK(live_portfolio_sha256({{3, 65}, {2, -65}}) ==
          "1a40b981a841e0aec08a05fba6266c3db68194c53afaa9264e339e5215c70daa");
    CHECK(live_portfolio_sha256({}) == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945");
}

TEST_CASE("a good payload decodes; a bad one is refused by name") {
    const LiveAnswer a = check_live_payload(payload_for({{2, -65}, {3, 65}}, "[[2,-65],[3,65]]"));
    CHECK(a.span == 5'000'000);
    CHECK(a.exposure == 700'000);
    CHECK(a.additional == 30'000);
    CHECK(a.option_premium == 999);
    CHECK(a.legs.size() == 2);
    CHECK_THROWS_WITH_AS(check_live_payload("[]"), "margin_requirement: payload must be an object", RunError);
    CHECK_THROWS_AS(check_live_payload(payload_for({{3, 65}}, "[[3,65]]", ",\"x\":1")), RunError);
    CHECK_THROWS_AS(check_live_payload(payload_for({{3, 66}}, "[[3,65]]")), RunError);  // hash of other legs
    std::string floaty = payload_for({{3, 65}}, "[[3,65]]");
    floaty.replace(floaty.find("5000000"), 7, "5000000.0");
    CHECK_THROWS_AS(check_live_payload(floaty), RunError);  // a float amount
}

TEST_CASE("the latest answer per hash, fresh from its record's time for fresh_ns inclusive") {
    LiveAnswers answers;
    LiveAnswer a = check_live_payload(payload_for({{3, 65}}, "[[3,65]]"));
    const std::string sha = a.sha;
    answers.put(a, 100);
    CHECK(answers.fresh(sha, 150, 50) != nullptr);
    CHECK(answers.fresh(sha, 151, 50) == nullptr);
    CHECK(answers.fresh(sha, 99, 50) == nullptr);
    CHECK(answers.fresh(live_portfolio_sha256({{3, 66}}), 100, 50) == nullptr);
    a.span = 1;
    answers.put(a, 120);
    REQUIRE(answers.fresh(sha, 130, 50) != nullptr);
    CHECK(answers.fresh(sha, 130, 50)->span == 1);
}

namespace {

// The sod_state fixture's day-1 bundle (a flat-margin run), with the margin object replaced.
std::unique_ptr<Engine> engine_with(const Json& margin) {
    Bundle b = load_bundle(std::string(STOCKSIM_FIXTURES_DIR) + "/sod_state/day1_bundle.json");
    b.root["run"]["margin"] = margin;
    auto e = std::make_unique<Engine>(std::move(b));
    e->set_strategy(make_strategy("noop_cpp"));
    return e;
}

}  // namespace

TEST_CASE("labels() binds the margin model's label set once: sorted, unique, the same on every call") {
    const auto e = engine_with(Json{{"model", "live_with_fallback"}});
    const std::vector<std::string> first = e->labels();
    CHECK(std::is_sorted(first.begin(), first.end()));
    CHECK(std::adjacent_find(first.begin(), first.end()) == first.end());
    CHECK(std::find(first.begin(), first.end(), "margin_live:L1_answers=0") != first.end());
    CHECK(e->labels() == first);
}

TEST_CASE("margin_now reads the book and changes no label") {
    const auto e = engine_with(Json{{"model", "live_with_fallback"}});
    const std::vector<std::string> before = e->labels();
    const MarginNow now = e->margin_now();
    CHECK(now.legs.empty());
    CHECK(now.portfolio_sha256 == live_portfolio_sha256({}));
    CHECK_FALSE(now.l1);
    CHECK(e->labels() == before);
    CHECK_THROWS_AS(engine_with(Json{{"model", "flat"}})->margin_now(), RunError);
}

TEST_CASE("live_fresh_s is 1 to 86,400 seconds") {
    CHECK_NOTHROW(engine_with(Json{{"model", "live_with_fallback"}, {"live_fresh_s", 86400}}));
    CHECK_NOTHROW(engine_with(Json{{"model", "live_with_fallback"}, {"live_fresh_s", 1}}));
    for (const std::int64_t bad : {std::int64_t{0}, std::int64_t{-1}, std::int64_t{86401}, std::int64_t{10'000'000'000}}) {
        CHECK_THROWS_AS(engine_with(Json{{"model", "live_with_fallback"}, {"live_fresh_s", bad}}), RunError);
    }
    CHECK_THROWS_AS(engine_with(Json{{"model", "live_with_fallback"}, {"live_fresh_s", 1.5}}), RunError);
}
