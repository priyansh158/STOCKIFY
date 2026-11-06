#include "stocksim/decimal.hpp"

#include <limits>
#include <stdexcept>
#include <string>

#include "stocksim/errors.hpp"

namespace stocksim {

namespace {

bool is_space(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v'; }

bool is_digit(char c) { return c >= '0' && c <= '9'; }

[[noreturn]] void not_a_decimal(std::string_view text) {
    throw NumericError("not a decimal: '" + std::string(text) + "'");
}

}  // namespace

std::int64_t decimal_scaled(std::string_view text, int scale_exponent, std::string_view unit) {
    const std::string_view original = text;
    while (!text.empty() && is_space(text.front())) {
        text.remove_prefix(1);
    }
    while (!text.empty() && is_space(text.back())) {
        text.remove_suffix(1);
    }
    std::size_t i = 0;
    bool negative = false;
    if (i < text.size() && (text[i] == '+' || text[i] == '-')) {
        negative = text[i] == '-';
        ++i;
    }

    // Significant digits (leading zeros dropped) and the power of ten they are scaled by.
    std::string digits;
    long long exponent = 0;
    std::size_t mantissa_digits = 0;
    while (i < text.size() && is_digit(text[i])) {
        if (!(digits.empty() && text[i] == '0')) {
            digits.push_back(text[i]);
        }
        ++mantissa_digits;
        ++i;
    }
    if (i < text.size() && text[i] == '.') {
        ++i;
        while (i < text.size() && is_digit(text[i])) {
            if (!(digits.empty() && text[i] == '0')) {
                digits.push_back(text[i]);
            }
            --exponent;
            ++mantissa_digits;
            ++i;
        }
    }
    if (mantissa_digits == 0) {
        not_a_decimal(original);  // "", "+", ".", "inf", "nan", ...
    }
    if (i < text.size() && (text[i] == 'e' || text[i] == 'E')) {
        ++i;
        bool exp_negative = false;
        if (i < text.size() && (text[i] == '+' || text[i] == '-')) {
            exp_negative = text[i] == '-';
            ++i;
        }
        if (i >= text.size() || !is_digit(text[i])) {
            not_a_decimal(original);
        }
        long long e = 0;
        while (i < text.size() && is_digit(text[i])) {
            if (e < 1'000'000'000) {  // saturate: anything this large is overflow or zero anyway
                e = e * 10 + (text[i] - '0');
            }
            ++i;
        }
        exponent += exp_negative ? -e : e;
    }
    if (i != text.size()) {
        not_a_decimal(original);
    }
    if (digits.empty()) {
        return 0;  // any zero, whatever its exponent
    }

    // value = digits * 10^(exponent + scale_exponent); the fractional digits must all be zero.
    long long shift = exponent + scale_exponent;
    while (shift < 0 && !digits.empty() && digits.back() == '0') {
        digits.pop_back();
        ++shift;
    }
    if (shift < 0) {
        throw NumericError("'" + std::string(original) + "' is not a whole number of " + std::string(unit));
    }
    // An int64 has at most 19 digits; anything longer cannot fit.
    if (static_cast<long long>(digits.size()) + shift > 19) {
        throw std::overflow_error("'" + std::string(original) + "' does not fit in int64 " + std::string(unit));
    }
    digits.append(static_cast<std::size_t>(shift), '0');

    // Accumulate as a negative number so that INT64_MIN is representable.
    constexpr std::int64_t min = std::numeric_limits<std::int64_t>::min();
    std::int64_t acc = 0;
    for (char c : digits) {
        const int d = c - '0';
        if (acc < (min + d) / 10) {
            throw std::overflow_error("'" + std::string(original) + "' does not fit in int64 " + std::string(unit));
        }
        acc = acc * 10 - d;
    }
    if (!negative) {
        if (acc == min) {
            throw std::overflow_error("'" + std::string(original) + "' does not fit in int64 " + std::string(unit));
        }
        return -acc;
    }
    return acc;
}

}  // namespace stocksim
