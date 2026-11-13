"""Display bars for a chart: the bars a run saw, read back from its own pack or journal (CH 6.9).

Reads packs and journals only; nothing is written and no database is opened. If the stream holds `BAR` records for
the instrument, those are the bars (exactly what the strategy was given). Otherwise bars are built from the
`TICK_*` and `INDEX` snapshots with the runner's own helpers (`stockify.oracle.bars`), anchored at the instrument's
session group's CONTINUOUS start of each IST date. A bucket with no snapshot makes no bar (I-7); real and built bars
never mix (ST 5.9: real bars win).

Units: OHLC in paise, volume in units, `start_ns` in UTC epoch ns, `interval_s` in whole seconds.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NamedTuple

from stockify import timeutil
from stockify.data import packs
from stockify.oracle.bars import bucket_start, volume_delta

DEFAULT_INTERVAL_S = 60


class Bar(NamedTuple):
    start_ns: int
    open: int
    high: int
    low: int
    close: int
    volume: int


class BarFold:
    """The bars of one instrument, folded record by record: `feed` records in stream order (as often as you like, in
    any chunks) and `result()` is what `bars_from_records` returns for all the records fed so far. `copy()` is an
    independent fold, so a cached one can be continued without disturbing readers of the old one."""

    def __init__(self, instrument_id: int, *, interval_s: int | None, anchors: Mapping[str, int]) -> None:
        if interval_s is not None and (isinstance(interval_s, bool) or not isinstance(interval_s, int) or interval_s <= 0):
            raise ValueError(f"interval_s must be a whole number of seconds above 0, not {interval_s!r}")
        self.instrument_id = instrument_id
        self.interval_s = interval_s
        self.anchors = anchors
        self._isec = interval_s or DEFAULT_INTERVAL_S
        self._real: dict[int, list[Bar]] = {}
        self._acc: dict[int, list[int]] = {}
        self._day: str | None = None
        self._prev_vol: int | None = None

    def copy(self) -> BarFold:
        c = BarFold(self.instrument_id, interval_s=self.interval_s, anchors=self.anchors)
        c._real = {k: list(v) for k, v in self._real.items()}
        c._acc = {k: list(v) for k, v in self._acc.items()}
        c._day, c._prev_vol = self._day, self._prev_vol
        return c

    def feed(self, records: Iterable[dict]) -> None:
        for rec in records:
            self.feed_one(rec)

    def feed_one(self, rec: dict) -> None:
        kind = rec["kind"]
        iid = self.instrument_id
        if kind == "GAP":                                   # a gap forgets the last snapshot, as the runner does
            if rec["scope"] != 1 or rec["instrument_id"] == iid:
                self._prev_vol = None
            return
        if rec["instrument_id"] != iid:
            return
        if kind == "BAR":
            self._real.setdefault(rec["interval_s"], []).append(
                Bar(rec["start_ns"], rec["open"], rec["high"], rec["low"], rec["close"], rec["volume"]))
            return
        if kind == "INDEX":
            price, vol = rec["value"], None
        elif kind in ("TICK_QUOTE", "TICK_FULL"):
            price, vol = rec["ltp"], rec["volume"]
        else:
            return
        t = rec["t_event"]
        d = timeutil.ns_to_ist(t).date().isoformat()
        if d != self._day:
            self._day, self._prev_vol = d, None
        dv = volume_delta(vol, self._prev_vol)
        if vol is not None:                                 # an index is not a snapshot: it leaves the baseline alone
            self._prev_vol = vol
        anchor = self.anchors.get(d)
        if anchor is None or t < anchor:
            return
        start = bucket_start(anchor, t, self._isec)
        b = self._acc.get(start)
        if b is None:
            self._acc[start] = [price, price, price, price, dv]
        else:
            b[1], b[2], b[3] = max(b[1], price), min(b[2], price), price
            b[4] += dv

    def result(self) -> tuple[list[Bar], int | None]:
        if self._real:
            chosen = self.interval_s if self.interval_s in self._real else min(self._real)
            return list(self._real[chosen]), chosen
        return [Bar(s, *self._acc[s]) for s in sorted(self._acc)], None


def bars_from_records(records: Iterable[dict], instrument_id: int, *, interval_s: int | None,
                      anchors: Mapping[str, int]) -> tuple[list[Bar], int | None]:
    """(bars, interval) of one instrument from decoded pack or journal records in stream order.

    With `BAR` records present the bars are returned as they are and the interval is theirs (`interval_s`, or the
    smallest present when None). Otherwise the bars are built at `interval_s` (60 when None) and the second value
    is None; `anchors` maps an IST date to the CONTINUOUS start (ns) of that day, and a day without one makes no bars.
    """
    fold = BarFold(instrument_id, interval_s=interval_s, anchors=anchors)
    fold.feed(records)
    return fold.result()


def anchors_from_bundle(bundle: dict, instrument_id: int) -> dict[str, int]:
    """IST date to the CONTINUOUS start (ns) of the instrument's session group, over the bundle's `days`,
    `prev_days` and `next_days` (the runner's `continuous_start`). A date with no CONTINUOUS phase is left out."""
    row = next(i for i in bundle["instruments"] if i["id"] == instrument_id)
    out: dict[str, int] = {}
    for day in [*(bundle.get("prev_days") or []), *bundle["days"], *(bundle.get("next_days") or [])]:
        group = row["session_group"]
        if group == "CM_CAS":
            group = "CM_CAS" if day["date"] in row["cas_dates"] else "CM"
        starts = [p["start_ns"] for p in day["groups"][group] if p["phase"] == "CONTINUOUS"]
        if starts:
            out[day["date"]] = min(starts)
    return out


def records_of_pack(sha256: str) -> list[dict]:
    """Every record of the stored pack `sha256` (a bad hash raises `PackError`)."""
    return packs.read(packs.pack_path(sha256))[1]


def records_of_journal(path: str | Path) -> list[dict]:
    """The whole records of a journal, open or finalised; a torn tail is ignored, as `journal.read` does."""
    from stockify.daemon import journal
    _, body, _ = journal.read(Path(path))
    out, off = [], 0
    while off < len(body):
        rec, off = packs.decode(body, off)
        out.append(rec)
    return out
