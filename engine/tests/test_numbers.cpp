// A4 §0: integer helpers and exact decimals. Random cross-checks against Python live in
// tests/stockify/test_engine_foundations.py (through stockify._engine.testing).
#include <stdexcept>

#include <ostream>  // doctest prints string_view through operator<<

#include "doctest.h"
#include "stocksim/decimal.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/types.hpp"

using namespace stocksim;

TEST_CASE("floor_div and ceil_div are mathematical for every sign") {
    CHECK(floor_div(7, 2) == 3);
    CHECK(floor_div(-7, 2) == -4);
    CHECK(floor_div(7, -2) == -4);
    CHECK(floor_div(-7, -2) == 3);
    CHECK(floor_div(-6, 2) == -3);
    CHECK(ceil_div(7, 2) == 4);
    CHECK(ceil_div(-7, 2) == -3);
    CHECK(ceil_div(7, -2) == -3);
    CHECK(ceil_div(-7, -2) == 4);
    CHECK(floor_div(kInt64Min, 1) == kInt64Min);
    CHECK_THROWS_AS(floor_div(kInt64Min, -1), std::overflow_error);
    CHECK_THROWS_AS(ceil_div(kInt64Min, -1), std::overflow_error);
    CHECK_THROWS_AS(floor_div(1, 0), NumericError);
}

TEST_CASE("round_half_up_div rounds halves up and checks its domain") {
    CHECK(round_half_up_div(5, 2) == 3);
    CHECK(round_half_up_div(4, 2) == 2);
    CHECK(round_half_up_div(149, 100) == 1);
    CHECK(round_half_up_div(150, 100) == 2);
    CHECK(round_half_up_div(kInt64Max, 2) == kInt64Max / 2 + 1);  // no overflow on the way
    CHECK_THROWS_AS(round_half_up_div(-1, 2), NumericError);
    CHECK_THROWS_AS(round_half_up_div(5, 3), NumericError);
    CHECK_THROWS_AS(round_half_up_div(5, 0), NumericError);
}

TEST_CASE("floor_to and ceil_to snap to a step") {
    CHECK(floor_to(1234, 5) == 1230);
    CHECK(ceil_to(1231, 5) == 1235);
    CHECK(floor_to(-1231, 5) == -1235);
    CHECK(ceil_to(-1231, 5) == -1230);
    CHECK_THROWS_AS(ceil_to(kInt64Max, 10), std::overflow_error);
    CHECK_THROWS_AS(floor_to(kInt64Min, 10), std::overflow_error);
    // Only the final result must fit: floor_div(INT64_MIN, -1) overflows, but INT64_MIN is already a multiple.
    CHECK(floor_to(kInt64Min, -1) == kInt64Min);
    CHECK(ceil_to(kInt64Min, -1) == kInt64Min);
    CHECK(floor_mod(-7, 2) == 1);
    CHECK(floor_mod(7, -2) == -1);
    CHECK(floor_mod(kInt64Min, -1) == 0);
}

TEST_CASE("mul_div forms the product in 128 bits") {
    CHECK(mul_div_floor(kInt64Max, kInt64Max, kInt64Max) == kInt64Max);
    CHECK(mul_div_floor(kInt64Min, 2, 2) == kInt64Min);
    CHECK(mul_div_floor(-7, 3, 2) == -11);
    CHECK(mul_div_floor(7, -3, 2) == -11);
    CHECK(mul_div_floor(-7, -3, 2) == 10);
    CHECK(mul_div_floor(7, 3, -2) == -11);
    CHECK(mul_div_floor(0, kInt64Min, -1) == 0);
    CHECK_THROWS_AS(mul_div_floor(kInt64Max, kInt64Max, 1), std::overflow_error);
    CHECK_THROWS_AS(mul_div_floor(kInt64Min, -1, 1), std::overflow_error);
    CHECK_THROWS_AS(mul_div_floor(1, 1, 0), NumericError);
}

