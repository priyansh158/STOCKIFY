// Indicators (A2 §4.3), identical in definition to stockify.strategy's: inputs are paise; arithmetic in double in
// exactly the oracle's expression order (A4 §0.3); NaN until there are enough samples (A3 U-05).
#pragma once

#include <array>
#include <vector>

#include "stocksim/types.hpp"

namespace stocksim {

double sma(const std::vector<Paise>& values, std::int64_t n);
double ema(const std::vector<Paise>& values, std::int64_t n);
double rsi(const std::vector<Paise>& values, std::int64_t n);
double atr(const std::vector<std::array<Paise, 3>>& hlc, std::int64_t n);  // (high, low, close) in order

}  // namespace stocksim
