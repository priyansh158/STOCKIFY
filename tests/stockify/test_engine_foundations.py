"""M3.1: the C++ foundations agree with their Python twins (A4 §0, §4.5, §9.2; A3 P-01, U-11).

Each test drives `stockify._engine.testing` and the oracle's Python code with the same seeded random inputs.
Skipped when the engine is not built (`run.ps1 -BuildEngine`).
"""
from __future__ import annotations

import hashlib
import json
import random
import struct
import unittest

import tests.support  # noqa: F401  — must precede project imports

from stockify import engine, numerics as n
from stockify.oracle import artifacts
from stockify.oracle.rng import Pcg32

INT64_MIN, INT64_MAX = n.INT64_MIN, n.INT64_MAX


def _testing():
    return engine.load().testing


def _outcome(fn, *args):
    """('ok', value) or ('error', None): both implementations must fail on the same inputs."""
    try:
        return ("ok", fn(*args))
    except (ValueError, ArithmeticError):
        return ("error", None)


def _int64(rng: random.Random) -> int:
    """Mixed magnitudes: small values, values near the limits, and anything in between."""
    pick = rng.random()
    if pick < 0.3:
        return rng.randint(-1000, 1000)
    if pick < 0.5:
        return rng.choice([INT64_MIN, INT64_MIN + 1, INT64_MAX, INT64_MAX - 1, -1, 0, 1, 2, -2])
    bits = rng.randint(1, 63)
    v = rng.getrandbits(bits)
    return -v - 1 if rng.random() < 0.5 and v < 2**63 else v


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class IntegerHelperParityTests(unittest.TestCase):
    def test_two_argument_helpers(self):
        t = _testing()
        rng = random.Random(3101)
        pairs = [(n.floor_div, t.floor_div), (n.ceil_div, t.ceil_div), (n.floor_to, t.floor_to),
                 (n.ceil_to, t.ceil_to), (n.round_half_up_div, t.round_half_up_div)]
        for _ in range(20_000):
            a, d = _int64(rng), _int64(rng)
            if rng.random() < 0.3:
                d = rng.choice([2, 10, 100, 1000, 10**9, -2, 5, 3])
            for py_fn, cpp_fn in pairs:
                expected = _outcome(py_fn, a, d)
                if expected[0] == "ok" and not INT64_MIN <= expected[1] <= INT64_MAX:
                    expected = ("error", None)  # Python's int grows; C++ must report the overflow
                with self.subTest(fn=py_fn.__name__, a=a, d=d):
                    self.assertEqual(_outcome(cpp_fn, a, d), expected)

    def test_mul_div(self):
        t = _testing()
        rng = random.Random(3102)
        for _ in range(20_000):
            a, b, d = _int64(rng), _int64(rng), _int64(rng)
            if rng.random() < 0.5:
                d = rng.choice([n.PPB, n.PPM, n.BP, 100, 2, -7, 3])
                b = rng.randint(0, 2 * n.PPB)
            for py_fn, cpp_fn in [(n.mul_div_floor, t.mul_div_floor),
                                  (n.mul_div_round_half_up, t.mul_div_round_half_up)]:
                with self.subTest(fn=py_fn.__name__, a=a, b=b, d=d):
                    self.assertEqual(_outcome(cpp_fn, a, b, d), _outcome(py_fn, a, b, d))


