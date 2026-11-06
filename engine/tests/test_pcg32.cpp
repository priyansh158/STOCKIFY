// A4 §4.5 / A3 P-01: PCG32 against the published pcg_basic reference sequence and the Python port.
#include <ostream>  // doctest prints string_view through operator<<

#include "doctest.h"
#include "stocksim/errors.hpp"
#include "stocksim/pcg32.hpp"

using stocksim::Pcg32;

TEST_CASE("pcg32_srandom(42, 54) reproduces the pcg_basic demo sequence") {
    Pcg32 r(42, 54);
    const std::uint32_t expected[] = {0xa15c02b7, 0x7b47f409, 0xba1d3330, 0x83d2f293, 0xbfa4784b, 0xcbed606e};
    for (std::uint32_t e : expected) {
        CHECK(r.next_u32() == e);
    }
}

TEST_CASE("below, random and next_u64 match stockify.oracle.rng for seed 42, stream 1") {
    Pcg32 r(42, stocksim::kSlippageStream);
    const std::uint32_t expected[] = {1, 2, 4, 9, 6};
    for (std::uint32_t e : expected) {
        CHECK(r.below(10) == e);
    }
    CHECK(r.random() == 0.41793660273021593);
    CHECK(r.next_u64() == 8327088744064588887ULL);
}

TEST_CASE("below(1) is always 0 and below(0) is an error") {
    Pcg32 r(7, 2);
    for (int i = 0; i < 100; ++i) {
        CHECK(r.below(1) == 0);
    }
    CHECK_THROWS_AS(r.below(0), stocksim::NumericError);
}

TEST_CASE("a copy continues the same sequence independently") {
    Pcg32 a(123, 2);
    a.next_u32();
    Pcg32 b = a;
    for (int i = 0; i < 50; ++i) {
        CHECK(a.next_u32() == b.next_u32());
    }
}
