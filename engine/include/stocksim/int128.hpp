// Integer helpers of A4 §0.2, identical to stockify.numerics (Python plain int).
//
// Products are formed in 128 bits with the MSVC x64 intrinsics (_umul128 / _udiv128; Windows only, D-26).
// Division is mathematical floor/ceiling for any sign, never C++ truncation. A result that does not fit in
// int64 is an error (std::overflow_error), never a wraparound; a zero divisor or an argument outside a
// helper's domain is a NumericError.
#pragma once

#include <intrin.h>

#include <cmath>
#include <compare>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>

#include "stocksim/errors.hpp"

namespace stocksim {

inline constexpr std::int64_t kInt64Min = std::numeric_limits<std::int64_t>::min();
inline constexpr std::int64_t kInt64Max = std::numeric_limits<std::int64_t>::max();

namespace detail {

inline std::uint64_t magnitude(std::int64_t v) {
    // |INT64_MIN| = 2^63 is representable as uint64.
    return v < 0 ? (~static_cast<std::uint64_t>(v) + 1u) : static_cast<std::uint64_t>(v);
}

[[noreturn]] inline void overflow(const char* what) { throw std::overflow_error(std::string(what) + ": int64 overflow"); }

[[noreturn]] inline void zero_divisor(const char* what) { throw NumericError(std::string(what) + " by zero"); }

// A non-negative magnitude q with a sign, back to int64; q must be <= 2^63 when negative, <= 2^63-1 otherwise.
inline std::int64_t apply_sign(std::uint64_t q, bool negative, const char* what) {
    constexpr std::uint64_t two63 = std::uint64_t{1} << 63;
    if (negative) {
        if (q > two63) {
            overflow(what);
        }
        return q == two63 ? kInt64Min : -static_cast<std::int64_t>(q);
    }
    if (q >= two63) {
        overflow(what);
    }
    return static_cast<std::int64_t>(q);
}

// (hi:lo) / d for d > 0 with quotient and remainder; the quotient must fit in 64 bits.
inline std::uint64_t udiv128(std::uint64_t hi, std::uint64_t lo, std::uint64_t d, std::uint64_t& rem,
                             const char* what) {
    if (hi >= d) {
        overflow(what);  // the quotient would need more than 64 bits (and _udiv128 would fault)
    }
    return _udiv128(hi, lo, d, &rem);
}

}  // namespace detail

// ── Checked int64 arithmetic ─────────────────────────────────────────────────

inline std::int64_t checked_add(std::int64_t a, std::int64_t b) {
    if ((b > 0 && a > kInt64Max - b) || (b < 0 && a < kInt64Min - b)) {
        detail::overflow("checked_add");
    }
    return a + b;
}

inline std::int64_t checked_sub(std::int64_t a, std::int64_t b) {
    if ((b < 0 && a > kInt64Max + b) || (b > 0 && a < kInt64Min + b)) {
        detail::overflow("checked_sub");
    }
    return a - b;
}

// -a; INT64_MIN has no negation in int64.
inline std::int64_t checked_neg(std::int64_t a) {
    if (a == kInt64Min) {
        detail::overflow("checked_neg");
    }
    return -a;
}

// A double to int64 by truncation (the caller applies ceil/floor first): NaN is a NumericError (the oracle's
// ValueError from int(nan)); a value outside int64, infinity included, is an overflow (OverflowError).
inline std::int64_t checked_trunc(double x) {
    if (std::isnan(x)) {
        throw NumericError("cannot convert float NaN to int64");
    }
    if (!(x >= -9223372036854775808.0 && x < 9223372036854775808.0)) {
        detail::overflow("checked_trunc");
    }
    return static_cast<std::int64_t>(x);
}

inline std::int64_t checked_mul(std::int64_t a, std::int64_t b) {
    std::int64_t hi = 0;
    const std::int64_t lo = _mul128(a, b, &hi);
    if (hi != (lo < 0 ? -1 : 0)) {
        detail::overflow("checked_mul");
    }
    return lo;
}

// ── A4 §0.2 ──────────────────────────────────────────────────────────────────

// Mathematical floor of a / d for any sign.
inline std::int64_t floor_div(std::int64_t a, std::int64_t d) {
    if (d == 0) {
        detail::zero_divisor("floor_div");
    }
    if (a == kInt64Min && d == -1) {
        detail::overflow("floor_div");
    }
    std::int64_t q = a / d;
    const std::int64_t r = a % d;
    if (r != 0 && ((r < 0) != (d < 0))) {
        --q;
    }
    return q;
}

// Mathematical ceiling of a / d for any sign.
inline std::int64_t ceil_div(std::int64_t a, std::int64_t d) {
    if (d == 0) {
        detail::zero_divisor("ceil_div");
    }
    if (a == kInt64Min && d == -1) {
        detail::overflow("ceil_div");
    }
    std::int64_t q = a / d;
    const std::int64_t r = a % d;
    if (r != 0 && ((r < 0) == (d < 0))) {
        ++q;
    }
    return q;
}

// (a + d/2) / d for a >= 0 and an even d > 0. Computed as q + (r >= d/2), which cannot overflow.
inline std::int64_t round_half_up_div(std::int64_t a, std::int64_t d) {
    if (a < 0 || d <= 0 || d % 2 != 0) {
        throw NumericError("round_half_up_div needs a >= 0 and an even d > 0, got " + std::to_string(a) + ", " +
                           std::to_string(d));
    }
    const std::int64_t q = a / d;
    const std::int64_t r = a % d;
    return r >= d / 2 ? q + 1 : q;
}

// Python's x % d: the remainder takes the divisor's sign. Never overflows (INT64_MIN % -1 is 0).
inline std::int64_t floor_mod(std::int64_t x, std::int64_t d) {
    if (d == 0) {
        detail::zero_divisor("floor_mod");
    }
    if (d == -1) {
        return 0;
    }
    std::int64_t r = x % d;
    if (r != 0 && ((r < 0) != (d < 0))) {
        r += d;
    }
    return r;
}

// floor_div(x, step) * step, computed as x - floor_mod(x, step) so that only a final result outside int64 is an
// overflow (floor_to(INT64_MIN, -1) is INT64_MIN, as in Python).
inline std::int64_t floor_to(std::int64_t x, std::int64_t step) { return checked_sub(x, floor_mod(x, step)); }

// ceil_div(x, step) * step = x + ((-x) mod step), where (-x) mod step is 0 or step - (x mod step).
inline std::int64_t ceil_to(std::int64_t x, std::int64_t step) {
    const std::int64_t m = floor_mod(x, step);
    return m == 0 ? x : checked_add(x, step - m);
}

// floor(a * b / d) with the product formed exactly in 128 bits.
inline std::int64_t mul_div_floor(std::int64_t a, std::int64_t b, std::int64_t d) {
    if (d == 0) {
        detail::zero_divisor("mul_div_floor");
    }
    std::uint64_t hi = 0;
    const std::uint64_t lo = _umul128(detail::magnitude(a), detail::magnitude(b), &hi);
    if (hi == 0 && lo == 0) {
        return 0;
    }
    const bool negative = ((a < 0) != (b < 0)) != (d < 0);
    std::uint64_t rem = 0;
    std::uint64_t q = detail::udiv128(hi, lo, detail::magnitude(d), rem, "mul_div_floor");
    if (negative && rem != 0) {
        if (q == std::numeric_limits<std::uint64_t>::max()) {
            detail::overflow("mul_div_floor");
        }
        ++q;  // floor of a negative quotient rounds away from zero
    }
    return detail::apply_sign(q, negative, "mul_div_floor");
}

// round_half_up_div(a * b, d): the product must be >= 0 and d even and > 0.
inline std::int64_t mul_div_round_half_up(std::int64_t a, std::int64_t b, std::int64_t d) {
    const bool product_negative = (a < 0) != (b < 0) && a != 0 && b != 0;
    if (product_negative || d <= 0 || d % 2 != 0) {
        throw NumericError("round_half_up_div needs a >= 0 and an even d > 0, got " + std::to_string(a) + "*" +
                           std::to_string(b) + ", " + std::to_string(d));
    }
    std::uint64_t hi = 0;
    const std::uint64_t lo = _umul128(detail::magnitude(a), detail::magnitude(b), &hi);
    std::uint64_t rem = 0;
    std::uint64_t q = detail::udiv128(hi, lo, static_cast<std::uint64_t>(d), rem, "mul_div_round_half_up");
    if (rem >= static_cast<std::uint64_t>(d / 2)) {
        if (q == std::numeric_limits<std::uint64_t>::max()) {
            detail::overflow("mul_div_round_half_up");
        }
        ++q;
    }
    return detail::apply_sign(q, false, "mul_div_round_half_up");
}

// ── Int128: an exact signed 128-bit integer ──────────────────────────────────
//
// MSVC has no __int128. Where A4 forms values beyond int64 (interval VWAP value, charges in 10⁻¹⁸ paisa,
// margin in paise × ppb) the engine uses this type; Python uses plain int. Every operation is exact: a result
// outside the 128-bit range is std::overflow_error, never a wraparound.
class Int128 {
 public:
    constexpr Int128() = default;
    constexpr Int128(std::int64_t v)  // NOLINT: implicit, like an integer promotion
        : lo_(static_cast<std::uint64_t>(v)), hi_(v < 0 ? -1 : 0) {}

