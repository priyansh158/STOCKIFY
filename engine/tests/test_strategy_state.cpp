// CH 6.24: a refused load leaves a C++ twin as it was, and the state a twin saves is the one it reads back.
#include <string>

#include "doctest.h"
#include "stocksim/errors.hpp"
#include "stocksim/strategy.hpp"

using namespace stocksim;

namespace {

const std::string kOrb =
    R"({"closed":false,"day":"2026-03-02","entry_id":4,"exit_hhmm":"15:10","hi":10150,"iid":1,"lo":9950,"minutes":2,)"
    R"("open_ns":1772423100000000000,"qty":10,"short":false,"stop_ids":[5],"stop_side":"SELL","stop_trigger":9950,)"
    R"("symbol":"NSE:TEST","traded":true})";
const std::string kSma = R"({"fast":2,"iid":1,"isec":86400,"long_short":false,"product":"CNC","slow":3,"symbol":"NSE:TEST"})";

void refused_load_changes_nothing(const char* name, const std::string& good, const std::string& bad) {
    const auto s = make_strategy(name);
    s->load_state(good);
    const std::string before = s->save_state();
    CHECK(before == Json::parse(good).dump());  // what it read is what it writes
    CHECK_THROWS_AS(s->load_state(bad), RunError);
    CHECK(s->save_state() == before);
}

}  // namespace

TEST_CASE("a refused state leaves orb_cpp as it was") {
    Json bad = Json::parse(kOrb);
    bad["qty"] = "ten";
    refused_load_changes_nothing("orb_cpp", kOrb, bad.dump());
    Json lopsided = Json::parse(kOrb);
    lopsided["lo"] = nullptr;  // hi set, lo null: refused, never dereferenced
    refused_load_changes_nothing("orb_cpp", kOrb, lopsided.dump());
    Json late = Json::parse(kOrb);
    late["stop_side"] = "HOLD";  // a field read after the others: the earlier ones must not stick
    refused_load_changes_nothing("orb_cpp", kOrb, late.dump());
}

TEST_CASE("a refused state leaves sma_cross_cpp as it was") {
    Json bad = Json::parse(kSma);
    bad["product"] = 3;
    refused_load_changes_nothing("sma_cross_cpp", kSma, bad.dump());
    Json cnc = Json::parse(kSma);
    cnc["long_short"] = true;
    refused_load_changes_nothing("sma_cross_cpp", kSma, cnc.dump());
}

TEST_CASE("an empty state is accepted, and a twin never started saves it back") {
    for (const char* name : {"orb_cpp", "sma_cross_cpp"}) {
        const auto s = make_strategy(name);
        CHECK(s->save_state() == "{}");
        s->load_state("{}");
        CHECK(s->save_state() == "{}");
    }
}
