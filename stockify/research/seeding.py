"""A seeded random stream per research purpose. Streams 1 and 2 belong to the engine (slippage, ctx.rng()), so a
research draw can never collide with a run's own numbers; the same (seed, purpose) always gives the same numbers."""
from __future__ import annotations

from datetime import date

from stockify.bundle import day_seed
from stockify.oracle.rng import Pcg32

STREAMS = {"bootstrap": 11, "pbo": 12, "random_search": 13}
ANCHOR = date(2000, 1, 3)          # a fixed date: research draws belong to no trading day


def stream(seed: int, purpose: str, anchor: date | None = None) -> Pcg32:
    """Pcg32(day_seed(seed, anchor), STREAMS[purpose]). An unknown purpose raises KeyError."""
    return Pcg32(day_seed(seed, anchor or ANCHOR), STREAMS[purpose])
