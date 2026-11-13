"""IWB 5.1: `python -m stockify margin live-report [--from D] [--to D]`: where Kite's live margin answers and the
staircase disagree, from the `margin_live` log the daemon's poller writes (`daemon/margin_poller.py`).

Read-only: the lab is opened `mode=ro` and nothing is written. A row's gap is ours - Kite's (negative: the model
asks for less than Kite); Kite's figure is span + exposure + additional (D-62: no premium). Medians are over the
absolute gap, linear interpolation between the two nearest ranks (`margin_check.quantile`, exact integers),
shown rounded half up. Groups: portfolio type, underlying, IST hour of the request, and the age of the SPAN file in
force at the request (the file's created time to the request). Rows that are errors are counted, not grouped.

The L2 column is experimental and computed here in Python only; no engine, run or journal uses it. L2 is the
staircase total corrected by the median Kite / staircase ratio of the EARLIER answers of the same type and
underlying (no look-ahead; the first answer of a group has none to learn from and is left out of the L2 figures). The
earlier answers are every one in the log before it, whatever the report's --from and --to say, so the same answer
has the same L2 in any window; the window only picks which answers are reported.
It says how much of the gap a simple learned correction would remove, which is what the deferred L2 (D-69) would be.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from fractions import Fraction

from stockify import timeutil
from stockify.fno.margin_check import MEDIAN, quantile

AGE_BUCKETS = (("under 1 h", 3_600), ("1 h to 24 h", 86_400), ("over 24 h", None))
UNKNOWN = "unknown"


def _half_up(x: Fraction) -> int:
    return int((x * 2 + 1) // 2)


def _market(row: dict) -> dict:
    try:
        m = json.loads(row.get("market_json") or "{}")
    except ValueError:
        return {}
    return m if isinstance(m, dict) else {}


def _age_bucket(age) -> str:  # noqa: ANN001
    if not isinstance(age, int):
        return UNKNOWN
    for label, top in AGE_BUCKETS:
        if top is None or age < top:
            return label
    return UNKNOWN


def _rows() -> list[dict]:
    from stockify import store
    from stockify.fno import span_validate as sv
    try:
        with sv.read_only(store):
            every = store.margin_live_rows()
    except sqlite3.OperationalError:                     # no lab yet, or one from before the log: nothing logged
        return []
    return [r for r in every if r.get("trade_date") and r.get("step") == "current"]   # older rows have no gap


def _in_window(r: dict, start: date | None, end: date | None) -> bool:
    d = date.fromisoformat(r["trade_date"])
    return not ((start is not None and d < start) or (end is not None and d > end))


def _table(title: str, groups: dict[str, list[dict]], order: list[str] | None = None) -> list[str]:
    lines = ["", title, f"  {'':<16}{'n':>5}{'median |gap|':>18}{'median |gap| ppm':>18}{'mean gap ppm':>14}"]
    for key in order or sorted(groups):
        rows = groups.get(key)
        if not rows:
            continue
        gaps = [abs(r["gap_paise"]) for r in rows]
        ppm = [abs(r["gap_ppm"]) for r in rows if r["gap_ppm"] is not None]
        signed = [r["gap_ppm"] for r in rows if r["gap_ppm"] is not None]
        med = _half_up(quantile(gaps, MEDIAN))
        med_ppm = "–" if not ppm else str(_half_up(quantile(ppm, MEDIAN)))
        mean_ppm = "–" if not signed else str(_half_up(Fraction(sum(signed), len(signed))))
        lines.append(f"  {key:<16}{len(rows):>5}{_rupees(med):>18}{med_ppm:>18}{mean_ppm:>14}")
    return lines


def _rupees(paise: int) -> str:
    from stockify.oracle.report import _rupees as fmt
    return fmt(paise)


def _l2(history: list[dict], window: list[dict]) -> list[str]:
    """The experimental L2 figures (see the module doc), by portfolio type, for the answers in `window`. Each is
    corrected by every earlier answer in `history` (the whole log, not only the report's window)."""
    by_type: dict[str, list[tuple[int, int]]] = {}          # type -> [(staircase |gap| ppm, L2 |gap| ppm)]
    ratios: dict[tuple, list[Fraction]] = {}
    seen: dict[str, int] = {}
    shown = {id(r) for r in window}
    for r in sorted(history, key=lambda r: r["wall_ns"]):
        m = _market(r)
        typ, key = m.get("type", UNKNOWN), (m.get("type"), m.get("underlying"))
        ours, kite = r["ours_total_paise"], r["kite_total_paise"]
        if id(r) in shown:
            seen[typ] = seen.get(typ, 0) + 1
        if ours is None or ours <= 0 or kite is None or kite <= 0:
            continue
        earlier = ratios.get(key)
        if earlier and id(r) in shown:
            est = ours * quantile_fraction(earlier)
            by_type.setdefault(typ, []).append(
                (abs(_half_up(Fraction(ours - kite, kite) * 1_000_000)),
                 abs(_half_up((est - kite) / kite * 1_000_000))))
        ratios.setdefault(key, []).append(Fraction(kite, ours))
    lines = ["", "L2 (experimental, Python only: the staircase scaled by the median Kite / staircase ratio of the earlier "
                 "answers of the same type and underlying)",
             f"  {'':<16}{'answers':>14}{'staircase ppm':>15}{'L2 ppm':>10}"]
    if not by_type:
        return lines + ["  no group has an earlier answer to learn from yet"]
    for typ in sorted(by_type):
        pairs = by_type[typ]
        stair = _half_up(quantile([p[0] for p in pairs], MEDIAN))
        l2 = _half_up(quantile([p[1] for p in pairs], MEDIAN))
        lines.append(f"  {typ:<16}{f'{len(pairs)} of {seen[typ]}':>14}{stair:>15}{l2:>10}")
    return lines


def quantile_fraction(values: list[Fraction]) -> Fraction:
    xs = sorted(values)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def report(*, start: date | None, end: date | None) -> str:
    """The text of the live-margin disagreement report for the IST trade dates from `start` to `end` (inclusive)."""
    every = _rows()
    rows = [r for r in every if _in_window(r, start, end)]
    span = f"{start.isoformat() if start else 'the first day'} to {end.isoformat() if end else 'the last day'}"
    if not rows:
        return f"no live margin rows for {span} (the paper daemon's poller writes them while a session holds a book)\n"
    answers = [r for r in rows if not r.get("error") and r.get("gap_paise") is not None]
    history = [r for r in every if not r.get("error") and r.get("gap_paise") is not None]
    errors = len(rows) - len(answers)
    lines = [f"Live Kite margin answers against the staircase, {span} (IST trade dates)",
             f"{len(rows)} rows, {len(answers)} answers, {errors} error{'s' if errors != 1 else ''}",
             "Gap = the staircase's requirement minus Kite's span + exposure + additional (negative: we ask for less).",
             "Medians are over the absolute gap."]
    if not answers:
        return "\n".join(lines + ["", "no answers to group (every row is an error)"]) + "\n"
    by_type: dict[str, list[dict]] = {}
    by_und: dict[str, list[dict]] = {}
    by_hour: dict[str, list[dict]] = {}
    by_age: dict[str, list[dict]] = {}
    for r in answers:
        m = _market(r)
        by_type.setdefault(m.get("type") or UNKNOWN, []).append(r)
        by_und.setdefault(m.get("underlying") or UNKNOWN, []).append(r)
        hour = timeutil.ns_to_ist(r["wall_ns"]).hour
        by_hour.setdefault(f"{hour:02d}:00", []).append(r)
        by_age.setdefault(_age_bucket(m.get("span_age_s")), []).append(r)
    lines += _table("by portfolio type", by_type)
    lines += _table("by underlying", by_und)
    lines += _table("by IST hour", by_hour)
    lines += _table("by SPAN file age", by_age, [b[0] for b in AGE_BUCKETS] + [UNKNOWN])
    lines += _l2(history, answers)
    expiry = [r for r in answers if _market(r).get("expiry_day")]
    lines += ["", f"Answers with a leg expiring that day (kept for the X-9 expiry-day add-on check): {len(expiry)}"]
    return "\n".join(lines) + "\n"
