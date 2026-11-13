"""Seeded random streams per purpose: never the engine's streams 1 and 2."""
from __future__ import annotations

import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports

from stockify.bundle import day_seed
from stockify.oracle.rng import Pcg32
from stockify.research import seeding


class SeedingTests(unittest.TestCase):
    def test_streams_are_not_the_engines(self):
        values = list(seeding.STREAMS.values())
        self.assertFalse({1, 2} & set(values))
        self.assertEqual(len(values), len(set(values)))

    def test_same_seed_same_numbers(self):
        a, b = seeding.stream(5, "bootstrap"), seeding.stream(5, "bootstrap")
        self.assertEqual([a.next_u32() for _ in range(20)], [b.next_u32() for _ in range(20)])
        c = seeding.stream(6, "bootstrap")
        self.assertNotEqual([seeding.stream(5, "bootstrap").next_u32() for _ in range(5)],
                            [c.next_u32() for _ in range(5)])

    def test_purposes_differ(self):
        draws = {p: seeding.stream(5, p).next_u32() for p in seeding.STREAMS}
        self.assertEqual(len(set(draws.values())), len(draws))

    def test_state_is_day_seed_of_the_anchor(self):
        self.assertEqual(seeding.stream(7, "pbo").state, Pcg32(day_seed(7, seeding.ANCHOR), 12).state)
        self.assertEqual(seeding.ANCHOR, date(2000, 1, 3))
        other = date(2001, 5, 7)
        self.assertEqual(seeding.stream(7, "pbo", other).state, Pcg32(day_seed(7, other), 12).state)

    def test_unknown_purpose_raises(self):
        with self.assertRaises(KeyError):
            seeding.stream(1, "dice")


if __name__ == "__main__":
    unittest.main()