def _decimal_text(rng: random.Random) -> str:
    sign = rng.choice(["", "", "-", "+"])
    whole = "".join(rng.choice("0123456789") for _ in range(rng.randint(0, 12)))
    frac = "".join(rng.choice("0123456789") for _ in range(rng.randint(0, 12)))
    if not whole and not frac:
        whole = "0"
    text = sign + whole + ("." + frac if frac or rng.random() < 0.2 else "")
    if rng.random() < 0.3:
        text += rng.choice("eE") + rng.choice(["", "+", "-"]) + str(rng.randint(0, 25))
    if rng.random() < 0.1:
        text = " " + text + "\t"
    return text


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class DecimalParityTests(unittest.TestCase):
    def test_random_decimal_strings(self):
        t = _testing()
        rng = random.Random(3103)
        pairs = [(n.to_ppb, t.to_ppb), (n.to_ppm, t.to_ppm), (n.to_bp, t.to_bp), (n.to_paise, t.to_paise)]
        for _ in range(20_000):
            text = _decimal_text(rng)
            for py_fn, cpp_fn in pairs:
                with self.subTest(fn=py_fn.__name__, text=text):
                    self.assertEqual(_outcome(cpp_fn, text), _outcome(py_fn, text))

    def test_more_than_28_significant_digits_is_still_exact(self):
        # Decimal multiplication used to round to 28 digits and accept this as exactly 1e9 ppb.
        text = "1.0000000000000000000000000001"
        self.assertRaises(n.NumericError, n.to_ppb, text)
        self.assertRaises(ValueError, _testing().to_ppb, text)

    def test_malformed_text_fails_on_both(self):
        t = _testing()
        for text in ["", " ", "+", ".", "e5", "1e", "1.2.3", "1,5", "inf", "nan", "--1", "1 2"]:
            with self.subTest(text=text):
                self.assertEqual(_outcome(t.to_paise, text), ("error", None))
                self.assertEqual(_outcome(n.to_paise, text), ("error", None))


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class Pcg32ParityTests(unittest.TestCase):
    def test_p01_twenty_seeds_two_streams(self):
        t = _testing()
        rng = random.Random(3104)
        seeds = [0, 1, 42, 2**64 - 1] + [rng.getrandbits(64) for _ in range(16)]
        for seed in seeds:
            for stream in (1, 2):
                py, cpp = Pcg32(seed, stream), t.Pcg32(seed, stream)
                with self.subTest(seed=seed, stream=stream):
                    self.assertEqual([cpp.next_u32() for _ in range(10_000)], [py.next_u32() for _ in range(10_000)])
                    bounds = [rng.choice([1, 2, 3, 10, 1000, 2**31 + 1, 2**32 - 1]) for _ in range(500)]
                    self.assertEqual([cpp.below(b) for b in bounds], [py.below(b) for b in bounds])
                    self.assertEqual([cpp.random() for _ in range(500)], [py.random() for _ in range(500)])
                    self.assertEqual([cpp.next_u64() for _ in range(500)], [py.next_u64() for _ in range(500)])
                    self.assertEqual((cpp.state, cpp.inc), (py.state, py.inc))

    def test_reference_sequence(self):
        r = _testing().Pcg32(42, 54)
        self.assertEqual([r.next_u32() for _ in range(6)],
                         [0xa15c02b7, 0x7b47f409, 0xba1d3330, 0x83d2f293, 0xbfa4784b, 0xcbed606e])


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class OutputFormatParityTests(unittest.TestCase):
    def test_u11_a_million_random_doubles(self):
        rng = random.Random(3105)
        values = []
        while len(values) < 1_000_000:
            pick = rng.random()
            if pick < 0.6:  # any finite bit pattern, subnormals included
                (x,) = struct.unpack("<d", struct.pack("<Q", rng.getrandbits(64)))
                if x != x or x in (float("inf"), float("-inf")):
                    continue
            elif pick < 0.9:  # values like the metrics produce
                x = rng.uniform(-1e6, 1e6) / rng.choice([1, 7, 100, 3600])
            else:  # the layout boundaries
                x = rng.choice([1e-4, 1e16, 9999999999999998.0, 1e-5, 0.0, -0.0, 5e-324, 1.0, 0.1]) * rng.choice(
                    [1.0, -1.0, 1.0000000000000002, 0.9999999999999999])
            values.append(x)
        got = _testing().format_floats(struct.pack(f"<{len(values)}d", *values)).split("\n")
        expected = [repr(x) for x in values]
        mismatches = [(v, g, e) for v, g, e in zip(values, got, expected) if g != e]
        self.assertEqual(len(got), len(values))
        self.assertEqual(mismatches[:5], [])

    def test_canonical_json(self):
        t = _testing()
        rng = random.Random(3106)

        def value(depth: int):
            kinds = ["none", "bool", "int", "float", "str"] + (["list", "dict"] if depth < 3 else [])
            k = rng.choice(kinds)
            if k == "none":
                return None
            if k == "bool":
                return rng.random() < 0.5
            if k == "int":
                return rng.randint(INT64_MIN, INT64_MAX)
            if k == "float":
                return rng.uniform(-1e9, 1e9) * rng.choice([1, 1e-12, 1e12])
            if k == "str":
                return "".join(rng.choice('ab,"\\\n\r\t\x01\x7féरु₹ ') for _ in range(rng.randint(0, 8)))
            if k == "list":
                return [value(depth + 1) for _ in range(rng.randint(0, 4))]
            return {"".join(rng.choice("abcéz_") for _ in range(rng.randint(0, 4))): value(depth + 1)
                    for _ in range(rng.randint(0, 4))}

        for _ in range(3_000):
            v = value(0)
            with self.subTest(value=v):
                self.assertEqual(t.dumps_canonical(v), n.dumps_canonical(v))

    def test_csv_cells(self):
        t = _testing()
        for v in [None, True, False, 0, -5, INT64_MAX, "", "BUY", "a,b", 'say "hi"', "x\ny", "cr\rok", "₹ 100"]:
            with self.subTest(value=v):
                self.assertEqual(t.csv_cell(v), artifacts._cell(v))

    def test_sha256(self):
        t = _testing()
        rng = random.Random(3107)
        for size in [0, 1, 55, 56, 63, 64, 65, 1000, 100_000]:
            data = rng.randbytes(size)
            self.assertEqual(t.sha256_hex(data), hashlib.sha256(data).hexdigest())


