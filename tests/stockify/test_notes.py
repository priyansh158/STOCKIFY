"""CH 6.1: clean_note, fmt_price, fmt_qty and to_level, identical in Python and C++."""
from __future__ import annotations

import math
import random
import re
import unicodedata
import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine
from stockify.oracle.notes import NOTE_MAX, clean_note
from stockify.strategy import StrategyCodeError, check_source, fmt_price, fmt_qty, to_level

_SURROGATE = re.compile("[\ud800-\udfff]")
_ENGINE = "stockify._engine is not built; run `.\\run.ps1 -BuildEngine`"


class CleanNoteTests(unittest.TestCase):
    def test_clean_note_hand_cases(self):
        self.assertEqual(NOTE_MAX, 200)
        self.assertEqual(clean_note(""), "")
        self.assertEqual(clean_note("  x  "), "x")
        self.assertEqual(clean_note("a\tb\nc"), "a b c")
        self.assertEqual(clean_note("a\x00b\x7fc\x85d"), "a b c d")
        self.assertEqual(clean_note("a b c"), "a b c")
        self.assertEqual(clean_note("a  b"), "a  b")                     # no inner collapsing
        self.assertEqual(clean_note(" x "), " x ")   # only ASCII spaces are trimmed
        self.assertEqual(clean_note("\n\tx\r\n"), "x")                    # controls become spaces, then trim
        self.assertEqual(clean_note("a\ud800b"), "a�b")
        self.assertEqual(clean_note("x" * 200), "x" * 200)
        self.assertEqual(clean_note("x" * 201), "x" * 199 + "…")
        self.assertEqual(len(clean_note("\U0001F600" * 500)), 200)
        self.assertEqual(clean_note(" " * 5 + "x" * 250), "x" * 199 + "…")
        with self.assertRaises(TypeError):
            clean_note(None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            clean_note(b"x")  # type: ignore[arg-type]

    def test_fmt_price_and_qty_hand_cases(self):
        self.assertEqual(fmt_price(241840), "2,418.40")
        self.assertEqual(fmt_price(123456780), "12,34,567.80")
        self.assertEqual(fmt_price(-5), "-0.05")
        self.assertEqual(fmt_price(0), "0.00")
        self.assertEqual(fmt_price(5), "0.05")
        self.assertEqual(fmt_price(-123456780), "-12,34,567.80")
        self.assertEqual(fmt_price(99999), "999.99")
        self.assertEqual(fmt_price(100000), "1,000.00")
        self.assertEqual(fmt_price(2**63 - 1), "92,23,37,20,36,85,47,758.07")
        self.assertEqual(fmt_price(-2**63), "-92,23,37,20,36,85,47,758.08")
        self.assertEqual(fmt_qty(125000), "1,25,000")
        self.assertEqual(fmt_qty(0), "0")
        self.assertEqual(fmt_qty(999), "999")
        self.assertEqual(fmt_qty(1000), "1,000")
        self.assertEqual(fmt_qty(-125000), "-1,25,000")
        for bad in (1.0, "1", None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    fmt_price(bad)  # type: ignore[arg-type]
                with self.assertRaises(TypeError):
                    fmt_qty(bad)  # type: ignore[arg-type]

    def test_to_level_hand_cases(self):
        self.assertEqual(to_level(54.3217), 543217)
        self.assertEqual(to_level(0.0), 0)
        self.assertEqual(to_level(-1.5), -15000)
        self.assertEqual(to_level(0.00005), 0)       # 0.5 rounds half-even to 0
        self.assertEqual(to_level(0.00035), 4)       # 3.5 rounds half-even to 4
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                to_level(bad)

    def test_to_level_refuses_past_int64_and_non_numbers(self):
        for bad in (1e20, -1e20, 1e305, -1e305, 2**70):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                to_level(bad)
        for bad in ("1.5", True, False, None, b"1"):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                to_level(bad)  # type: ignore[arg-type]
        self.assertEqual(to_level(3), 30000)

    @unittest.skipUnless(engine.available(), _ENGINE)
    def test_to_level_matches_engine_at_the_int64_boundary(self):
        eng = engine.load()
        edge = 2.0**63 / 10000.0
        probes = [edge, -edge, 1e20, -1e20, 1e305, -1e305]
        for base in (edge, -edge):
            x = base
            for _ in range(4):
                x = math.nextafter(x, 0.0)
                probes.append(x)
            x = base
            for _ in range(4):
                x = math.nextafter(x, math.copysign(math.inf, base))
                probes.append(x)
        for x in probes:
            try:
                want = to_level(x)
            except ValueError:
                with self.subTest(x=x), self.assertRaises(RuntimeError):
                    eng.to_level(x)
            else:
                with self.subTest(x=x):
                    self.assertEqual(eng.to_level(x), want)

    def test_helpers_are_importable_by_a_strategy(self):
        check_source("from stockify.strategy import fmt_price\n", "stockify.strategies.t", "t.py")
        with self.assertRaises(StrategyCodeError):
            check_source("from stockify.oracle.notes import clean_note\n", "stockify.strategies.t", "t.py")


_ALPHABET = ["a", "Z", "7", " ", "  ", "\t", "\n", "\r", "\x00", "\x1f", "\x7f", "\x85", "\x9f", ",", '"', " ",
             " ", " ", "‏", "א", "ا", "\U0001F600", "\U0001F468‍\U0001F4BB", "…",
             "<script>", "é", "\ud800", "\udfff"]


@unittest.skipUnless(engine.available(), _ENGINE)
class EngineParityTests(unittest.TestCase):
    def test_the_engine_refuses_a_bool_for_fmt_price_fmt_qty_and_to_level_as_the_twins_do(self):
        eng = engine.load()
        for name in ("fmt_price", "fmt_qty", "to_level"):
            for bad in (True, False):
                with self.subTest(fn=name, bad=bad), self.assertRaises(TypeError):
                    getattr(eng, name)(bad)
        self.assertEqual((eng.fmt_price(5), eng.fmt_qty(1000), eng.to_level(1)), ("0.05", "1,000", 10000))

    def test_clean_note_matches_engine_on_random_text(self):
        eng = engine.load()
        rng = random.Random(61)
        for i in range(2000):
            s = "".join(rng.choice(_ALPHABET) for _ in range(rng.choice((0, 1, 5, 40, 199, 200, 201, 260, 400))))
            got = clean_note(s)
            # a lone surrogate has no UTF-8 form; rule 1 makes it U+FFFD, so hand the engine that
            self.assertEqual(got, eng.clean_note(_SURROGATE.sub("�", s).encode("utf-8")), f"case {i}: {s!r}")
            self.assertLessEqual(len(got), NOTE_MAX)
            self.assertFalse(any(unicodedata.category(c) == "Cc" for c in got), repr(got))
            self.assertEqual(got, got.strip(" "))
            self.assertEqual(clean_note(got), got)

    def test_clean_note_replaces_invalid_utf8_in_the_engine(self):
        eng = engine.load()
        self.assertEqual(eng.clean_note(b"\xc3("), "�(")
        self.assertEqual(eng.clean_note(b"a\xff\xfeb"), "a��b")

    def test_fmt_matches_engine_on_random_ints(self):
        eng = engine.load()
        rng = random.Random(62)
        vals = [0, 5, -5, 100, -100, 2**63 - 1, -2**63, 10**15, -10**15]
        vals += [rng.randint(-10**15, 10**15) for _ in range(2000)]
        vals += [100 * rng.randint(-10**13, 10**13) for _ in range(500)]
        for v in vals:
            self.assertEqual(fmt_price(v), eng.fmt_price(v), v)
            self.assertEqual(fmt_qty(v), eng.fmt_qty(v), v)

    def test_to_level_matches_engine(self):
        eng = engine.load()
        rng = random.Random(63)
        for x in [0.00005, 0.00035, 54.3217, -0.00005] + [rng.uniform(-1e6, 1e6) for _ in range(500)]:
            self.assertEqual(to_level(x), eng.to_level(x), x)
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(RuntimeError):
                eng.to_level(bad)


if __name__ == "__main__":
    unittest.main()
