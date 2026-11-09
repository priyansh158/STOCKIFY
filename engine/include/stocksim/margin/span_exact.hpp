// Exact SPAN of one combined commodity (MEZ 4.10), the engine's twin of stockify/fno/span_exact.py.
//
// The oracle computes in Python Decimal under a context that traps Inexact; here every amount is a `SpanDecimal`,
// an exact decimal (Int128 coefficient × 10^exponent) parsed digit by digit from the bundle's decimal strings.
// Nothing is ever rounded: a sum or product is exact or it is std::overflow_error (the run fails, never a silent
// change of step); a quotient that does not terminate is a SpanCalcError, as Inexact is in the oracle. The one
// rounding is `span_paise` / `rupees_up_paise`: up to the whole rupee, then × 100, int64-checked.
//
// Composition (validated against Kite 405/405, do not change): scan risk summed per scan tier, each tier floored
// at 0; the calendar spread charge; inter-commodity credit 0; SOM (GROSS); long / short option value; and
// span = max(scan + intra - inter - LOV + SOV, SOM, 0).
#pragma once

#include <compare>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/json_bundle.hpp"
#include "stocksim/types.hpp"

namespace stocksim::span {

// The SPAN parameters cannot be applied to this book exactly (the oracle's SpanCalcError): the staircase steps
// down; span_exact lets it fail the run. A RunError, so it surfaces like any other run failure.
class SpanCalcError : public RunError {
 public:
    explicit SpanCalcError(const std::string& what) : RunError(what) {}
};

// An exact decimal: coefficient × 10^exponent, kept normalised (no trailing zeros in the coefficient; zero is
// 0 × 10^0), so equal values have equal representations. Coefficients are Int128 (about 38 digits); a result
// beyond that is std::overflow_error. The oracle's context holds 50 digits, so any value both can represent is
// computed identically; realistic books stay below 25 digits (units ≤ 1e9 × rupees ≤ 1e7 with ≤ 6 decimals).
class SpanDecimal {
 public:
    constexpr SpanDecimal() = default;
    explicit SpanDecimal(std::int64_t v) : coef_(v) { normalise(); }

    // Python Decimal(text) for ASCII text: surrounding whitespace, an optional sign, digits with an optional
    // fraction (".5", "5."), an optional exponent, single '_' between digits. nullopt for anything that is not a
    // finite decimal (NaN, Infinity, garbage). An exponent beyond ±10^6 is std::overflow_error.
    [[nodiscard]] static std::optional<SpanDecimal> parse(std::string_view text);

    [[nodiscard]] const Int128& coefficient() const { return coef_; }
    [[nodiscard]] std::int32_t exponent() const { return exp_; }
    [[nodiscard]] bool is_zero() const { return coef_.is_zero(); }
    [[nodiscard]] bool negative() const { return coef_.negative(); }
    [[nodiscard]] int sign() const { return is_zero() ? 0 : (negative() ? -1 : 1); }

    [[nodiscard]] SpanDecimal operator-() const;
    [[nodiscard]] SpanDecimal abs() const { return negative() ? -*this : *this; }
    friend SpanDecimal operator+(const SpanDecimal& a, const SpanDecimal& b);
    friend SpanDecimal operator-(const SpanDecimal& a, const SpanDecimal& b) { return a + (-b); }
    friend SpanDecimal operator*(const SpanDecimal& a, const SpanDecimal& b);
    SpanDecimal& operator+=(const SpanDecimal& b) { return *this = *this + b; }
    SpanDecimal& operator-=(const SpanDecimal& b) { return *this = *this - b; }
    friend bool operator==(const SpanDecimal& a, const SpanDecimal& b) {
        return a.coef_ == b.coef_ && a.exp_ == b.exp_;
    }
    friend std::strong_ordering operator<=>(const SpanDecimal& a, const SpanDecimal& b);

    // a / b when the quotient is a terminating decimal of at most 50 significant digits (the oracle's
    // precision), else nullopt (the oracle's Inexact). b must not be zero.
    [[nodiscard]] static std::optional<SpanDecimal> divide_exact(const SpanDecimal& a, const SpanDecimal& b);

    // The smallest integer >= this (ROUND_CEILING to the unit), as Int128.
    [[nodiscard]] Int128 ceiling() const;
    // value × 10^power when that is an integer (nullopt otherwise); std::overflow_error beyond Int128.
    [[nodiscard]] std::optional<Int128> scaled_integer(int power) const;

    [[nodiscard]] std::string to_string() const;  // plain text, for messages

 private:
    void normalise();
    Int128 coef_ = 0;
    std::int32_t exp_ = 0;
};

// Rupees → int64 paise, rounded UP to the whole rupee (152,055.01 → 15,205,600). Negative is a SpanCalcError.
[[nodiscard]] Paise rupees_up_paise(const SpanDecimal& rupees);

// A SPAN contract key: (kind, expiry, strike paise | none). Futures have no strike.
struct ContractKey {
    std::string kind;    // FUT | CE | PE
    std::string expiry;  // YYYY-MM-DD
    std::optional<Paise> strike;
    friend auto operator<=>(const ContractKey&, const ContractKey&) = default;
    friend bool operator==(const ContractKey&, const ContractKey&) = default;
};
[[nodiscard]] std::string key_name(const ContractKey& key);  // "CE 2026-11-17 2000000", "FUT 2026-11-23"
[[nodiscard]] std::string key_repr(const ContractKey& key);  // Python's tuple repr: "('FUT', '2026-11-23', None)"

// The bundle's decimal strings are kept as JSON values and converted when the oracle converts them (lazily), so
// a malformed value the book never reads is never an error.
struct Contract {
    Json price, composite_delta;
    std::vector<Json> risk_array;
};
struct Tier {
    std::int64_t number = 0;
    std::optional<std::string> start, end;  // YYYY-MM-DD, or none (the whole commodity)
    Json rates;                              // {rate class: decimal string}
};
struct SpreadLeg {
    std::string expiry, side;
    Json weight;
};
struct CalendarSpread {
    std::int64_t number = 0;
    std::optional<std::string> charge_method;
    Json rates;
    std::vector<SpreadLeg> legs;
};
struct Commodity {
    std::string code;
    std::map<ContractKey, Contract> contracts;
    Json cvf;  // {pf type: decimal string}
    std::optional<std::string> som_method;
    std::vector<Tier> scan_tiers, som_tiers;
    std::vector<CalendarSpread> calendar_spreads;
};

// A bundle span commodity (A1 §6.2) as the calculation reads it (staircase.commodity_from_bundle).
[[nodiscard]] Commodity commodity_from_bundle(const std::string& code, const Json& d);

// Rupees, exact. With `missing` non-empty the amounts are meaningless (the oracle's NaN): callers step down.
struct SpanResult {
    SpanDecimal scan, intra_spread, inter_credit, som, long_option_value, short_option_value, span;
    std::vector<ContractKey> missing;
};

// The SPAN requirement of `legs` (contract key, signed units; repeated keys are netted) in `cc`.
[[nodiscard]] SpanResult span_requirement(const Commodity& cc, const std::vector<std::pair<ContractKey, Qty>>& legs);

// The whole SPAN requirement in paise (rupee_up); SpanCalcError when contracts are missing.
[[nodiscard]] Paise span_paise(const SpanResult& result);

}  // namespace stocksim::span
