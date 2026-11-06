// A1 §6.2: the bundle envelope.
#include <string>

#include <ostream>  // doctest prints string_view through operator<<

#include "doctest.h"
#include "stocksim/errors.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/sha256.hpp"

using namespace stocksim;

namespace {

const std::string kMinimal = R"json({
  "schema": "stockify.bundle/1",
  "seed": 42,
  "packs": [{"sha256": "ab12", "path": "packs/ab12.evt", "price_basis": "adjusted", "spec": {}}],
  "strategy": {"kind": "python", "ref": "stockify.strategies.sma_cross:SmaCross", "source_sha256": "ff",
               "params": {"fast": 10, "slow": 30}},
  "labels": ["fill:bar", "margin:flat(approx)"],
  "days": [], "instruments": []
})json";

std::string replace(std::string text, const std::string& from, const std::string& to) {
    text.replace(text.find(from), from.size(), to);
    return text;
}

}  // namespace

TEST_CASE("a bundle's envelope is read and its bytes hashed") {
    const Bundle b = parse_bundle(kMinimal);
    CHECK(b.sha256 == sha256_hex(kMinimal));
    CHECK(b.seed == 42);
    REQUIRE(b.packs.size() == 1);
    CHECK(b.packs[0].path == "packs/ab12.evt");
    CHECK(b.packs[0].price_basis == "adjusted");
    CHECK(b.strategy.kind == "python");
    CHECK(b.strategy.params["slow"].get<int>() == 30);
    CHECK(b.labels == std::vector<std::string>{"fill:bar", "margin:flat(approx)"});
    CHECK(b.root["days"].is_array());
}

TEST_CASE("a wrong or incomplete bundle fails with the field named") {
    CHECK_THROWS_AS(parse_bundle("{not json"), FormatError);
    CHECK_THROWS_WITH_AS(parse_bundle(replace(kMinimal, "stockify.bundle/1", "stockify.bundle/2")),
                         doctest::Contains("unsupported schema"), FormatError);
    CHECK_THROWS_WITH_AS(parse_bundle(replace(kMinimal, "\"seed\": 42", "\"seed\": -1")),
                         doctest::Contains("seed"), FormatError);
    CHECK_THROWS_WITH_AS(parse_bundle(replace(kMinimal, "\"price_basis\": \"adjusted\", ", "")),
                         doctest::Contains("packs[0].price_basis"), FormatError);
    CHECK_THROWS_AS(load_bundle("does/not/exist/bundle.json"), FormatError);
}
