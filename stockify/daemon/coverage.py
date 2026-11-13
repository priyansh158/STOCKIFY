"""A session-day's coverage (LW 5.1; M5.11): how much of the day's continuous trading the session actually saw.

    c = coverage(bundle, journal_body, ids, threshold)       # -> Coverage
    as_float(c)                                              # session_days.coverage
    as_json(c)                                               # session_days.coverage_json (the fraction as exact "n/d")

Continuous time is the union of the CONTINUOUS phases of the session groups of `ids` (the bundle's day). Uncovered
time is the union of the GAP intervals in the journal that are scope-all or scoped to an id in `ids`, of any reason,
cut to the continuous time; a gap that never closed runs to the end of the phase it began in. Coverage is
`1 - uncovered / continuous`, exact. It only reports: a gap is never filled or repaired (I-7), and a day under the
threshold is flagged, not changed. The frames a recorder dropped on a full queue are not in the journal and are not
counted here: they cost the recording, not the live session (parity reports them).

Time the journal does not show (I-2): continuous time in which the session saw no frame or clock record for longer
than the clock cadence plus a tolerance (`configured_bounds`: paper.json `clock_every_s` and
`coverage_gap_tolerance_s`) is uncovered too, as a gap of reason `unseen`. That covers a process that died, a failed
session, a dead engine, a restart after the close and a day with no records at all, none of which journals a GAP.
GAP, REFDATA and SETTLE records are not frames and show nothing. Without a cadence only journaled gaps count.

`journal_body` is the journal's records without its header (`journal.read(path)[1]`).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from fractions import Fraction

from stockify.config import loader
from stockify.data import packs

_NS = 1_000_000_000


class CoverageError(ValueError):
    """A coverage that cannot be worked out: ids the bundle does not hold, or no continuous time at all."""


@dataclass
class Coverage:
    fraction: Fraction
    covered_s: int
    continuous_s: int
    gaps: list[dict] = field(default_factory=list)
    below_threshold: bool = False


def configured_threshold() -> Fraction:
    """`coverage_threshold` of paper.json (a plain decimal string in (0, 1]), exact."""
    return Fraction(loader.load_json("paper.json")["coverage_threshold"])


def configured_bounds() -> tuple[int, int]:
    """(clock cadence ns, tolerance ns) of paper.json: a record is expected at least this often, plus this much."""
    doc = loader.load_json("paper.json")
    return int(doc["clock_every_s"]) * _NS, int(doc["coverage_gap_tolerance_s"]) * _NS


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(s for s in spans if s[0] < s[1]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _length(spans: list[tuple[int, int]]) -> int:
    return sum(b - a for a, b in spans)


def _intersect(spans: list[tuple[int, int]], within: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = []
    for a, b in spans:
        for c, d in within:
            lo, hi = max(a, c), min(b, d)
            if lo < hi:
                out.append((lo, hi))
    return _merge(out)


def _continuous(bundle: dict, ids: set[int]) -> list[tuple[int, int]]:
    rows = {int(i["id"]): i for i in bundle["instruments"]}
    missing = sorted(ids - rows.keys())
    if not ids or missing:
        raise CoverageError(f"the bundle does not hold the instruments {missing or sorted(ids)}")
    groups = {rows[i]["session_group"] for i in ids}
    spans = [(p["start_ns"], p["end_ns"]) for day in bundle["days"] for g in sorted(groups)
             for p in day["groups"].get(g, []) if p["phase"] == "CONTINUOUS"]
    merged = _merge(spans)
    if not merged:
        raise CoverageError("the bundle has no continuous phase for these instruments")
    return merged


def _gaps(journal_body: bytes, ids: set[int], continuous: list[tuple[int, int]]) -> list[dict]:
    """The journal's GAP intervals that touch `ids`, a start paired with the next end of the same reason and scope."""
    open_: dict[tuple[str, int], int] = {}
    found: list[dict] = []
    off = 0
    while off < len(journal_body):
        kind, size = struct.unpack_from("<HH", journal_body, off)
        if kind == packs.GAP_K:
            rec, _ = packs.decode(journal_body, off)
            iid = rec["instrument_id"] if rec["scope"] else 0
            if iid == 0 or iid in ids:
                key = (rec["reason"], iid)
                if rec["phase"] == 0:
                    open_.setdefault(key, rec["t_event"])        # a repeated start keeps the first
                elif key in open_:
                    found.append({"reason": key[0], "scope": "all" if iid == 0 else f"instrument:{iid}",
                                  "start_ns": open_.pop(key), "end_ns": rec["t_event"], "closed": True})
        off += size
    for (reason, iid), start in open_.items():                   # still open: to the end of the phase it began in
        end = next((b for a, b in continuous if a <= start < b), continuous[-1][1])
        found.append({"reason": reason, "scope": "all" if iid == 0 else f"instrument:{iid}",
                      "start_ns": start, "end_ns": max(end, start), "closed": False})
    found.sort(key=lambda g: (g["start_ns"], g["end_ns"], g["reason"], g["scope"]))
    for g in found:
        g["counted_s"] = _length(_intersect([(g["start_ns"], g["end_ns"])], continuous)) // _NS
    return found