    static Int128 from_parts(std::int64_t hi, std::uint64_t lo) {
        Int128 x;
        x.hi_ = hi;
        x.lo_ = lo;
        return x;
    }
    static Int128 mul(std::int64_t a, std::int64_t b) {  // exact 64 × 64 → 128
        Int128 x;
        x.lo_ = static_cast<std::uint64_t>(_mul128(a, b, &x.hi_));
        return x;
    }

    bool negative() const { return hi_ < 0; }
    bool is_zero() const { return hi_ == 0 && lo_ == 0; }
    bool fits_int64() const { return hi_ == (static_cast<std::int64_t>(lo_) < 0 ? -1 : 0); }
    std::int64_t to_int64() const {
        if (!fits_int64()) {
            detail::overflow("Int128::to_int64");
        }
        return static_cast<std::int64_t>(lo_);
    }

    Int128 operator-() const {
        if (hi_ == kInt64Min && lo_ == 0) {
            detail::overflow("Int128 negate");
        }
        return from_parts(~hi_ + (lo_ == 0 ? 1 : 0), ~lo_ + 1);
    }

    friend Int128 operator+(const Int128& a, const Int128& b) {
        const std::uint64_t lo = a.lo_ + b.lo_;
        const std::uint64_t carry = lo < a.lo_ ? 1 : 0;
        const std::int64_t hi = static_cast<std::int64_t>(static_cast<std::uint64_t>(a.hi_) +
                                                          static_cast<std::uint64_t>(b.hi_) + carry);
        // Signed overflow: both operands have the same sign and the result's sign differs.
        if ((a.hi_ < 0) == (b.hi_ < 0) && (hi < 0) != (a.hi_ < 0)) {
            detail::overflow("Int128 add");
        }
        return from_parts(hi, lo);
    }
    // Direct, not a + (−b): −INT128_MIN overflows although a − INT128_MIN often fits.
    friend Int128 operator-(const Int128& a, const Int128& b) {
        const std::uint64_t lo = a.lo_ - b.lo_;
        const std::uint64_t borrow = a.lo_ < b.lo_ ? 1 : 0;
        const std::int64_t hi = static_cast<std::int64_t>(static_cast<std::uint64_t>(a.hi_) -
                                                          static_cast<std::uint64_t>(b.hi_) - borrow);
        // Signed overflow: the operands have different signs and the result's sign differs from a's.
        if ((a.hi_ < 0) != (b.hi_ < 0) && (hi < 0) != (a.hi_ < 0)) {
            detail::overflow("Int128 subtract");
        }
        return from_parts(hi, lo);
    }
    Int128& operator+=(const Int128& b) { return *this = *this + b; }
    Int128& operator-=(const Int128& b) { return *this = *this - b; }

