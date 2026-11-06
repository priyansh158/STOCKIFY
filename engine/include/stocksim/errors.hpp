// Error types shared by the engine. A failure is always an exception with a message that says what and
// where; nothing is silently clamped, wrapped around or skipped (A4 §0.2, A1 §5).
#pragma once

#include <stdexcept>
#include <string>

namespace stocksim {

// A value cannot be represented exactly in the requested unit (the twin of stockify.numerics.NumericError).
class NumericError : public std::domain_error {
 public:
    explicit NumericError(const std::string& what) : std::domain_error(what) {}
};

// An input file (bundle, event pack) is malformed or has an unsupported version.
class FormatError : public std::runtime_error {
 public:
    explicit FormatError(const std::string& what) : std::runtime_error(what) {}
};

}  // namespace stocksim
