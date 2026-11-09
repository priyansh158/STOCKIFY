// MEZ 4.10: the engine's exact SPAN against the oracle's vectors (tests/fixtures/span/span_vectors.json, written
// and checked by tests/stockify/test_span_parity.py), and the exact decimal arithmetic under it.
#include <ostream>  // doctest prints strings through operator<<

#include <fstream>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "doctest.h"
#include "stocksim/json_bundle.hpp"
#include "stocksim/margin/span_exact.hpp"

using namespace stocksim;
using stocksim::span::SpanDecimal;

namespace {

Json load_vectors() {
    std::ifstream in(std::string(STOCKSIM_FIXTURES_DIR) + "/span/span_vectors.json", std::ios::binary);
    REQUIRE(in.good());
    std::stringstream text;
    text << in.rdbuf();
    return Json::parse(text.str());
}

SpanDecimal dec(const std::string& text) {
    const std::optional<SpanDecimal> d = SpanDecimal::parse(text);
    REQUIRE(d.has_value());
    return *d;
}

}  // namespace

TEST_CASE("exact decimal-to-paise conversion") {
    // up to the whole rupee, then × 100
    CHECK(span::rupees_up_paise(dec("152055.01")) == 15'205'600);
    CHECK(span::rupees_up_paise(dec("152055")) == 15'205'500);
    CHECK(span::rupees_up_paise(dec("152055.000000")) == 15'205'500);
    CHECK(span::rupees_up_paise(dec("0.000000001")) == 100);
    CHECK(span::rupees_up_paise(dec("0")) == 0);
    CHECK(span::rupees_up_paise(dec("-0.00")) == 0);
    CHECK(span::rupees_up_paise(dec("1E+2")) == 10'000);
    CHECK(span::rupees_up_paise(dec("  1_000.5 ")) == 100'100);
    CHECK(span::rupees_up_paise(dec("29851.900000")) == 2'985'200);
    CHECK_THROWS_AS((void)span::rupees_up_paise(dec("-0.01")), span::SpanCalcError);
    CHECK_THROWS_AS((void)span::rupees_up_paise(dec("1e30")), std::overflow_error);  // beyond int64 paise
    // what the oracle's Decimal() refuses, or is not finite
    for (const char* bad : {"", " ", "+", ".", "nan", "NaN", "inf", "Infinity", "1e", "e5", "_1", "1_", "1__0",
                            "1.2.3", "0x10", "1 2"}) {
        CAPTURE(bad);
        CHECK_FALSE(SpanDecimal::parse(bad).has_value());
    }
    // exact: no binary fraction anywhere
    CHECK(dec("0.1") + dec("0.2") == dec("0.3"));
    CHECK(dec("2339.32") * SpanDecimal(65) == dec("152055.8"));
    CHECK(dec("1.50") == dec("1.5"));
    CHECK(dec("-0") == SpanDecimal(0));
    CHECK(dec("0.5181") * SpanDecimal(130) - dec("0.5098") * SpanDecimal(65) == dec("34.216"));
    CHECK(dec("9.79") < dec("9.791"));
    CHECK(dec("-1") < dec("0.0001"));
    CHECK(dec("123.45").to_string() == "123.45");
    CHECK(dec("-0.05").to_string() == "-0.05");
    CHECK(dec("12E3").to_string() == "12000");
}

TEST_CASE("exact division: terminating quotients only") {
    CHECK(SpanDecimal::divide_exact(SpanDecimal(65), dec("0.8")) == std::optional<SpanDecimal>(dec("81.25")));
    CHECK(SpanDecimal::divide_exact(dec("33.6765"), dec("1.000000")) == std::optional<SpanDecimal>(dec("33.6765")));
    CHECK(SpanDecimal::divide_exact(SpanDecimal(1), SpanDecimal(1024)) ==
          std::optional<SpanDecimal>(dec("0.0009765625")));
    CHECK(SpanDecimal::divide_exact(dec("-7.5"), SpanDecimal(6)) == std::optional<SpanDecimal>(dec("-1.25")));
    CHECK_FALSE(SpanDecimal::divide_exact(SpanDecimal(65), SpanDecimal(3)).has_value());  // the oracle's Inexact
    CHECK_FALSE(SpanDecimal::divide_exact(SpanDecimal(1), SpanDecimal(7)).has_value());
    CHECK(SpanDecimal::divide_exact(SpanDecimal(0), SpanDecimal(3)) == std::optional<SpanDecimal>(SpanDecimal(0)));
    CHECK_THROWS((void)SpanDecimal::divide_exact(SpanDecimal(1), SpanDecimal(0)));
}

TEST_CASE("exact SPAN agrees with the oracle's vectors") {
    const Json v = load_vectors();
    const Json& commodities = v.at("commodities");
    REQUIRE(v.at("cases").size() >= 10);
    for (const Json& c : v.at("cases")) {
        const std::string name = c.at("name").get<std::string>();
        CAPTURE(name);
        const std::string code = c.at("commodity").get<std::string>();
        const span::Commodity cc = span::commodity_from_bundle(code, commodities.at(code));
        std::vector<std::pair<span::ContractKey, Qty>> legs;
        for (const Json& l : c.at("legs")) {
            span::ContractKey key{l.at("kind").get<std::string>(), l.at("expiry").get<std::string>(), std::nullopt};
            if (!l.at("strike_paise").is_null()) {
                key.strike = l.at("strike_paise").get<Paise>();
            }
            legs.emplace_back(std::move(key), l.at("units").get<Qty>());
        }
        if (c.contains("error")) {
            try {
                (void)span::span_requirement(cc, legs);
                FAIL("no SpanCalcError");
            } catch (const span::SpanCalcError& e) {
                CHECK(std::string(e.what()) == c.at("error").get<std::string>());
            }
            continue;
        }
        const span::SpanResult res = span::span_requirement(cc, legs);
        if (c.contains("missing")) {
            REQUIRE(res.missing.size() == c.at("missing").size());
            for (std::size_t i = 0; i < res.missing.size(); ++i) {
                const Json& m = c.at("missing")[i];
                CHECK(res.missing[i].kind == m[0].get<std::string>());
                CHECK(res.missing[i].expiry == m[1].get<std::string>());
            }
            CHECK_THROWS_AS((void)span::span_paise(res), span::SpanCalcError);
            continue;
        }
        CHECK(res.scan == dec(c.at("scan").get<std::string>()));
        CHECK(res.intra_spread == dec(c.at("intra_spread").get<std::string>()));
        CHECK(res.inter_credit == dec(c.at("inter_credit").get<std::string>()));
        CHECK(res.som == dec(c.at("som").get<std::string>()));
        CHECK(res.long_option_value == dec(c.at("long_option_value").get<std::string>()));
        CHECK(res.short_option_value == dec(c.at("short_option_value").get<std::string>()));
        CHECK(res.span == dec(c.at("span").get<std::string>()));
        CHECK(span::span_paise(res) == c.at("span_paise").get<Paise>());
    }
}