    // Exact product with an int64; overflow of the 128-bit result is an error.
    friend Int128 operator*(const Int128& a, std::int64_t b) {
        const bool neg = a.negative() != (b < 0);
        std::uint64_t mh = 0, ml = 0;
        a.magnitude(mh, ml);
        const std::uint64_t mb = detail::magnitude(b);
        std::uint64_t lo_hi = 0;
        const std::uint64_t lo = _umul128(ml, mb, &lo_hi);
        std::uint64_t hi_hi = 0;
        const std::uint64_t hi_lo = _umul128(mh, mb, &hi_hi);
        const std::uint64_t hi = hi_lo + lo_hi;
        if (hi_hi != 0 || hi < hi_lo) {
            detail::overflow("Int128 multiply");
        }
        return from_magnitude(hi, lo, neg, "Int128 multiply");
    }

    friend bool operator==(const Int128& a, const Int128& b) { return a.hi_ == b.hi_ && a.lo_ == b.lo_; }
    friend std::strong_ordering operator<=>(const Int128& a, const Int128& b) {
        if (a.hi_ != b.hi_) {
            return a.hi_ <=> b.hi_;
        }
        return a.lo_ <=> b.lo_;
    }

    // Mathematical floor / ceiling of this / d for a non-zero int64 d of any sign.
    Int128 floor_div(std::int64_t d) const { return divide(d, false); }
    Int128 ceil_div(std::int64_t d) const { return divide(d, true); }

