// Exact decimal-string conversion (A4 §0.1), the twin of stockify.numerics.to_ppb / to_ppm / to_bp / to_paise.
//
// Text is parsed digit by digit, never via double. Accepted: optional surrounding whitespace, an optional sign,
// digits with an optional fractional part (".5" and "5." included), an optional exponent ("1.5e-3").
// A value that is not a whole number of the target unit is a NumericError; one outside int64 is
// std::overflow_error. NaN, infinities and anything else that is not a finite decimal are NumericErrors.
#pragma once

#include <cstdint>
#include <string_view>

namespace stocksim {

// value * 10^scale_exponent, which must be an integer that fits in int64.
std::int64_t decimal_scaled(std::string_view text, int scale_exponent, std::string_view unit);

inline std::int64_t to_ppb(std::string_view fraction) { return decimal_scaled(fraction, 9, "ppb"); }
inline std::int64_t to_ppm(std::string_view fraction) { return decimal_scaled(fraction, 6, "ppm"); }
inline std::int64_t to_bp(std::string_view fraction) { return decimal_scaled(fraction, 4, "bp"); }
inline std::int64_t to_paise(std::string_view rupees) { return decimal_scaled(rupees, 2, "paise"); }

}  // namespace stocksim
