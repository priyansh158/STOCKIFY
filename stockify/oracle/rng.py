"""PCG32 exactly as the reference `pcg_basic.c` (A4 §4.5).

The C++ engine implements the same generator, so both draw identical
numbers (A3 P-01). Stream 1 = slippage, stream 2 = `ctx.rng()`.
"""
from __future__ import annotations

MASK64 = (1 << 64) - 1
MASK32 = (1 << 32) - 1
MULT = 6364136223846793005


class Pcg32:
    __slots__ = ("state", "inc")

    def __init__(self, initstate: int, initseq: int):
        self.reseed(initstate, initseq)

    def reseed(self, initstate: int, initseq: int) -> None:
        """pcg32_srandom_r in place: the generator becomes Pcg32(initstate, initseq)."""
        self.state = 0
        self.inc = ((initseq << 1) | 1) & MASK64
        self.next_u32()
        self.state = (self.state + initstate) & MASK64
        self.next_u32()

    def next_u32(self) -> int:
        old = self.state
        self.state = (old * MULT + self.inc) & MASK64
        xorshifted = (((old >> 18) ^ old) >> 27) & MASK32
        rot = old >> 59
        return ((xorshifted >> rot) | (xorshifted << ((-rot) & 31))) & MASK32

    def below(self, n: int) -> int:
        """Uniform in [0, n), n >= 1, without modulo bias."""
        if n < 1:
            raise ValueError("below(n) needs n >= 1")
        threshold = ((1 << 32) - n) % n
        while True:
            v = self.next_u32()
            if v >= threshold:
                return v % n

    def next_u64(self) -> int:
        hi = self.next_u32()
        return (hi << 32) | self.next_u32()

    def random(self) -> float:
        """Uniform in [0, 1) with 53 random bits."""
        return (self.next_u64() >> 11) * (2.0 ** -53)

    def copy(self) -> "Pcg32":
        c = Pcg32.__new__(Pcg32)
        c.state, c.inc = self.state, self.inc
        return c


SLIPPAGE_STREAM = 1
STRATEGY_STREAM = 2
