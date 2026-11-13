"""The bucketing of A4 §2.2 as two pure functions, called by the runner's `_build_bars` and by the chart's display bars
(`stockify.reporting.chartbars`), so a chart's bars for a tick dataset are the bars the strategy was given.

Units: times are UTC epoch ns (`anchor_ns`, `t_ns`, the result); `interval_s` is whole seconds and is turned into
ns here, once; volume is units (shares or contracts).
"""
from __future__ import annotations

NS_PER_S = 1_000_000_000


def bucket_start(anchor_ns: int, t_ns: int, interval_s: int) -> int:
    """Start (ns) of the `interval_s` bucket holding `t_ns`, counted from `anchor_ns` (the session's CONTINUOUS
    start). `t_ns` is at or after the anchor: the caller skips earlier snapshots."""
    step = interval_s * NS_PER_S
    return anchor_ns + (t_ns - anchor_ns) // step * step


def volume_delta(vol: int | None, prev_vol: int | None) -> int:
    """What one snapshot adds to a bar's volume. `vol` is the day's cumulative volume, `prev_vol` the one of the
    snapshot before it (None for the first of a day or after a gap): the first snapshot contributes its whole day
    volume, a decrease contributes 0, and no volume (an index) contributes 0."""
    dv = vol if prev_vol is None else max(0, vol - prev_vol) if vol is not None else 0
    return dv or 0
