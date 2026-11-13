// A4 §9.2: float format, canonical JSON, CSV cells, SHA-256. The 10^6-double comparison with Python's repr
// (A3 U-11) is in tests/stockify/test_engine_foundations.py.
#include <cmath>
#include <limits>
#include <string>

#include <ostream>  // doctest prints string_view through operator<<

#include "doctest.h"
#include "stocksim/engine.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/format.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/json_out.hpp"
#include "stocksim/sha256.hpp"

using namespace stocksim;

TEST_CASE("format_float lays digits out like Python's repr") {
    // Expected strings are Python 3.12 repr() of the same doubles.
    CHECK(format_float(0.0) == "0.0");
    CHECK(format_float(-0.0) == "-0.0");
    CHECK(format_float(1.0) == "1.0");
    CHECK(format_float(0.1) == "0.1");
    CHECK(format_float(1e16) == "1e+16");
    CHECK(format_float(1e-4) == "0.0001");
    CHECK(format_float(1e-5) == "1e-05");
    CHECK(format_float(5e-324) == "5e-324");
    CHECK(format_float(1.7976931348623157e308) == "1.7976931348623157e+308");
    CHECK(format_float(123456789012345680.0) == "1.2345678901234568e+17");
    CHECK(format_float(9999999999999998.0) == "9999999999999998.0");
    CHECK(format_float(0.1 + 0.2) == "0.30000000000000004");
    CHECK(format_float(-2.5e-7) == "-2.5e-07");
    CHECK(format_float(1234.5) == "1234.5");
    CHECK(format_float(100.0) == "100.0");
    CHECK(format_float(1e15) == "1000000000000000.0");
    CHECK(format_float(2.2250738585072014e-308) == "2.2250738585072014e-308");
    CHECK_THROWS_AS(format_float(std::numeric_limits<double>::infinity()), NumericError);
    CHECK_THROWS_AS(format_float(std::nan("")), NumericError);
}

TEST_CASE("dumps_canonical sorts keys, has no spaces and escapes like the oracle") {
    JValue v = JValue::object();
    v["b"] = JValue::Array{JValue(1), JValue(2.5), JValue(), JValue(true)};
    v["a"] = std::string("x\"y\\z\n\t\x01\xc3\xa9");  // é as UTF-8 passes through unescaped
    JValue c = JValue::object();
    c["z"] = 0.0001;
    c["y"] = -1e-5;
    v["c"] = c;
    CHECK(dumps_canonical(v) ==
          "{\"a\":\"x\\\"y\\\\z\\n\\t\\u0001\xc3\xa9\",\"b\":[1,2.5,null,true],\"c\":{\"y\":-1e-05,\"z\":0.0001}}");
    CHECK(dumps_canonical(JValue()) == "null");
    CHECK(dumps_canonical(JValue::object()) == "{}");
    CHECK(dumps_canonical(JValue::array()) == "[]");
    CHECK(dumps_canonical(JValue(std::int64_t{-9'223'372'036'854'775'807} - 1)) == "-9223372036854775808");
}

TEST_CASE("CSV cells and rows match stockify.oracle.artifacts._csv") {
    CHECK(csv_cell(CsvNull{}).empty());
    CHECK(csv_cell(true) == "true");
    CHECK(csv_cell(std::int64_t{-42}) == "-42");
    CHECK(csv_cell(std::string_view("entry")) == "entry");
    CHECK(csv_cell(std::string_view("a,b")) == "\"a,b\"");
    CHECK(csv_cell(std::string_view("a\rb")) == "\"a\rb\"");  // M-4: a CR splits a row for a universal-newline reader
    CHECK(csv_cell(std::string_view("say \"hi\"")) == "\"say \"\"hi\"\"\"");
    CHECK(csv_cell(std::string_view("line\nbreak")) == "\"line\nbreak\"");
    CHECK(csv_cell(std::string_view("cr\rstays")) == "\"cr\rstays\"");  // , " CR and LF are quoted
    CsvWriter w("a,b,c");
    CHECK(w.text() == "a,b,c\n");
    w.add_row({std::int64_t{1}, std::string_view("BUY"), CsvNull{}});
    w.add_row({std::int64_t{2}, std::string_view("x,y"), false});
    CHECK(w.text() == "a,b,c\n1,BUY,\n2,\"x,y\",false\n");
    CHECK(w.rows() == 2);
}

TEST_CASE("SHA-256 of known inputs") {
    CHECK(sha256_hex(std::string_view("")) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    CHECK(sha256_hex(std::string_view("abc")) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
}

// CH 6.4: the plot buffer's rules on an engine that has not run (time 0): the cap, the kinds, the names, last-wins.
// The 200,000-point cap needs 200,000 distinct times; tests/stockify/test_plot.py runs it on both runners.
TEST_CASE("ctx.plot: caps, kinds, names and last-wins") {
    Engine e(load_bundle(std::string(STOCKSIM_FIXTURES_DIR) + "/sod_state/day1_bundle.json"));
    Context& c = e.context();
    static_assert(kPlotMaxSeries == 8 && kPlotMaxPoints == 200'000);
    SUBCASE("a repeat at the same time replaces, and the series keeps its first kind") {
        c.plot("s", 1);
        c.plot("s", 2);
        CHECK(e.plot_points().size() == 1);
        CHECK(e.plot_points().begin()->second == 2);
        CHECK_THROWS_WITH_AS(c.plot("s", 3, "level"), doctest::Contains("is kind \"price\", not \"level\""),
                             std::invalid_argument);
        c.plot("s", 4, "price", 0);
        CHECK(e.plot_points().begin()->second == 4);
    }
    SUBCASE("the ninth series is dropped and one line says so, however many calls") {
        for (int i = 0; i < 1000; ++i) {
            c.plot("s" + std::to_string(i % 12), i);
        }
        CHECK(e.plot_kinds().size() == 8);
        CHECK(e.plot_points().size() == 8);
        int lines = 0;
        for (const std::string& l : e.log_lines()) {
            lines += l.find("plot limit") != std::string::npos ? 1 : 0;
        }
        CHECK(lines == 1);
        CHECK(e.log_lines().back() == "0 WARN plot limit reached: more than 8 series");
    }
    SUBCASE("names and kinds are checked, kind first") {
        CHECK_THROWS_AS(c.plot("", 1), std::invalid_argument);
        CHECK_THROWS_AS(c.plot(std::string(33, 'x'), 1), std::invalid_argument);
        CHECK_NOTHROW(c.plot(std::string(32, 'x'), 1));
        CHECK_NOTHROW(c.plot("Ã©Ã©", 1));  // two code points, four bytes
        CHECK_THROWS_AS(c.plot(" pad", 1), std::invalid_argument);
        CHECK_THROWS_AS(c.plot("a
b", 1), std::invalid_argument);
        CHECK_THROWS_AS(c.plot("aÿ", 1), std::invalid_argument);  // not UTF-8
        CHECK_THROWS_WITH_AS(c.plot("", 1, "volume"), doctest::Contains("kind must be"), std::invalid_argument);
        CHECK_THROWS_WITH_AS(c.plot("a", 1, "price", 4'000'000'000), doctest::Contains("is not in this run's bundle"),
                             std::invalid_argument);
        CHECK_THROWS_AS(c.plot("a", 1, "price", -1), std::invalid_argument);
    }
}