    // The nearest double, ties to even: what Python's float(int) gives.
    double to_double() const {
        std::uint64_t mh = 0, ml = 0;
        magnitude(mh, ml);
        double mag = 0.0;
        if (mh == 0) {
            mag = u64_to_double(ml);
        } else {
            // Keep the top 64 significant bits, folding any lower bit into a sticky bit, then scale.
            unsigned long top = 0;
            _BitScanReverse64(&top, mh);
            const int shift = static_cast<int>(top) + 1;  // bits of mh used, 1…64
            const std::uint64_t window = shift == 64 ? mh : (mh << (64 - shift)) | (ml >> shift);
            const bool sticky = shift == 64 ? ml != 0 : (ml << (64 - shift)) != 0;
            mag = std::ldexp(u64_to_double(window | (sticky ? 1u : 0u)), shift);
        }
        return negative() ? -mag : mag;
    }

    std::string to_string() const {
        if (fits_int64()) {
            return std::to_string(static_cast<std::int64_t>(lo_));
        }
        std::uint64_t mh = 0, ml = 0;
        magnitude(mh, ml);
        std::string digits;
        while (mh != 0 || ml != 0) {  // repeated division by 10^18
            constexpr std::uint64_t base = 1'000'000'000'000'000'000ULL;
            const std::uint64_t qh = mh / base;
            std::uint64_t rem = 0;
            const std::uint64_t ql = _udiv128(mh % base, ml, base, &rem);
            std::string chunk = std::to_string(rem);
            if (qh != 0 || ql != 0) {
                chunk.insert(0, 18 - chunk.size(), '0');
            }
            digits.insert(0, chunk);
            mh = qh;
            ml = ql;
        }
        return (negative() ? "-" : "") + digits;
    }

 private:
    void magnitude(std::uint64_t& hi, std::uint64_t& lo) const {
        if (!negative()) {
            hi = static_cast<std::uint64_t>(hi_);
            lo = lo_;
            return;
        }
        lo = ~lo_ + 1;
        hi = ~static_cast<std::uint64_t>(hi_) + (lo == 0 ? 1 : 0);  // |INT128_MIN| = 2^127 still fits unsigned
    }

    static Int128 from_magnitude(std::uint64_t hi, std::uint64_t lo, bool negative, const char* what) {
        constexpr std::uint64_t top = std::uint64_t{1} << 63;
        if (!negative) {
            if (hi >= top) {
                detail::overflow(what);
            }
            return from_parts(static_cast<std::int64_t>(hi), lo);
        }
        if (hi > top || (hi == top && lo != 0)) {
            detail::overflow(what);
        }
        const std::uint64_t nlo = ~lo + 1;
        const std::uint64_t nhi = ~hi + (nlo == 0 ? 1 : 0);
        return from_parts(static_cast<std::int64_t>(nhi), nlo);
    }

    Int128 divide(std::int64_t d, bool ceiling) const {
        if (d == 0) {
            detail::zero_divisor("Int128 divide");
        }
        const bool neg = negative() != (d < 0);
        std::uint64_t mh = 0, ml = 0;
        magnitude(mh, ml);
        const std::uint64_t md = detail::magnitude(d);
        std::uint64_t qh = mh / md;
        std::uint64_t rem = 0;
        std::uint64_t ql = _udiv128(mh % md, ml, md, &rem);
        // Floor rounds a negative quotient away from zero; ceiling rounds a positive one away from zero.
        if (rem != 0 && neg != ceiling) {
            if (++ql == 0) {
                ++qh;
            }
        }
        return from_magnitude(qh, ql, neg, "Int128 divide");
    }

    static double u64_to_double(std::uint64_t v) {
        if (static_cast<std::int64_t>(v) >= 0) {
            return static_cast<double>(static_cast<std::int64_t>(v));  // exact or correctly rounded
        }
        // Halve with the dropped bit kept as a sticky bit, convert, double: correctly rounded.
        return static_cast<double>(static_cast<std::int64_t>((v >> 1) | (v & 1))) * 2.0;
    }

    std::uint64_t lo_ = 0;
    std::int64_t hi_ = 0;
};

}  // namespace stocksim
