// A1 §6.4 / VOL 5.3: the day-to-day state, loaded and written back byte for byte. The fixtures are the oracle's: a
// fuzz world's second-day bundle (prev_days, next_days, sod_state null) and its first day's eod_state() with the
// strategy's part emptied (a C++ noop strategy saves "{}"), so the round trip proves the engine writes what the oracle
// wrote. The cross-implementation runs are in tests/stockify/test_engine_sod_state.py.
#include <fstream>
#include <iterator>
#include <memory>
#include <stdexcept>
#include <string>

#include <ostream>  // doctest prints string_view through operator<<

#include "doctest.h"
#include "stocksim/engine.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/json_out.hpp"
#include "stocksim/sod_state.hpp"
#include "stocksim/strategy.hpp"

using namespace stocksim;

namespace {

const std::string kFixtures = std::string(STOCKSIM_FIXTURES_DIR) + "/sod_state/";

std::string read_text(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    REQUIRE(in.good());
    return std::string((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
}

std::unique_ptr<Engine> fresh_engine() {  // an Engine is neither copied nor moved (its run guard is atomic)
    auto e = std::make_unique<Engine>(load_bundle(kFixtures + "day1_bundle.json"));
    e->set_strategy(make_strategy("noop_cpp"));
    return e;
}

}  // namespace

TEST_CASE("sod_state round trip keeps every counter") {
    const std::string text = read_text(kFixtures + "day0_state.json");
    const Json st = Json::parse(text);
    REQUIRE(st.at("counters").at("next_order").get<std::uint64_t>() > 1);  // a day with orders, fills and positions
    REQUIRE(st.at("counters").at("next_fill").get<std::uint64_t>() > 1);
    REQUIRE(!st.at("ledger").at("positions").empty());

    SUBCASE("load_sod_state then eod_state_json gives the oracle's bytes back") {
        const auto e = fresh_engine();
        load_sod_state(*e, text);
        const std::string again = eod_state_json(*e);
        CHECK(again == text);
        const Json back = Json::parse(again);
        CHECK(back.at("counters") == st.at("counters"));
        CHECK(back.at("rng") == st.at("rng"));  // unsigned 64-bit stream positions survive (one is above INT64_MAX)
        CHECK(back.at("trade_date") == st.at("trade_date"));
        CHECK(back.at("pending_orders").size() == st.at("pending_orders").size());
        CHECK(back.at("final_orders").size() == st.at("final_orders").size());
        CHECK(e->prior_final().size() == st.at("final_orders").size());
        CHECK(e->charge_row_base() == st.at("counters").at("charge_row").get<std::int64_t>());
        CHECK_THROWS_WITH_AS(load_sod_state(*e, text), doctest::Contains("already loaded"), RunError);
    }

    SUBCASE("the constructor applies a bundle's sod_state") {
        Bundle b = load_bundle(kFixtures + "day1_bundle.json");
        b.root["sod_state"] = st;
        Engine e(std::move(b));
        e.set_strategy(make_strategy("noop_cpp"));
        CHECK(eod_state_json(e) == text);
    }

    SUBCASE("a bundle's sod_state must be an object or null") {
        Bundle b = load_bundle(kFixtures + "day1_bundle.json");
        b.root["sod_state"] = 3;
        CHECK_THROWS_WITH_AS(Engine{std::move(b)}, doctest::Contains("sod_state must be an object or null"), RunError);
    }

    SUBCASE("a missing key, an unknown key, another schema or invalid JSON is refused") {
        Json bad = st;
        bad.erase("timers");
        CHECK_THROWS_WITH_AS(load_sod_state_json(*fresh_engine(), bad), doctest::Contains("is missing 'timers'"), RunError);
        bad = st;
        bad["marks"] = Json::object();
        CHECK_THROWS_WITH_AS(load_sod_state_json(*fresh_engine(), bad), doctest::Contains("unknown keys ['marks']"), RunError);
        bad = st;
        bad["counters"]["event_no"] = 0;
        CHECK_THROWS_WITH_AS(load_sod_state_json(*fresh_engine(), bad), doctest::Contains("counters has unknown keys"),
                             RunError);
        bad = st;
        bad["schema"] = "stockify.sod_state/0";
        CHECK_THROWS_WITH_AS(load_sod_state_json(*fresh_engine(), bad), doctest::Contains("schema must be"), RunError);
        CHECK_THROWS_WITH_AS(load_sod_state(*fresh_engine(), std::string_view("{not json")),
                             doctest::Contains("invalid JSON"), RunError);
        bad = st;
        bad["strategy"] = 7;
        CHECK_THROWS_WITH_AS(load_sod_state_json(*fresh_engine(), bad), doctest::Contains("strategy must be an object"),
                             RunError);
    }

    SUBCASE("the strategy's part waits for set_strategy, and the loaded state is what a not-yet-run engine writes") {
        Engine e(load_bundle(kFixtures + "day1_bundle.json"));
        load_sod_state(e, text);
        CHECK(eod_state_json(e) == text);
        e.set_strategy(make_strategy("noop_cpp"));
        CHECK(eod_state_json(e) == text);
    }
}

TEST_CASE("an order's note round-trips through the state; a state without the key loads with an empty note") {
    const Json st = Json::parse(read_text(kFixtures + "day0_state.json"));
    REQUIRE(!st.at("final_orders").empty());
    REQUIRE(st.at("final_orders").at(0).contains("note"));
    const std::string hostile = "say \"hi\", then\xE2\x80\xA8 \xC3\xA9 \xF0\x9F\x98\x80";  // CH 6.22: already clean (a U+2028 loads as a space)

    SUBCASE("a note is written back byte for byte") {
        Json with = st;
        with["final_orders"][0]["note"] = hostile;
        const auto e = fresh_engine();
        load_sod_state_json(*e, with);
        const Json back = Json::parse(eod_state_json(*e));
        CHECK(back.at("final_orders").at(0).at("note").get<std::string>() == hostile);
        CHECK(back == with);
    }

    SUBCASE("a state written before the note existed loads, and writes the key with an empty note") {
        Json old = st;
        for (const char* group : {"pending_orders", "final_orders"}) {
            for (Json& o : old[group]) {
                o.erase("note");
            }
        }
        const auto e = fresh_engine();
        load_sod_state_json(*e, old);
        CHECK(Json::parse(eod_state_json(*e)) == st);
    }

    SUBCASE("a note that is not text is refused") {
        Json bad = st;
        bad["final_orders"][0]["note"] = 5;
        CHECK_THROWS_WITH_AS(load_sod_state_json(*fresh_engine(), bad), doctest::Contains("final_orders.note"), RunError);
    }
}

TEST_CASE("a state that is wrong is refused whole: the engine is left as it was") {
    const std::string text = read_text(kFixtures + "day0_state.json");
    const Json st = Json::parse(text);
    const auto fresh = [] { return std::make_unique<Engine>(load_bundle(kFixtures + "day1_bundle.json")); };
    const auto refused = [&](const std::string& bad, const char* words) {
        const auto e = fresh();
        CHECK_THROWS_WITH_AS(load_sod_state(*e, bad), doctest::Contains(words), RunError);
        load_sod_state(*e, text);  // the same engine then takes the good state, once
        CHECK(eod_state_json(*e) == text);
    };
    const auto changed = [&](const std::string& from, const std::string& to) {
        std::string bad = text;
        const std::size_t at = bad.find(from);
        REQUIRE(at != std::string::npos);
        bad.replace(at, from.size(), to);
        return bad;
    };
    refused(changed("\"strategy\":{}", "\"strategy\":{\"k\":" + std::string(64, '[') + "1" + std::string(64, ']') + "}"),
            "nested more than");
    refused(std::string(100000, '['), "nested more than");
    const std::string cash = "\"cash_paise\":" + std::to_string(st.at("cash_paise").get<std::int64_t>());
    refused(changed(cash, "\"cash_paise\":1," + cash), "duplicate key 'cash_paise'");
    refused(changed("\"strategy\":{}", "\"strategy\":{\"n\":18446744073709551616}"), "an integer outside 64 bits");
    refused(changed("\"strategy\":{}", "\"strategy\":{\"x\":1e999}"), "a number is out of range");
    refused(text + std::string(1, static_cast<char>(0)), "the text has a NUL character");
    refused(changed("[1,1768244760000000000,2220000000000]", "[1,1768244760000000000,-1]"),
            "timer period_ns must not be negative");
    refused(changed("[1,1768244760000000000,2220000000000]", "[1,1768244760000000000,9223372036854775800]"),
            "does not fit in int64");
    const std::uint64_t next_order = st.at("counters").at("next_order").get<std::uint64_t>();  // the highest id is next_order - 1
    refused(changed("\"next_order\":" + std::to_string(next_order), "\"next_order\":" + std::to_string(next_order - 1)),
            ("is not below counters.next_order (" + std::to_string(next_order - 1) + ")").c_str());
    refused(changed("\"trade_date\":\"2026-01-12\"}", "\"trade_date\":\"2026-01-13\"}"), "must be before the bundle's first day");
}

namespace {
class RefusesItsState final : public Strategy {
 public:
    void load_state(std::string_view) override { throw std::runtime_error("boom"); }
};
}  // namespace

TEST_CASE("a C++ strategy that refuses its state is named, and the failure is final for that engine") {
    const std::string text = read_text(kFixtures + "day0_state.json");
    Engine e(load_bundle(kFixtures + "day1_bundle.json"));
    load_sod_state(e, text);
    CHECK_THROWS_WITH_AS(e.set_strategy(std::make_shared<RefusesItsState>(), "refusing_cpp"),
                         doctest::Contains("strategy refusing_cpp.load_state() raised: boom"), RunError);
    CHECK_THROWS_WITH_AS(e.eod_state(), doctest::Contains("failed earlier"), RunError);
    CHECK_THROWS_WITH_AS(e.set_strategy(make_strategy("noop_cpp")), doctest::Contains("failed earlier"), RunError);
}

TEST_CASE("prev_days and next_days are validated as the oracle validates them") {
    Bundle b = load_bundle(kFixtures + "day1_bundle.json");
    REQUIRE(b.root.at("prev_days").size() == 1);
    REQUIRE(b.root.at("next_days").size() == 1);
    SUBCASE("a day before the bundle's own cannot follow it") {
        Bundle x = b;
        x.root["next_days"] = x.root["prev_days"];
        CHECK_THROWS_WITH_AS(Engine{std::move(x)}, doctest::Contains("is not after"), RunError);
    }
    SUBCASE("a seeded day is never a calendar-only day") {
        Bundle x = b;
        x.root["next_days"][0]["seed"] = 1;
        CHECK_THROWS_WITH_AS(Engine{std::move(x)}, doctest::Contains("has a seed"), RunError);
    }
    SUBCASE("not a list, not a day object") {
        Bundle x = b;
        x.root["prev_days"] = Json::object();
        CHECK_THROWS_WITH_AS(Engine{std::move(x)}, doctest::Contains("prev_days must be a list"), RunError);
        Bundle y = b;
        y.root["prev_days"] = Json::array({Json{{"date", "2000-01-01"}}});
        CHECK_THROWS_WITH_AS(Engine{std::move(y)}, doctest::Contains("must be a day object"), RunError);
    }
}

TEST_CASE("canonical JSON writes unsigned 64-bit values in full") {
    CHECK(dumps_canonical(JValue(std::uint64_t{18446744073709551615ULL})) == "18446744073709551615");
    CHECK(dumps_canonical(JValue(std::uint64_t{7})) == "7");
}