TEST_CASE("the half-paisa charge cases of A3 U-06") {
    // Stamp duty on a ₹900 CNC buy at 0.015 %: 13.5 → 14 paise. MIS brokerage on ₹1,950 at 0.03 %: 58.5 → 59.
    CHECK(mul_div_round_half_up(90'000, 150'000, kPpb) == 14);
    CHECK(mul_div_round_half_up(195'000, 300'000, kPpb) == 59);
    CHECK(mul_div_round_half_up(0, -5, 2) == 0);
    CHECK_THROWS_AS(mul_div_round_half_up(-1, 5, 2), NumericError);
    CHECK_THROWS_AS(mul_div_round_half_up(kInt64Max, kInt64Max, 2), std::overflow_error);
}

TEST_CASE("checked arithmetic refuses to wrap around") {
    CHECK(checked_add(kInt64Max - 1, 1) == kInt64Max);
    CHECK_THROWS_AS(checked_add(kInt64Max, 1), std::overflow_error);
    CHECK_THROWS_AS(checked_sub(kInt64Min, 1), std::overflow_error);
    CHECK(checked_mul(-3'037'000'499, 3'037'000'499) == -9'223'372'030'926'249'001);
    CHECK_THROWS_AS(checked_mul(kInt64Min, -1), std::overflow_error);
}

TEST_CASE("decimal strings convert exactly (A4 §0.1 examples)") {
    CHECK(to_ppb("0.0015") == 1'500'000);   // 0.15 %
    CHECK(to_ppb("0.0000183") == 18'300);   // 0.00183 %
    CHECK(to_ppb("0.000001") == 1'000);     // ₹10 per crore
    CHECK(to_ppb("0.0000001") == 100);
    CHECK(to_ppb("0.18") == 180'000'000);   // GST 18 %
    CHECK(to_ppm("0.1") == 100'000);
    CHECK(to_bp("0.02") == 200);
    CHECK(to_bp("0.005") == 50);
    CHECK(to_paise("1234.50") == 123'450);
    CHECK(to_paise(" -0.05 ") == -5);
    CHECK(to_paise("+.5") == 50);
    CHECK(to_paise("5.") == 500);
    CHECK(to_paise("1.5e3") == 150'000);
    CHECK(to_paise("12E-2") == 12);
    CHECK(to_paise("-0") == 0);
    CHECK(to_paise("0e999999") == 0);
    CHECK(decimal_scaled("-9223372036854775808", 0, "units") == kInt64Min);
    CHECK(decimal_scaled("9223372036854775807", 0, "units") == kInt64Max);
}

TEST_CASE("decimal strings that are not exact are errors, never roundings") {
    CHECK_THROWS_AS(to_paise("0.001"), NumericError);
    CHECK_THROWS_AS(to_ppb("1.0000000000000000000000000001"), NumericError);  // 29 significant digits
    CHECK_THROWS_AS(to_paise("1e-999999"), NumericError);
    CHECK_THROWS_AS(decimal_scaled("9223372036854775808", 0, "units"), std::overflow_error);
    CHECK_THROWS_AS(to_paise("1e400"), std::overflow_error);
    for (const char* bad : {"", " ", "+", "-", ".", "e5", "1e", "1e+", "1.2.3", "1,5", "inf", "nan", "0x10", "1_000",
                            "--1", "1 2"}) {
        CAPTURE(bad);
        CHECK_THROWS_AS(to_paise(bad), NumericError);
    }
}

TEST_CASE("enum spellings round-trip and match the oracle's artifacts") {
    CHECK(to_string(OrderType::SLM) == "SL-M");
    CHECK(to_string(OrderStatus::TriggerPending) == "TRIGGER_PENDING");
    CHECK(to_string(Reason::None).empty());
    CHECK(to_string(Reason::QtyLot) == "R_QTY_LOT");
    CHECK(parse_order_type("SL-M") == OrderType::SLM);
    CHECK(parse_reason("R_NO_REFERENCE") == Reason::NoReference);
    CHECK(parse_reason("") == Reason::None);
    CHECK_FALSE(parse_side("buy").has_value());
    CHECK(parse_product("NRML") == Product::NRML);
    CHECK(parse_validity("IOC") == Validity::IOC);
    CHECK(parse_order_status("SLICED") == OrderStatus::Sliced);
}
