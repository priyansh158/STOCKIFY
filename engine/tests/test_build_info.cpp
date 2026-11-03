#include <cctype>

#include "doctest.h"
#include "stocksim/build_info.hpp"

TEST_CASE("engine_build_id is 12 lowercase hex characters") {
    const auto& id = stocksim::build_info().engine_build_id;
    REQUIRE(id.size() == 12);
    for (char c : id) {
        CHECK((std::isdigit(static_cast<unsigned char>(c)) || (c >= 'a' && c <= 'f')));
    }
}

TEST_CASE("build info names the compiler and C++ standard") {
    const auto& b = stocksim::build_info();
    CHECK(b.compiler_id == "MSVC");
    CHECK(b.cxx_standard == "20");
    CHECK_FALSE(b.python_version.empty());
}
