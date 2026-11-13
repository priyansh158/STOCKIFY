"""The measured numbers for the public write-up (OM 6.1, M6.12).

    python -m stockify tools measure [--json]

Reads only the lab's stores and run folders (nothing is run, nothing is written) and returns, for each number, the
same four things: `value`, `n` (the count that gates the number: days or re-runs), `source` (the table or report it
comes from) and `needs` (None, or what is missing). A number whose sample is missing or short has `value` None and
the text says "not measured yet": a number is never printed without its sample. The text is public, so it carries no
plan ids and no exception names.

Parity rows are counted in parity days, live and test together: nothing in `parity_results` or its session says
whether a day was live (D-122), so these rows stay "not measured yet" until the owner confirms five live days.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import sqlite3
import statistics
import sys
from pathlib import Path

from stockify import paths, store

MIN_LIVE_DAYS = 5          # M6.12 gate (a): five live days of journal parity and of recorder parity
MIN_RERUNS = 5
NOT_MEASURED = "not measured yet"
_RERUN = re.compile(r"^(?P<src>.+)-rerun(?:-\d+)?$")


def _entry(value, n: int, source: str, needs: str | None) -> dict:
    return {"value": None if needs else value, "n": int(n), "source": source, "needs": needs}


def _parity_needs(days: int) -> str:
    return (f"{MIN_LIVE_DAYS} live days needed; {days} parity days (live and test) so far, "
            "none confirmed as live")


def _parity_rows(kind: str) -> list[dict]:
    rows = store._all("SELECT trade_date, outcome, summary_json FROM parity_results WHERE kind = ? "
                      "ORDER BY trade_date, session_id", (kind,))
    return [{"day": r["trade_date"], "outcome": r["outcome"], "summary": json.loads(r["summary_json"] or "{}")}
            for r in rows]


def _journal() -> dict:
    rows = _parity_rows("journal")
    days = len({r["day"] for r in rows})
    value = {"passed": sum(r["outcome"] == "pass" for r in rows), "total": len(rows)}
    return _entry(value, days, "parity_results (kind journal)", _parity_needs(days))


def _recorder() -> tuple[dict, dict]:
    rows = [r for r in _parity_rows("recorder") if r["outcome"] == "pass"]
    days = len({r["day"] for r in rows})
    needs = _parity_needs(days)
    src = "parity_results (kind recorder)"
    nav = None
    if rows:
        nav = int(statistics.median(abs(int(r["summary"]["pnl_diff_paise"])) for r in rows))
    live = sum(int(r["summary"].get("fills_live", 0)) for r in rows)
    matched = sum(int(r["summary"].get("fills_matched", 0)) for r in rows)
    return _entry(nav, days, src, needs), _entry({"matched": matched, "live": live}, days, src, needs)


def _determinism_rerun() -> dict:
    same = total = 0
    for row in store.list_runs(limit=100000):
        m = _RERUN.match(row["run_id"]) if row.get("role") == "rerun" else None
        if not m:
            continue
        try:
            a = (paths.run_path(m["src"]) / "determinism.json").read_bytes()
            b = (paths.run_path(row["run_id"]) / "determinism.json").read_bytes()
        except (OSError, ValueError):
            continue
        total += 1
        same += a == b
    needs = None if total >= MIN_RERUNS else f"{MIN_RERUNS} re-runs: {total} so far"
    return _entry({"reproduced": same, "total": total}, total, "runs (role rerun) and determinism.json", needs)


def _margin() -> dict:
    src = "margin scorecard (staircase against Kite's answers)"
    if not store.margin_calibrations():
        return _entry(None, 0, src, "no stored answers: 0 so far")
    try:
        from stockify.fno import margin_check as mc
        card = mc.scorecard()
        best = None
        for step in mc.STEPS:
            errs = card.errors(card.rows, step)
            if errs:
                med = mc.quantile(errs, mc.MEDIAN)
                if best is None or med < best[1]:
                    best = (step, med, len(errs))
    except (sqlite3.Error, FileNotFoundError):     # the stores' own failures; anything else is a bug and surfaces
        return _entry(None, 0, src, "the stored answers could not be read")
    if best is None:
        return _entry(None, 0, src, "no stored answers: 0 so far")
    return _entry({"step": best[0], "median_error_paise": float(best[1])}, best[2], src, None)


def collect() -> dict:
    nav, fills = _recorder()
    return {
        "determinism_rerun": _determinism_rerun(),
        "determinism_goldens": _entry(None, 0, "golden scenarios (oracle against engine)",
                                      "no stored oracle-against-engine result on the goldens: 0 so far"),
        "determinism_cross_platform": _entry(None, 0, "none", "Windows only, cross-platform not measured"),
        "journal_parity": _journal(),
        "recorder_parity_nav": nav,
        "recorder_parity_fills": fills,
        "margin_error": _margin(),
        "throughput_full_day": _entry(None, 0, "engine benchmark",
                                      "no full recorded day benchmarked: 0 so far"),
    }


LABELS = {
    "determinism_rerun": "Re-runs reproducing determinism.json byte for byte (re-runs)",
    "determinism_goldens": "Oracle against engine on the goldens",
    "determinism_cross_platform": "Determinism across platforms",
    "journal_parity": "Journal parity, days passed of days (parity days, live and test)",
    "recorder_parity_nav": "Recorder against live, median absolute NAV difference in paise (parity days, live and test)",
    "recorder_parity_fills": "Recorder against live, fills matched (parity days, live and test)",
    "margin_error": "Margin staircase error against Kite's answers, median in paise (answers)",
    "throughput_full_day": "Engine throughput on a full recorded day",
}


def _show(name: str, value) -> str:
    if name == "determinism_rerun":
        return f"{value['reproduced']} of {value['total']}"
    if name == "journal_parity":
        return f"{value['passed']} of {value['total']}"
    if name == "recorder_parity_fills":
        return f"{value['matched']} of {value['live']}"
    if name == "margin_error":
        return f"{value['median_error_paise']:.2f} ({value['step']})"
    return str(value)


def render(got: dict) -> str:
    """A markdown table: what, the number or "not measured yet", the sample, the source."""
    lines = ["| Measure | Result | Sample | Source |", "|---|---|---|---|"]
    for name, e in got.items():
        result = f"{NOT_MEASURED} ({e['needs']})" if e["value"] is None else _show(name, e["value"])
        lines.append(f"| {LABELS[name]} | {result} | {e['n']} | {e['source']} |")
    return "\n".join(lines) + "\n"


def check_finite(obj) -> None:
    """ValueError for any NaN or infinity anywhere in the result."""
    if isinstance(obj, float) and not math.isfinite(obj):
        raise ValueError("a measured number is not finite")
    if isinstance(obj, dict):
        for v in obj.values():
            check_finite(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            check_finite(v)


_PATTERNS = (
    ("looks like an IPv4 address", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")),
    ("names localhost", re.compile(r"localhost", re.I)),
    ("looks like an email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("looks like a drive path", re.compile(r"\b[a-z]:[\\/]", re.I)),
    ("looks like a UNC path", re.compile(r"\\\\[^\s\\]+\\")),
    ("looks like a user folder", re.compile(r"[\\/](?:users|home)[\\/]", re.I)),
    ("holds a credential name", re.compile(r"KITE_|access_token|api_key|api_secret", re.I)),
    ("holds a long key-like string", re.compile(r"(?<![A-Za-z0-9+/=_-])(?=[+/=_-]*[A-Za-z0-9])[A-Za-z0-9+/=_-]{32,}")),
)


def privacy_problems(text: str) -> list[str]:
    """What in `text` must not be published: a lab or home folder, the user, home-folder or host name, a path, an
    address, an email, a credential name or a long key-like run. The broker's name in plain words is fine."""
    low = text.lower()
    names = [("the lab folder", str(paths.lab_dir())), ("the home folder", str(Path.home())),
             ("the home folder's name", Path.home().name),
             ("the user name", os.environ.get("USERNAME") or os.environ.get("USER") or ""),
             ("the host name", socket.gethostname())]
    problems = [f"names {label}" for label, s in names if len(s) >= 3 and s.lower() in low]
    problems += [why for why, rx in _PATTERNS if rx.search(text)]
    return problems


def main(as_json: bool = False, out=None) -> int:
    """Print the table (or the JSON); exit 1 and print only the reason if the text fails the privacy scan."""
    out = out or sys.stdout
    got = collect()
    check_finite(got)
    text = json.dumps(got, indent=2, sort_keys=True, allow_nan=False) + "\n" if as_json else render(got)
    problems = privacy_problems(text)
    if problems:
        print("not printed: " + "; ".join(problems), file=sys.stderr)
        return 1
    out.write(text)
    return 0
