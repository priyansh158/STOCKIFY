"""The measured numbers for the public write-up (OM 6.1, M6.12).

    python -m stockify tools measure [--json]

Reads only the lab's stores and run folders (nothing is run, nothing is written) and returns, for each number, the
same four things: `value`, `n` (the sample size), `source` (the table or report it comes from) and `needs` (None, or
what is missing). A number whose sample is missing or short has `value` None and the text says "not measured yet":
a number is never printed without its sample. Windows only, cross-platform determinism is not measured (D-26).
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
from pathlib import Path

from stockify import paths, store

MIN_LIVE_DAYS = 5          # M6.12 gate (a): five live days of journal parity and of recorder parity
NOT_MEASURED = "not measured yet"
_RERUN = re.compile(r"^(?P<src>.+)-rerun(?:-\d+)?$")


def _entry(value, n: int, source: str, needs: str | None) -> dict:
    return {"value": None if needs else value, "n": int(n), "source": source, "needs": needs}


def _live_days_needed(n: int) -> str | None:
    return None if n >= MIN_LIVE_DAYS else f"{MIN_LIVE_DAYS} live days: {n} so far"


def _parity_rows(kind: str) -> list[dict]:
    rows = store._all("SELECT trade_date, outcome, summary_json FROM parity_results WHERE kind = ? "
                      "ORDER BY trade_date, session_id", (kind,))
    return [{"day": r["trade_date"], "outcome": r["outcome"], "summary": json.loads(r["summary_json"] or "{}")}
            for r in rows]


def _journal() -> dict:
    rows = _parity_rows("journal")
    value = {"passed": sum(r["outcome"] == "pass" for r in rows), "total": len(rows)}
    return _entry(value, len(rows), "parity_results (kind journal)",
                  _live_days_needed(len({r["day"] for r in rows})))


def _recorder() -> tuple[dict, dict]:
    rows = [r for r in _parity_rows("recorder") if r["outcome"] == "pass"]
    needs = _live_days_needed(len({r["day"] for r in rows}))
    src = "parity_results (kind recorder)"
    nav = None
    if rows:
        nav = int(statistics.median(abs(int(r["summary"]["pnl_diff_paise"])) for r in rows))
    live = sum(int(r["summary"].get("fills_live", 0)) for r in rows)
    matched = sum(int(r["summary"].get("fills_matched", 0)) for r in rows)
    fills_needs = needs or (None if live else "no live fills on the recorded days: 0 so far")
    return (_entry(nav, len(rows), src, needs),
            _entry({"matched": matched, "live": live}, len(rows), src, fills_needs))


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
    needs = None if total else "no re-run with both determinism.json files: 0 so far"
    return _entry({"reproduced": same, "total": total}, total, "runs (role rerun) and determinism.json", needs)


def _margin() -> dict:
    src = "margin scorecard (staircase against the broker's answers)"
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
    except Exception as e:             # a store or config that cannot be read is "not measured", not a crash
        return _entry(None, 0, src, f"the scorecard could not be read ({type(e).__name__})")
    if best is None:
        return _entry(None, 0, src, "no stored answers: 0 so far")
    return _entry({"step": best[0], "median_error_paise": float(best[1])}, best[2], src, None)


def collect() -> dict:
    nav, fills = _recorder()
    return {
        "determinism_rerun": _determinism_rerun(),
        "determinism_goldens": _entry(None, 0, "golden scenarios (oracle against engine)",
                                      "no stored oracle-against-engine result on the goldens: 0 so far"),
        "determinism_cross_platform": _entry(None, 0, "none",
                                             "Windows only, cross-platform not measured (D-26)"),
        "journal_parity": _journal(),
        "recorder_parity_nav": nav,
        "recorder_parity_fills": fills,
        "margin_error": _margin(),
        "throughput_full_day": _entry(None, 0, "engine benchmark (M3.13)",
                                      "no full recorded day benchmarked: 0 so far (D-48)"),
    }


LABELS = {
    "determinism_rerun": "Re-runs reproducing determinism.json byte for byte",
    "determinism_goldens": "Oracle against engine on the goldens",
    "determinism_cross_platform": "Determinism across platforms",
    "journal_parity": "Journal parity (days passed of days)",
    "recorder_parity_nav": "Recorder against live, median absolute NAV difference (paise)",
    "recorder_parity_fills": "Recorder against live, fills matched",
    "margin_error": "Margin staircase error against the broker's answers (median, paise)",
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


def privacy_problems(text: str) -> list[str]:
    """What in `text` must not be published: a folder, the home folder, the user name, a path, a Kite or token word."""
    low = text.lower()
    problems = [f"names a folder ({label})" for label, p in (("lab", paths.lab_dir()), ("home", Path.home()))
                if str(p).lower() in low]
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if len(user) >= 3 and user.lower() in low:
        problems.append("names the user")
    if re.search(r"[a-z]:\\|/users/|/home/", low):
        problems.append("looks like a path")
    for word in ("kite", "token"):
        if word in low:
            problems.append(f"mentions {word}")
    return problems


def main(as_json: bool = False, out=None) -> int:
    """Print the table (or the JSON); exit 1 and print nothing but the reason if the text fails the privacy scan."""
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
