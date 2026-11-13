// CH 6.1: clean_note, fmt_price, fmt_qty, to_level (the twins of stockify.oracle.notes and stockify.strategy).
#include <cmath>
#include <cstdint>
#include <limits>
#include <string>

#include "doctest.h"
#include "stocksim/errors.hpp"
#include "stocksim/notes.hpp"

using stocksim::clean_note;
using stocksim::fmt_price;
using stocksim::fmt_qty;
using stocksim::to_level;

namespace {
constexpr const char* kFffd = "\xEF\xBF\xBD";
constexpr const char* kEllipsis = "\xE2\x80\xA6";
}  // namespace

TEST_CASE("clean_note hand cases") {
    CHECK(stocksim::kNoteMax == 200);
    CHECK(clean_note("") == "");
    CHECK(clean_note("  x  ") == "x");
    CHECK(clean_note("a\tb\nc") == "a b c");
    CHECK(clean_note(std::string("a\0b\x7f" "c", 5)) == "a b c");
    CHECK(clean_note("a\xC2\x85" "d") == "a d");                          // U+0085
    CHECK(clean_note("a\xE2\x80\xA8" "b\xE2\x80\xA9" "c") == "a b c");    // U+2028, U+2029
    CHECK(clean_note("a  b") == "a  b");
    CHECK(clean_note("\xC2\xA0x\xC2\xA0") == "\xC2\xA0x\xC2\xA0");        // NBSP is not trimmed
    CHECK(clean_note("\n\tx\r\n") == "x");
    CHECK(clean_note(std::string(200, 'x')) == std::string(200, 'x'));
    CHECK(clean_note(std::string(201, 'x')) == std::string(199, 'x') + kEllipsis);
}

TEST_CASE("clean_note removes bidi controls outright (CH 6.25)") {
    CHECK(clean_note("aâ®" "b") == "ab");                       // U+202E
    CHECK(clean_note("aâ¦" "bâ©" "c") == "abc");    // U+2066, U+2069
    CHECK(clean_note("â x â") == "x");           // U+200E, U+200F, then trimmed
    CHECK(clean_note("aØ" "b") == "ab");                           // U+061C
    CHECK(clean_note("âªâ«â¬â­") == "");
    CHECK(clean_note("=1+1") == "=1+1");
    CHECK(clean_note("â®" + std::string(201, 'x')) == std::string(199, 'x') + kEllipsis);
}

TEST_CASE("clean_note replaces invalid UTF-8 with U+FFFD") {
    CHECK(clean_note("\xC3(") == std::string(kFffd) + "(");
    CHECK(clean_note("a\xFF\xFE" "b") == std::string("a") + kFffd + kFffd + "b");
    CHECK(clean_note("a\xED\xA0\x80" "b") == std::string("a") + kFffd + kFffd + kFffd + "b");  // an encoded surrogate
    CHECK(clean_note("\xE2\x82") == std::string(kFffd));                                       // truncated sequence
    CHECK(clean_note("\xC0\x80") == std::string(kFffd) + kFffd);                               // overlong
}

TEST_CASE("clean_note counts code points, not bytes") {
    std::string emoji;
    for (int i = 0; i < 500; ++i) emoji += "\xF0\x9F\x98\x80";
    const std::string out = clean_note(emoji);
    CHECK(out.size() == 199 * 4 + 3);
    CHECK(out.substr(out.size() - 3) == kEllipsis);
}

TEST_CASE("fmt_price and fmt_qty hand cases") {
    CHECK(fmt_price(241840) == "2,418.40");
    CHECK(fmt_price(123456780) == "12,34,567.80");
    CHECK(fmt_price(-5) == "-0.05");
    CHECK(fmt_price(0) == "0.00");
    CHECK(fmt_price(5) == "0.05");
    CHECK(fmt_price(-123456780) == "-12,34,567.80");
    CHECK(fmt_price(99999) == "999.99");
    CHECK(fmt_price(100000) == "1,000.00");
    CHECK(fmt_price(std::numeric_limits<std::int64_t>::max()) == "92,23,37,20,36,85,47,758.07");
    CHECK(fmt_price(std::numeric_limits<std::int64_t>::min()) == "-92,23,37,20,36,85,47,758.08");
    CHECK(fmt_qty(125000) == "1,25,000");
    CHECK(fmt_qty(0) == "0");
    CHECK(fmt_qty(999) == "999");
    CHECK(fmt_qty(1000) == "1,000");
    CHECK(fmt_qty(-125000) == "-1,25,000");
    CHECK(fmt_qty(std::numeric_limits<std::int64_t>::min()) == "-92,23,37,20,36,85,47,75,808");
}

TEST_CASE("to_level scales by 10,000 and rounds half to even") {
    CHECK(to_level(54.3217) == 543217);
    CHECK(to_level(0.0) == 0);
    CHECK(to_level(-1.5) == -15000);
    CHECK(to_level(0.00005) == 0);
    CHECK(to_level(0.00025) == 2);
    CHECK_THROWS_AS(to_level(std::numeric_limits<double>::quiet_NaN()), stocksim::RunError);
    CHECK_THROWS_AS(to_level(std::numeric_limits<double>::infinity()), stocksim::RunError);
    CHECK_THROWS_AS(to_level(1e300), stocksim::RunError);
}
