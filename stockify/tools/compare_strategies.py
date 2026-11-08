"""Side-by-side comparison of strategy runs on the same days (M4.13): `python -m stockify tools compare-strategies`.

The point (M4.13): run the same days as `eod_short_straddle` (NSE end-of-day files, next-day-close fills) and as
the intraday `short_straddle_stop` / `iron_fly` (recorded chains, quote fills, SPAN-lite margin), and put them
side by side with their labels. The gap between the end-of-day and the intraday straddle is a direct measure of how
much the end-of-day approximations distort option results; the iron fly against the straddle shows what the wings
do to the margin and the risk.

Config (JSON, schema `stockify.compare/1`):

    {"schema": "stockify.compare/1", "name": "straddle_vs_ironfly_recorded",
     "runs": [{"title": "End-of-day straddle (NSE files)", "role": "eod", "config": {… a run.json …}},
              {"title": "Intraday straddle with stops", "role": "straddle", "config": {…}},
              {"title": "Intraday iron fly", "role": "iron_fly", "config": {…}}]}

`role` (optional: eod | straddle | iron_fly) picks which pairs the closing sentences compare. Each Python strategy
is wrapped in `_margin_watch.MarginWatch`, which logs the margin after every fill, since equity.csv only has the
day-close margin and intraday strategies are flat by then. Each run goes through
`stockify.runs.launch` (a normal run folder and lab.sqlite row); the report is written to
`runs/compare-<name>/compare.md`. `compare(...)` works on finished run folders, so tests need no lab data.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from stockify.oracle.report import LABEL_TEXT, _rupees
from stockify.strategies._margin_watch import LOG_PREFIX

WATCH_REF = "stockify.strategies._margin_watch:MarginWatch"

SCHEMA = "stockify.compare/1"

# Plain words for the labels the option runs carry (the report's own list covers the rest).
MORE_LABELS = {
    "margin:flat(approx)": "margin from a flat percentage of the position's value (approximate)",
    "margin:spanlite(approx)": "margin from SPAN-lite scenarios (approximate)",
    "margin_spanlite:iv_fallback": "some option volatilities for margin had to be filled in (no usable quote)",
    "fill:eod_close": "fills at the next day's closing price",
    "fill:snapshot_quote": "fills against the recorded bid and ask",
    "fill:bar": "fills from price bars",
}


class CompareError(ValueError):
    pass


@dataclass
class RunSummary:
    title: str
    role: str | None
    folder: Path
    strategy: str
    start_paise: int
    end_paise: int
    costs_paise: int
    trades: int
    max_margin_paise: int
    worst_fall: float | None
    labels: list[str]
    daily: dict[str, int] = field(default_factory=dict)  # trade_date → gain or loss that day, paise

    @property
    def made_paise(self) -> int:
        return self.end_paise - self.start_paise


def label_text(label: str) -> str:
    if label in LABEL_TEXT:
        return LABEL_TEXT[label]
    if label in MORE_LABELS:
        return MORE_LABELS[label]
    if label.startswith("margin_spanlite:"):
        cal = label.split(":", 1)[1]
        return ("SPAN-lite margin settings are placeholders, not yet calibrated against Kite"
                if cal == "placeholder" else f"SPAN-lite margin settings: calibration {cal}")
    return label


def summarise(title: str, folder: Path, role: str | None = None) -> RunSummary:
    folder = Path(folder)
    metrics = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
    with (folder / "equity.csv").open(encoding="utf-8", newline="") as f:
        equity = list(csv.DictReader(f))
    margins = [int(r["margin_blocked_paise"]) for r in equity]          # at each day's close
    for line in (folder / "events_log.txt").read_text(encoding="utf-8").splitlines():
        if LOG_PREFIX in line:                                         # intraday, after each fill (margin watch)
            margins.append(int(line.split(LOG_PREFIX, 1)[1].split()[0]))
    daily, prev = {}, metrics["nav_start_paise"]
    for r in equity:
        nav = int(r["nav_paise"])
        daily[r["trade_date"]] = daily.get(r["trade_date"], 0) + nav - prev
        prev = nav
    s = bundle.get("strategy", {})
    ref = s.get("params", {}).get("inner_ref") if s.get("ref") == WATCH_REF else s.get("ref")  # the wrapped one
    return RunSummary(
        title=title, role=role, folder=folder, strategy=(ref or s.get("kind") or "?").split(":")[-1],
        start_paise=metrics["nav_start_paise"], end_paise=metrics["nav_end_paise"],
        costs_paise=metrics.get("costs_total_paise") or 0, trades=metrics.get("trades") or 0,
        max_margin_paise=max(margins, default=0),
        worst_fall=metrics.get("max_drawdown"), labels=sorted(manifest.get("labels", [])), daily=daily)


def _made(paise: int) -> str:
    return f"made {_rupees(paise)}" if paise >= 0 else f"lost {_rupees(-paise)}"


def _closing_sentences(runs: list[RunSummary]) -> list[str]:
    by_role = {r.role: r for r in runs if r.role}
    out = []
    eod, straddle, fly = by_role.get("eod"), by_role.get("straddle"), by_role.get("iron_fly")
    if eod and straddle:
        gap = straddle.made_paise - eod.made_paise
        side = "better" if gap > 0 else "worse"
        out.append(f"**End-of-day vs intraday, same days:** the intraday straddle {_made(straddle.made_paise)}, the "
                   f"end-of-day version {_made(eod.made_paise)}. The intraday run did {_rupees(abs(gap))} {side}. "
                   "That gap is how far the end-of-day shortcuts (one price a day, fills at the next close, no "
                   "intraday stops) move the result on these days.")
    if fly and straddle:
        saved = straddle.max_margin_paise - fly.max_margin_paise
        if straddle.max_margin_paise > 0:
            share = saved / straddle.max_margin_paise
            out.append(f"**Iron fly vs straddle, margin:** at most {_rupees(fly.max_margin_paise)} was tied up for the "
                       f"iron fly against {_rupees(straddle.max_margin_paise)} for the straddle, "
                       f"{_rupees(abs(saved))} {'less' if saved >= 0 else 'more'} ({abs(share) * 100:.0f} %). The bought "
                       "wings cap the loss, so SPAN-lite asks for less money to hold the position.")
        out.append(f"**Iron fly vs straddle, result:** the iron fly {_made(fly.made_paise)}, the straddle "
                   f"{_made(straddle.made_paise)}; the wings cost {_rupees(max(0, fly.costs_paise - straddle.costs_paise))} "
                   "more in charges and trade the bigger wins for a capped loss.")
    return out


def compare(runs: list[tuple[str, Path, str | None]], name: str = "comparison") -> str:
    """runs: (title, run folder, role) → the markdown report."""
    if not runs:
        raise CompareError("nothing to compare")
    s = [summarise(t, f, role) for t, f, role in runs]
    lines = [f"# {name}: side by side", "",
             "Simulated runs on the same days; no real orders. Money in rupees.", ""]
    for r in s:
        lines += [f"## {r.title}", "",
                  f"- Strategy: `{r.strategy}`",
                  f"- Money: {_rupees(r.start_paise)} → {_rupees(r.end_paise)}, so it {_made(r.made_paise)}",
                  f"- Charges paid: {_rupees(r.costs_paise)}; trades: {r.trades}",
                  f"- Margin: at most {_rupees(r.max_margin_paise)} tied up at any moment",
                  f"- Worst fall from a high: {'–' if r.worst_fall is None else f'{r.worst_fall * 100:.2f} %'}",
                  "- Caveats: " + ("; ".join(label_text(x) for x in r.labels) if r.labels else "none"),
                  ""]
    days = sorted({d for r in s for d in r.daily})
    lines += ["## Day by day (gain or loss each day)", "", "| Day | " + " | ".join(r.title for r in s) + " |",
              "|---|" + "---|" * len(s)]
    for d in days:
        lines.append(f"| {d} | " + " | ".join(_rupees(r.daily[d]) if d in r.daily else "–" for r in s) + " |")
    lines += ["", "## What the gaps mean", ""]
    closing = _closing_sentences(s)
    lines += [f"- {x}" for x in closing] if closing else ["- (give the runs roles eod / straddle / iron_fly to "
                                                         "compare them)"]
    return "\n".join(lines) + "\n"


def load_config(source) -> dict:  # noqa: ANN001
    doc = json.loads(Path(source).read_text(encoding="utf-8")) if not isinstance(source, dict) else source
    if doc.get("schema") != SCHEMA:
        raise CompareError(f"schema must be {SCHEMA!r}")
    if not isinstance(doc.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", doc["name"])             or doc["name"].strip(".") == "":
        raise CompareError("name: letters, digits, '.', '_' or '-' (it names the report's folder)")
    runs = doc.get("runs")
    if not isinstance(runs, list) or len(runs) < 2:
        raise CompareError("runs: at least two")
    for n, r in enumerate(runs):
        if not isinstance(r, dict) or not isinstance(r.get("title"), str) or not isinstance(r.get("config"), dict):
            raise CompareError(f"runs[{n}]: needs a title and a config")
        if r.get("role") not in (None, "eod", "straddle", "iron_fly"):
            raise CompareError(f"runs[{n}].role: one of eod, straddle, iron_fly")
    return doc


def watched(config: dict) -> dict:
    """The run config with a Python strategy wrapped in MarginWatch (its params pass through), so the intraday
    margin peak is in the log. C++ strategies are left alone (their peak falls back to the day-close margins)."""
    import copy
    out = copy.deepcopy(config)
    s = out.get("strategy", {})
    if s.get("kind") == "python" and s.get("ref") != WATCH_REF:
        out["strategy"] = {**s, "ref": WATCH_REF, "params": {"inner_ref": s["ref"], **s.get("params", {})}}
    return out


def run(config, *, impl: str = "engine") -> Path:  # noqa: ANN001
    """Launch every run of a compare config, then write runs/compare-<name>/compare.md; returns its path."""
    from stockify import paths, runs as runs_mod
    doc = load_config(config)
    done = []
    for r in doc["runs"]:
        run_id = runs_mod.launch(watched(r["config"]), impl=impl, report=False)
        done.append((r["title"], paths.runs_dir() / run_id, r.get("role")))
    out = paths.runs_dir() / f"compare-{doc['name']}"
    out.mkdir(parents=True, exist_ok=True)
    report = out / "compare.md"
    report.write_text(compare(done, doc["name"]), encoding="utf-8")
    return report
