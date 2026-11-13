// CH 6.1: the plain-English note a strategy attaches to an order, and the number text used in it. Twins of
// stockify.oracle.notes and stockify.strategy (fmt_price, fmt_qty, to_level); the results are byte-identical.
//
// clean_note, in order: each invalid UTF-8 sequence (a maximal ill-formed subpart, as the Unicode standard
// recommends; an encoded surrogate counts as invalid) becomes U+FFFD; each Cc character (U+0000-U+001F,
// U+007F-U+009F) and U+2028, U+2029 becomes one ASCII space; ASCII spaces are trimmed from both ends; past
// kNoteMax code points the first kNoteMax - 1 are kept and U+2026 is appended.
#pragma once

#include <cstddef>
#include <string>
#include <string_view>

#include "stocksim/types.hpp"

namespace stocksim {

inline constexpr std::size_t kNoteMax = 200;

std::string clean_note(std::string_view text);

// Paise as rupees: Indian grouping (last three digits, then pairs), two decimals, '-' for negatives.
std::string fmt_price(Paise paise);

// Units with Indian grouping, '-' for negatives.
std::string fmt_qty(Qty units);

// An indicator value times 10,000, rounded half to even (the default rounding mode). RunError when x is not
// finite or the result does not fit in an int64.
Paise to_level(double x);

}  // namespace stocksim
