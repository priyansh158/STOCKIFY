"""Fill-model decisions (A4 §4): where along a bar an order fills, and at what price.

Positions along a bar path are `(segment, fraction)` with exact fractions, so
"which order fills first" never depends on floating point. The runner applies
the fills; this module only decides.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from stockify import numerics

OPEN_POS = (0, Fraction(0))


def protection_limit(side: str, ref: int, bp: int, tick: int) -> int:
    """A4 §3.3: buy floor, sell ceil, on the tick grid."""
    if side == "BUY":
        return numerics.floor_div(ref * (10000 + bp), 10000 * tick) * tick
    return numerics.ceil_div(ref * (10000 - bp), 10000 * tick) * tick


def slipped(side: str, price: int, tick: int, slip: dict, rng, cap: int | None) -> int:  # noqa: ANN001
    """Adverse slippage (A4 §4.4), never beyond `cap` (the protection / limit price)."""
    kind = slip.get("kind", "none")
    if kind == "none":
        move = 0
    elif kind == "fixed_ticks":
        move = slip["k"] * tick
    elif kind == "random_ticks":
        move = rng.below(slip["k"] + 1) * tick
    elif kind == "bps":
        move = numerics.ceil_to(numerics.ceil_div(price * slip["bp"], 10000), tick)
    else:
        raise ValueError(f"slippage {kind!r}")
    out = price + move if side == "BUY" else price - move
    if cap is not None:
        out = min(out, cap) if side == "BUY" else max(out, cap)
    return out


def participation_cap(volume: int, ppm: int, lot: int) -> int:
    """A4 §4.0: floor_to(floor_div(volume·ppm, 10⁶), lot)."""
    return numerics.floor_to(numerics.floor_div(volume * ppm, 1_000_000), lot)


# ── Bar paths ────────────────────────────────────────────────────────────────

def path_points(o: int, h: int, lo: int, c: int, high_first: bool) -> list[int]:
    """P_HL = O→H→L→C, P_LH = O→L→H→C (A4 §4.1)."""
    return [o, h, lo, c] if high_first else [o, lo, h, c]


def _value(points: list[int], pos) -> Fraction:  # noqa: ANN001
    k, f = pos
    return points[k] + (points[k + 1] - points[k]) * f


def first_reach(points: list[int], x: int, down: bool, after=OPEN_POS):  # noqa: ANN001
    """Earliest position ≥ `after` where the path is ≤ x (down) or ≥ x (up); None if never."""
    ok = (lambda v: v <= x) if down else (lambda v: v >= x)
    seg0, frac0 = after
    for k in range(seg0, len(points) - 1):
        start = frac0 if k == seg0 else Fraction(0)
        if ok(_value(points, (k, start))):
            return (k, start)
        a, b = points[k], points[k + 1]
        if a != b and ok(b):
            f = Fraction(a - x, a - b)
            if f >= start:
                return (k, f)
    return None


def _extreme_after(points: list[int], pos, low: bool) -> Fraction:  # noqa: ANN001
    vals = [_value(points, pos), *points[pos[0] + 1:]]
    return min(vals) if low else max(vals)


@dataclass
class Opportunity:
    pos: tuple                 # where on the path
    price: int | None          # fill price before slippage; None = only a trigger happens
    evidence: str
    slip: bool = False         # MARKET-like: adverse slippage applies
    cap: int | None = None     # the limit the fill may not be worse than
    trigger: bool = False      # a stop triggers at `pos`
    new_limit: int | None = None
    becomes_market: bool = False


def _as_limit(points: list[int], side: str, p: int, after, touch: bool, open_price: bool):  # noqa: ANN001, ANN202
    """A limit at `p` from `after` on: marketable there, or reached later (through / touch)."""
    buy = side == "BUY"
    here = _value(points, after)
    if (here <= p) if buy else (here >= p):
        price = int(here) if open_price else p
        return after, price, ("bar_open" if after == OPEN_POS else "bar_path")
    extreme = _extreme_after(points, after, low=buy)
    if buy:
        reached = extreme <= p if touch else extreme < p
    else:
        reached = extreme >= p if touch else extreme > p
    if not reached:
        return None
    pos = first_reach(points, p, down=buy, after=after)
    return (pos, p, "bar_path") if pos is not None else None


def bar_opportunity(order, points: list[int], touch: bool, protect) -> Opportunity | None:  # noqa: ANN001
    """`protect(side, ref)` gives the protection limit of an SL-M triggered at `ref`."""
    o = points[0]
    buy = order.side == "BUY"

    if order.status == "OPEN":
        if order.from_market and not order.first_eligible_done and ((o <= order.limit) if buy else (o >= order.limit)):
            return Opportunity(OPEN_POS, o, "bar_open", slip=True, cap=order.limit)
        hit = _as_limit(points, order.side, order.limit, OPEN_POS, touch, open_price=True)
        if hit is None:
            return None
        return Opportunity(hit[0], hit[1], hit[2], cap=order.limit)

    # TRIGGER_PENDING (A4 §3.4): a buy stop triggers at the open if O ≥ s, else where the path reaches s.
    s = order.trigger
    gap = (o >= s) if buy else (o <= s)
    pos = OPEN_POS if gap else first_reach(points, s, down=not buy)
    if pos is None:
        return None
    if order.type == "SL-M":
        ref = o if gap else s                              # first price beyond the trigger
        cap = protect(order.side, ref)
        return Opportunity(pos, ref, "bar_open" if gap else "bar_path", slip=True, cap=cap, trigger=True,
                           new_limit=cap, becomes_market=True)
    p = order.price                                        # SL: a limit from the trigger point on
    hit = _as_limit(points, order.side, p, pos, touch, open_price=True)
    if hit is None:
        return Opportunity(pos, None, "", trigger=True, new_limit=p)
    return Opportunity(hit[0], hit[1], hit[2], cap=p, trigger=True, new_limit=p)