INT128_MIN, INT128_MAX = -(2**127), 2**127 - 1


def _int128(rng: random.Random) -> int:
    pick = rng.random()
    if pick < 0.2:
        return rng.choice([INT128_MIN, INT128_MIN + 1, INT128_MAX, -1, 0, 1, 2**64, -(2**64), 2**63, -(2**63) - 1])
    if pick < 0.5:
        return _int64(rng)
    v = rng.getrandbits(rng.randint(1, 127))
    return -v - 1 if rng.random() < 0.5 else v


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class Int128ParityTests(unittest.TestCase):
    """The engine's Int128 against Python int (interval VWAP, charges in 10⁻¹⁸ paisa, margin in paise × ppb)."""

    def _same(self, cpp, py, *args):  # noqa: ANN001
        try:
            expected = ("ok", py(*args))
            if not INT128_MIN <= expected[1] <= INT128_MAX:
                expected = ("error", None)
        except ZeroDivisionError:
            expected = ("error", None)
        self.assertEqual(_outcome(cpp, *args), expected, args)

    def test_arithmetic(self):
        t = _testing()
        rng = random.Random(3108)
        for _ in range(20_000):
            a, b = _int128(rng), _int128(rng)
            x, y = _int64(rng), _int64(rng)
            d = rng.choice([_int64(rng), 1, -1, 7, 10**9, 10**18, -(10**18), 0])
            self._same(t.int128_mul, lambda p, q: p * q, x, y)
            self._same(t.int128_add, lambda p, q: p + q, a, b)
            self._same(t.int128_sub, lambda p, q: p - q, a, b)
            self._same(t.int128_mul64, lambda p, q: p * q, a, x)
            self._same(t.int128_floor_div, lambda p, q: p // q, a, d)
            self._same(t.int128_ceil_div, lambda p, q: -((-p) // q), a, d)
            self.assertEqual(t.int128_cmp(a, b), (a > b) - (a < b))

    def test_to_double_is_pythons_float(self):
        t = _testing()
        rng = random.Random(3109)
        values = [_int128(rng) for _ in range(20_000)]
        # Ties and sticky bits at several magnitudes, including a 64-bit top word with its highest bit set.
        values += [2**53 + 1, 2**64 + 2**11, 2**64 + 2**11 + 1, (2**52 + 1) * 2**74, (2**52 + 1) * 2**74 + 1,
                   2**127 - 2**74, INT128_MIN, INT128_MAX, -(2**126) - 2**72 - 1]
        for v in values:
            self.assertEqual(t.int128_to_double(v), float(v), v)


@unittest.skipUnless(engine.available(), "stockify._engine is not built")
class BundleEnvelopeTests(unittest.TestCase):
    BUNDLE = {
        "schema": "stockify.bundle/1", "seed": 42, "days": [], "instruments": [],
        "packs": [{"sha256": "ab", "path": "packs/ab.evt", "price_basis": "raw", "spec": {}}],
        "strategy": {"kind": "python", "ref": "m:C", "source_sha256": "ff", "params": {}},
        "labels": ["fill:bar"],
    }

    def test_envelope_and_hash(self):
        text = json.dumps(self.BUNDLE, sort_keys=True)
        got = _testing().parse_bundle_envelope(text)
        self.assertEqual(got["sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(got["seed"], 42)
        self.assertEqual(got["packs"], [("ab", "packs/ab.evt", "raw")])
        self.assertEqual(got["strategy"], ("python", "m:C", "ff"))
        self.assertEqual(got["labels"], ["fill:bar"])

    def test_wrong_schema_is_a_format_error(self):
        mod = engine.load()
        text = json.dumps({**self.BUNDLE, "schema": "stockify.bundle/9"})
        with self.assertRaises(mod.FormatError):
            mod.testing.parse_bundle_envelope(text)


if __name__ == "__main__":
    unittest.main()
