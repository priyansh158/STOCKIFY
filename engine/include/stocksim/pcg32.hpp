// PCG32 exactly as the reference pcg_basic.c (A4 §4.5), the twin of stockify.oracle.rng.Pcg32.
// No standard-library distributions anywhere in the engine (C-9).
#pragma once

#include <cstdint>

#include "stocksim/errors.hpp"

namespace stocksim {

inline constexpr std::uint64_t kSlippageStream = 1;  // initseq for fill slippage (A4 §4.4)
inline constexpr std::uint64_t kStrategyStream = 2;  // initseq for ctx.rng()

class Pcg32 {
 public:
    // pcg32_srandom_r: state = 0; inc = (initseq << 1) | 1; next(); state += initstate; next().
    Pcg32(std::uint64_t initstate, std::uint64_t initseq) : state_(0), inc_((initseq << 1u) | 1u) {
        next_u32();
        state_ += initstate;
        next_u32();
    }

    std::uint32_t next_u32() {
        const std::uint64_t old = state_;
        state_ = old * 6364136223846793005ULL + inc_;  // mod 2^64 by unsigned wraparound
        const auto xorshifted = static_cast<std::uint32_t>(((old >> 18u) ^ old) >> 27u);
        const auto rot = static_cast<std::uint32_t>(old >> 59u);
        return (xorshifted >> rot) | (xorshifted << ((0u - rot) & 31u));
    }

    // Uniform in [0, n) for n >= 1, without modulo bias.
    std::uint32_t below(std::uint32_t n) {
        if (n < 1) {
            throw NumericError("below(n) needs n >= 1");
        }
        const std::uint32_t threshold = (0u - n) % n;  // (2^32 - n) mod n
        for (;;) {
            const std::uint32_t v = next_u32();
            if (v >= threshold) {
                return v % n;
            }
        }
    }

    // High word drawn first.
    std::uint64_t next_u64() {
        const std::uint64_t hi = next_u32();
        return (hi << 32u) | next_u32();
    }

    // Uniform in [0, 1) with 53 random bits; exact (the product is a power-of-two scaling).
    double random() { return static_cast<double>(next_u64() >> 11u) * 0x1.0p-53; }

    std::uint64_t state() const { return state_; }
    std::uint64_t inc() const { return inc_; }

    // A stream exactly where another left it (a paper session's next day, A1 §6.4 `rng`).
    static Pcg32 at(std::uint64_t state, std::uint64_t inc) {
        Pcg32 g(0, 0);
        g.state_ = state;
        g.inc_ = inc;
        return g;
    }

 private:
    std::uint64_t state_;
    std::uint64_t inc_;
};

}  // namespace stocksim
