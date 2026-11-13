"""Compare two run folders' deterministic artifacts (M2.7; used heavily in M3).

    python -m stockify compare-artifacts <runA> <runB> [--tol-paise 0]

CSV files are compared row by row; with `--tol-paise N`, integer columns
whose names end in `_paise` (or the charge components) may differ by up to N.
`metrics.json` floats must agree within a relative 1e-12 (A4 §9.3).
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

CSVS = ("orders.csv", "order_events.csv", "fills.csv", "charges.csv", "positions_eod.csv", "equity.csv",
        "margin_daily.csv")
MONEY = {"brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp", "other", "total"}


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _money(col: str) -> bool:
    return col.endswith("_paise") or col in MONEY


def _close(a, b, rel: float) -> bool:  # noqa: ANN001
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k], rel) for k in a)
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            return a is b
        return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=0.0)
    return a == b


def compare(a: Path, b: Path, tol_paise: int = 0, rel: float = 1e-12) -> list[str]:
    problems: list[str] = []
    for name in CSVS:
        pa, pb = a / name, b / name
        if not pa.exists() or not pb.exists():
            problems.append(f"{name}: missing in {'A' if not pa.exists() else 'B'}")
            continue
        ra, rb = _rows(pa), _rows(pb)
        if len(ra) != len(rb):
            problems.append(f"{name}: {len(ra)} rows vs {len(rb)}")
        for i, (x, y) in enumerate(zip(ra, rb), start=1):
            if x.keys() != y.keys():
                problems.append(f"{name}: different columns")
                break
            for col in x:
                if x[col] == y[col]:
                    continue
                if tol_paise and _money(col) and x[col].lstrip("-").isdigit() and y[col].lstrip("-").isdigit() \
                        and abs(int(x[col]) - int(y[col])) <= tol_paise:
                    continue
                problems.append(f"{name} row {i} {col}: {x[col]!r} vs {y[col]!r}")
    ma = json.loads((a / "metrics.json").read_text(encoding="utf-8"))
    mb = json.loads((b / "metrics.json").read_text(encoding="utf-8"))
    for k in sorted(set(ma) | set(mb)):
        if not _close(ma.get(k), mb.get(k), rel):
            problems.append(f"metrics.json {k}: {ma.get(k)!r} vs {mb.get(k)!r}")
    la = (a / "events_log.txt").read_bytes()
    lb = (b / "events_log.txt").read_bytes()
    if la != lb:
        problems.append("events_log.txt differs")
    return problems