_NOT_FRAMES = (packs.GAP_K, packs.REFDATA_K, packs.SETTLE_K)


def _unseen(journal_body: bytes, continuous: list[tuple[int, int]], limit_ns: int) -> list[dict]:
    """The stretches of continuous time between two frame or clock records (or the day's edges and one) further apart
    than `limit_ns`."""
    times: list[int] = []
    off = 0
    while off < len(journal_body):
        kind, size = struct.unpack_from("<HH", journal_body, off)
        if kind not in _NOT_FRAMES:
            times.append(packs.decode(journal_body, off)[0]["t_event"])
        off += size
    times.sort()
    first, last = continuous[0][0], continuous[-1][1]
    before = [t for t in times if t <= first]           # the last record at or before the open starts the day covered
    after = [t for t in times if t >= last]
    marks = [before[-1] if before else first, *[t for t in times if first < t < last],
             after[0] if after else last]
    out = []
    for a, b in zip(marks, marks[1:]):
        if b - a > limit_ns:
            span = _intersect([(a, b)], continuous)
            if span:
                out.append({"reason": "unseen", "scope": "all", "start_ns": a, "end_ns": b, "closed": True})
    return out


def coverage(bundle: dict, journal_bytes: bytes, ids: set[int], threshold: Fraction, *,
             cadence_ns: int | None = None, tolerance_ns: int = 0) -> Coverage:
    continuous = _continuous(bundle, set(ids))
    gaps = _gaps(journal_bytes, set(ids), continuous)
    if cadence_ns is not None:
        gaps += _unseen(journal_bytes, continuous, cadence_ns + tolerance_ns)
        gaps.sort(key=lambda g: (g["start_ns"], g["end_ns"], g["reason"], g["scope"]))
        for g in gaps:
            g.setdefault("counted_s", _length(_intersect([(g["start_ns"], g["end_ns"])], continuous)) // _NS)
    uncovered = _length(_intersect(_merge([(g["start_ns"], g["end_ns"]) for g in gaps]), continuous))
    total = _length(continuous)
    fraction = Fraction(total - uncovered, total)
    return Coverage(fraction=fraction, covered_s=(total - uncovered) // _NS, continuous_s=total // _NS, gaps=gaps,
                    below_threshold=fraction < threshold)


def as_float(c: Coverage) -> float:
    return c.fraction.numerator / c.fraction.denominator


def as_json(c: Coverage) -> dict:
    return {"fraction": f"{c.fraction.numerator}/{c.fraction.denominator}", "covered_s": c.covered_s,
            "continuous_s": c.continuous_s, "below_threshold": c.below_threshold, "gaps": c.gaps}
