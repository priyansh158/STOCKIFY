"""M4 (MEZ 4.7): the exact SPAN requirement of one combined commodity, from NSE's SPAN 4.00 risk parameters (oracle).

Input: a `CombinedCommodity` read by `stockify.fno.span_file` and the book's legs as (contract key, signed units),
long > 0. Output: a `SpanResult` of exact `Decimal` rupees, and `span_paise` for the engine's int64 paise.

Units (verified against the 2026-11-09 i1 file, see tests/stockify/test_span_exact.py):

* a risk-array value `ra/a` is **rupees lost per unit held long** in one scenario (a gain is negative). Evidence: the
  NIFTY 23-Nov future's scenario 13/14 value (price down one full range) is 2339.32 against a per-series
  `scanRate/priceScan` of 2323.74 rupees (9.3 % of the 24,986.40 spot, so per unit, not per 65-unit lot), the 1/3
  and 2/3 scenarios scale with it (786.25, 1562.79), and every portfolio's `cvf` is 1.00. The extreme scenarios
  15/16 already carry their weight (1627.32 = 0.70 x the mean full-range move), so values are used as published.
* option and future prices `p` are rupees per unit; the composite delta `ra/d` is delta per unit;
* calendar spread `dSpread/rate/val` is rupees per spread, one spread being one unit of delta on each leg
  (NIFTY 443.00 per unit: 1.8 % of spot; per lot it would be absurdly small);
* `somTiers/tier/rate/val` is rupees per short option unit (NIFTY's and BANKNIFTY's are 0 in this file: a real
  zero, not a missing value; nothing divides by it).

Positions are units (not lots); rupees = units x rupees-per-unit. Every amount is an exact Decimal: arithmetic runs in
a context that traps Inexact, so nothing is ever rounded silently. The one rounding is `span_paise` (see there).

The SPAN elements, per combined commodity:

    scan            scanning risk. Legs are grouped by the scan tier (`scanTiers`) whose expiry range holds their
                    expiry; per tier, the worst of the 16 scenarios of sum(units x ra[s]), floored at 0 (a book
                    that gains in every scenario has no scan risk); the tiers' results are added (tiered scanning).
    intra_spread    calendar spread charge. Net delta per expiry = sum(units x composite delta). The file's
                    `dSpread`s are applied in priority order (spread number): legs on side A must all have one
                    delta sign and side B the other; spreads formed = min over legs of |remaining delta| / weight;
                    charge += spreads x rate (charge method F, a flat rate per spread); each leg's remaining delta
                    shrinks by spreads x weight.
    inter_credit    inter-commodity spread credit: always 0 here. `interSpreads` is not read (Task 2: none
                    involves an index commodity); a book is one commodity.
    som             short option minimum, `somMeth` GROSS: every short option unit (not netted against longs) x
                    the rate of the som tier holding its expiry.
    long_option_value / short_option_value
                    sum(units x price) over long / short options (futures carry none).
    span            max(scan + intra_spread - inter_credit - long_option_value + short_option_value, som), floored
                    at 0. This is the composition the task brief prescribes; CME's general form subtracts net option
                    value after the max with SOM. The two agree whenever SOM does not bind (always, while NIFTY and
                    BANKNIFTY carry a 0 SOM rate); Task 4's comparison with Kite is where it is confirmed.

A leg whose contract is not in the file is never guessed: it is listed in `missing`, every amount is Decimal NaN
(it cannot pass for a margin, and any comparison with it raises), and `span_paise` raises `SpanMissingError`. The
caller steps down to the next margin source. A file this module does not understand (an unknown charge or SOM
method, a contract outside every tier, a cvf other than 1) raises `SpanCalcError`; callers step down on that too.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Context, Decimal, DivisionByZero, Inexact, InvalidOperation, Overflow, localcontext

from stockify import numerics
from stockify.fno.span_file import SCENARIOS, CombinedCommodity, ContractKey, Tier

_RATE_CLASS = "1"  # the only rate class the reader keeps (risk arrays, spread and SOM rates)
_PF_TYPE = {"FUT": "FUT", "CE": "OOP", "PE": "OOP"}
# Exact arithmetic: 50 digits is far beyond any book (units ~1e7 x rupees ~1e6 with 6 decimals), and Inexact traps.
_EXACT = Context(prec=50, traps=[Inexact, InvalidOperation, DivisionByZero, Overflow])
_NAN = Decimal("NaN")
_ZERO = Decimal(0)


class SpanCalcError(ValueError):
    """The SPAN parameters cannot be applied to this book exactly; the caller steps down."""


class SpanMissingError(SpanCalcError):
    """A result with missing contracts has no margin to convert."""


@dataclass(frozen=True)
class SpanResult:
    """Rupees, exact. With `missing` non-empty every amount is NaN."""
    scan: Decimal
    intra_spread: Decimal
    inter_credit: Decimal
    som: Decimal
    long_option_value: Decimal
    short_option_value: Decimal
    span: Decimal
    missing: list[ContractKey] = field(default_factory=list)


def _dec(text: str, what: str) -> Decimal:
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise SpanCalcError(f"{what}: not a decimal: {text!r}") from exc
    if not value.is_finite():
        raise SpanCalcError(f"{what}: not finite: {text!r}")
    return value


def _tier_for(tiers: tuple[Tier, ...], expiry: str, what: str) -> Tier:
    """The tier whose expiry range holds `expiry`; else the tier without a range (tier 0, the whole commodity)."""
    for tier in tiers:
        if tier.start is not None and tier.end is not None and tier.start <= expiry <= tier.end:
            return tier
    for tier in tiers:
        if tier.start is None and tier.end is None:
            return tier
    raise SpanCalcError(f"{what}: no tier holds expiry {expiry}")


def _net(legs: list[tuple[ContractKey, int]]) -> dict[ContractKey, int]:
    net: dict[ContractKey, int] = {}
    for key, units in legs:
        if isinstance(units, bool) or not isinstance(units, int):
            raise TypeError(f"{key}: units must be an int, got {units!r}")
        net[key] = net.get(key, 0) + units
    return {k: u for k, u in net.items() if u != 0}


def _scan(cc: CombinedCommodity, book: dict[ContractKey, int]) -> Decimal:
    groups: dict[int | None, list[Decimal]] = {}  # scan tier -> per-scenario loss, rupees
    for key, units in book.items():
        tier = _tier_for(cc.scan_tiers, key[1], f"{cc.code} scan tier").number if cc.scan_tiers else None
        losses = groups.setdefault(tier, [_ZERO] * SCENARIOS)
        ra = cc.contracts[key].risk_array
        for s in range(SCENARIOS):
            losses[s] += units * _dec(ra[s], f"{key} ra[{s + 1}]")  # units x rupees/unit = rupees
    return sum((max(max(losses), _ZERO) for losses in groups.values()), _ZERO)  # rupees, each tier floored at 0


def _intra_spread(cc: CombinedCommodity, book: dict[ContractKey, int]) -> Decimal:
    delta: dict[str, Decimal] = {}  # expiry -> net delta, in units of the underlying
    for key, units in book.items():
        delta[key[1]] = delta.get(key[1], _ZERO) + units * _dec(cc.contracts[key].composite_delta, f"{key} delta")
    charge = _ZERO  # rupees
    for spread in sorted(cc.calendar_spreads, key=lambda sp: sp.number):
        if not spread.legs:
            continue
        legs = [(leg, _dec(leg.weight, f"{cc.code} spread {spread.number} weight")) for leg in spread.legs]
        first = delta.get(legs[0][0].expiry, _ZERO)
        if first == 0:
            continue
        sign_a = (1 if first > 0 else -1) * (1 if legs[0][0].side == "A" else -1)  # the sign side A's legs need
        counts = []
        for leg, weight in legs:
            d = delta.get(leg.expiry, _ZERO)
            want = sign_a if leg.side == "A" else -sign_a
            if d == 0 or (d > 0) != (want > 0):
                break
            if weight <= 0:
                raise SpanCalcError(f"{cc.code} spread {spread.number}: weight {leg.weight}")
            try:
                counts.append(abs(d) / weight)  # delta units / (delta units per spread) = spreads
            except Inexact as exc:
                raise SpanCalcError(f"{cc.code} spread {spread.number}: spreads not exact") from exc
        else:
            n = min(counts)  # spreads
            if n == 0:
                continue
            if spread.charge_method != "F":
                raise SpanCalcError(f"{cc.code} spread {spread.number}: charge method {spread.charge_method!r}")
            rate = spread.rates.get(_RATE_CLASS)
            if rate is None:
                raise SpanCalcError(f"{cc.code} spread {spread.number}: no rate class {_RATE_CLASS}")
            charge += n * _dec(rate, f"{cc.code} spread {spread.number} rate")  # spreads x rupees/spread = rupees
            for leg, weight in legs:
                want = sign_a if leg.side == "A" else -sign_a
                delta[leg.expiry] -= want * n * weight  # toward zero: delta units
    return charge


def _som(cc: CombinedCommodity, book: dict[ContractKey, int]) -> Decimal:
    shorts = [(k, u) for k, u in book.items() if k[0] != "FUT" and u < 0]
    if not shorts:
        return _ZERO
    if cc.som_method != "GROSS":
        raise SpanCalcError(f"{cc.code}: short option minimum method {cc.som_method!r}")
    som = _ZERO  # rupees
    for key, units in shorts:
        tier = _tier_for(cc.som_tiers, key[1], f"{cc.code} som tier")
        rate = tier.rates.get(_RATE_CLASS)
        if rate is None:
            raise SpanCalcError(f"{cc.code} som tier {tier.number}: no rate class {_RATE_CLASS}")
        som += -units * _dec(rate, f"{cc.code} som rate")  # short units x rupees/unit (0 is a real rate) = rupees
    return som


def span_requirement(cc: CombinedCommodity, legs: list[tuple[ContractKey, int]]) -> SpanResult:
    """The SPAN requirement of `legs` (contract key, signed units; repeated keys are netted) in `cc`, exact rupees."""
    book = _net(legs)
    missing = [key for key in book if key not in cc.contracts]
    if missing:
        return SpanResult(_NAN, _NAN, _NAN, _NAN, _NAN, _NAN, _NAN, missing)
    for pf_type in sorted({_PF_TYPE[key[0]] for key in book}):
        cvf = cc.cvf.get(pf_type)
        if cvf is None or _dec(cvf, f"{cc.code} {pf_type} cvf") != 1:
            raise SpanCalcError(f"{cc.code} {pf_type}: contract value factor {cvf!r}; only 1 (one unit) is handled")
    with localcontext(_EXACT):
        scan = _scan(cc, book)
        intra = _intra_spread(cc, book)
        inter = _ZERO
        som = _som(cc, book)
        lov = sov = _ZERO  # rupees
        for key, units in book.items():
            if key[0] == "FUT":
                continue
            value = units * _dec(cc.contracts[key].price, f"{key} price")  # units x rupees/unit = rupees
            if units > 0:
                lov += value
            else:
                sov -= value
        span = max(scan + intra - inter - lov + sov, som, _ZERO)  # rupees
    return SpanResult(scan, intra, inter, som, lov, sov, span, [])


def span_paise(result: SpanResult) -> int:
    """Rupees -> int64 paise, rounded UP to the whole rupee (ROUND_CEILING: 152,055.01 -> 15,205,600)."""
    if result.missing or not result.span.is_finite():
        raise SpanMissingError(f"no SPAN margin: missing {result.missing}")
    if result.span < 0:
        raise SpanCalcError(f"negative SPAN {result.span}")
    rupees = int(result.span.to_integral_value(rounding=ROUND_CEILING))  # whole rupees
    return numerics.check_int64(rupees * numerics.PAISE_PER_RUPEE)  # paise
