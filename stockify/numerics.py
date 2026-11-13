"""Exact integer arithmetic and canonical output (A4_engine_semantics.md §0, §9.2).

Everything that touches money or a rate goes through this module:

* money is int64 paise, quantities int64 units (I-3);
* rates are integers in ppb (charges, taxes), ppm (fractions such as
  participation) or bp (market protection);
* config files hold decimals as strings, converted here *exactly* with
  `Decimal`; a value that is not an integer in the target unit is an error,
  never a rounding.

The C++ engine implements the same helpers; the two must agree bit for bit.
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

PPB = 10**9
PPM = 10**6
BP = 10**4
PAISE_PER_RUPEE = 100


class NumericError(ValueError):
    """A value cannot be represented exactly in the requested unit."""


# ── Exact decimal-string conversion ──────────────────────────────────────────

def _decimal(text: str) -> Decimal:
    if not isinstance(text, str):
        raise NumericError(f"expected a decimal string, got {type(text).__name__}: {text!r}")
    try:
        value = Decimal(text.strip())
    except InvalidOperation as e:
        raise NumericError(f"not a decimal: {text!r}") from e
    if not value.is_finite():
        raise NumericError(f"not a finite decimal: {text!r}")
    return value


def _scaled(text: str, scale: int, unit: str) -> int:
    # Exact integer arithmetic on the digits: Decimal multiplication would round to the context's 28 digits
    # and could make a non-whole value look whole (A4 §0.1). `scale` is a power of ten.
    sign, digits, exponent = _decimal(text).as_tuple()
    coefficient = int("".join(map(str, digits)))
    if coefficient == 0:
        return 0
    power = exponent + len(str(scale)) - 1
    significant = len(str(coefficient))
    if power >= 0:
        if significant + power > 19:  # more digits than any int64 has
            raise OverflowError(f"{text!r} does not fit in int64 {unit}")
        value = coefficient * 10**power
    else:
        if -power > significant:  # every significant digit is below the unit
            raise NumericError(f"{text!r} is not a whole number of {unit}")
        value, rest = divmod(coefficient, 10**-power)
        if rest:
            raise NumericError(f"{text!r} is not a whole number of {unit}")
    return check_int64(-value if sign else value)


def to_ppb(fraction: str) -> int:
    """'0.0015' (0.15%) -> 1_500_000."""
    return _scaled(fraction, PPB, "ppb")


def to_ppm(fraction: str) -> int:
    """'0.1' (10%) -> 100_000."""
    return _scaled(fraction, PPM, "ppm")


def to_bp(fraction: str) -> int:
    """'0.02' (2%) -> 200."""
    return _scaled(fraction, BP, "bp")


def to_paise(rupees: str) -> int:
    """'1234.50' -> 123_450."""
    return _scaled(rupees, PAISE_PER_RUPEE, "paise")


# ── Integer helpers (A4 §0.2) ────────────────────────────────────────────────

def check_int64(value: int) -> int:
    if not INT64_MIN <= value <= INT64_MAX:
        raise OverflowError(f"{value} does not fit in int64")
    return value


def traded_pnl(realised: int, d: int, gross: int, cost: int) -> int:
    """realised + d·(gross − cost), each step int64-checked as the engine's Position.pnl is (d is +1 or −1)."""
    value = check_int64(check_int64(gross) - cost)
    return check_int64(realised + (value if d > 0 else check_int64(-value)))


def floor_div(a: int, d: int) -> int:
    """Mathematical floor for any sign (Python `//` already floors)."""
    if d == 0:
        raise ZeroDivisionError("floor_div by zero")
    return a // d


def ceil_div(a: int, d: int) -> int:
    if d == 0:
        raise ZeroDivisionError("ceil_div by zero")
    return -((-a) // d)


def round_half_up_div(a: int, d: int) -> int:
    """(a + d/2) / d for a >= 0 and an even d > 0."""
    if a < 0 or d <= 0 or d % 2:
        raise NumericError(f"round_half_up_div needs a >= 0 and an even d > 0, got {a}, {d}")
    return (a + d // 2) // d


def floor_to(x: int, step: int) -> int:
    return floor_div(x, step) * step


def ceil_to(x: int, step: int) -> int:
    return ceil_div(x, step) * step


def mul_div_floor(a: int, b: int, d: int) -> int:
    """floor(a*b/d); the product is exact (C++ forms it in 128 bits)."""
    return check_int64(floor_div(a * b, d))


def mul_div_round_half_up(a: int, b: int, d: int) -> int:
    return check_int64(round_half_up_div(a * b, d))


def money_from_float_margin(x: float) -> int:
    """A float that becomes money: margin uses ceil_to(ceil(x), 100) (A4 §0.3)."""
    if not math.isfinite(x):
        raise NumericError(f"margin is not finite: {x!r}")
    return check_int64(ceil_to(math.ceil(x), 100))


# ── Canonical output (A4 §9.2) ───────────────────────────────────────────────

def format_float(x: float) -> str:
    """Shortest round-trip digits laid out like Python's `repr`."""
    if not math.isfinite(x):
        raise NumericError(f"non-finite float has no canonical form: {x!r}")
    return repr(float(x))


def _canonical(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format_float(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, dict):
        items = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError(f"canonical JSON keys must be strings, got {key!r}")
            items.append(_quote(key) + ":" + _canonical(value[key]))
        return "{" + ",".join(items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    raise TypeError(f"not JSON-serialisable: {type(value).__name__}")


def _quote(text: str) -> str:
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def dumps_canonical(value) -> str:
    """Sorted keys, no spaces, canonical floats; UTF-8 text."""
    return _canonical(value)
