"""DMF 6.1: compare two to six finished runs side by side (metrics, equity, drawdown, labels, manifest diff).

Reads finished runs only (it starts none, so no holdout check). Money columns are int paise; equity is NAV /
capital (unitless, 1.0 at the run's own start); drawdown is a fraction of the running peak.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from stockify import paths, store
from stockify.oracle.metrics import drawdown_series
from stockify.reporting import figures
from stockify.research import plain

# <block name="compare-limits" same-as="web/src/lib/researchHelpers.ts:compare-limits" same-as-mode="sequence" same-as-pattern='= (?P<value>\d+)'>
MIN_RUNS = 2
MAX_RUNS = 6
# </block>
FALLBACK_NOTE = "manifest not available (folder removed)"


@dataclass
class MetricRow:
    key: str
    label: str
    values: list[float | int | None]
    kind: str  # money | ratio | pct | count
    plain: str
    reasons: list[str | None] = field(default_factory=list)   # per run: why the value is withheld (D-135), else None


@dataclass
class DiffRow:
    field: str
    values: list[str | None]
    differs: bool


@dataclass
class RunComparison:
    run_ids: list[str]
    names: list[str]
    metrics: list[MetricRow]
    equity: list[dict]
    drawdown: list[dict]
    labels: dict[str, list[bool]]
    differing_labels: list[str]
    manifest_diff: list[DiffRow]
    notes: list[str] = field(default_factory=list)


def _json(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def _num(v: Any) -> float | int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if not isinstance(v, float) or math.isfinite(v) else None


def _metric_values(rows: list[dict]) -> list[tuple[str, str, str, list, list]]:
    """(key, label, kind, values, reasons) per row. A yearly figure of a run that is too short, and costs over a
    gross profit that is not above zero, are None with their plain reason (stockify.reporting.figures, D-135)."""
    raw = [_json(r.get("metrics_json"), {}) for r in rows]
    shown = [figures.present(m) for m in raw]
    ms = [s for s, _ in shown]
    withheld = [w for _, w in shown]

    def net(m: dict) -> int | None:
        a, b = _num(m.get("nav_end_paise")), _num(m.get("nav_start_paise"))
        return None if a is None or b is None else int(a) - int(b)

    def ret(m: dict) -> float | None:
        n, b = net(m), _num(m.get("nav_start_paise"))
        return None if n is None or not b else n / b

    def entry(key: str, label: str, kind: str, vals: list, mkey: str | None = None) -> tuple:
        return (key, label, kind, vals, [w.get(mkey or key) if v is None else None for v, w in zip(vals, withheld)])

    return [
        entry("net_pnl", "Net profit (₹)", "money", [net(m) for m in ms]),
        entry("return", "Return", "pct", [ret(m) for m in ms]),
        entry("cagr", "CAGR", "pct", [_num(m.get("cagr")) for m in ms]),
        entry("sharpe", "Sharpe", "ratio", [_num(m.get("sharpe")) for m in ms]),
        entry("sortino", "Sortino", "ratio", [_num(m.get("sortino")) for m in ms]),
        entry("max_drawdown", "Max drawdown", "pct", [_num(m.get("max_drawdown")) for m in ms]),
        entry("calmar", "Calmar", "ratio", [_num(m.get("calmar")) for m in ms]),
        entry("trades", "Trades", "count", [_num(m.get("trades")) for m in ms]),
        entry("win_rate", "Win rate", "pct", [_num(m.get("win_rate")) for m in ms]),
        entry("costs", "Costs (₹)", "money", [_num(m.get("costs_total_paise")) for m in ms]),
        entry("cost_share", "Costs / gross profit", "pct", [_num(m.get("cost_share_of_gross")) for m in ms],
              "cost_share_of_gross"),
    ]


def _s(v: Any) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, sort_keys=True, separators=(",", ":"))


def _manifest(row: dict) -> dict | None:
    """The run's manifest.json, or None when the folder (or the file) is gone."""
    try:
        p = paths.run_path(row["run_id"]) / "manifest.json"
    except ValueError:
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):      # missing, a folder, locked, not text: the database fallback answers
        return None
    m = _json(text, None)
    return m if isinstance(m, dict) else None


def _capital(v: Any) -> int | None:
    """A usable starting capital: a whole number of paise above zero (a bool, a string or a negative is not)."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _canonical(text: str | None) -> str:
    """The config with sorted keys and no spaces, so key order alone never reads as a difference."""
    try:
        return json.dumps(json.loads(text or "null"), sort_keys=True, separators=(",", ":"))
    except ValueError:
        return text or ""


def compare_runs(run_ids: list[str]) -> RunComparison:
    ids = list(run_ids)
    if not MIN_RUNS <= len(ids) <= MAX_RUNS or len(set(ids)) != len(ids):
        raise ValueError("compare 2 to 6 runs")
    rows = []
    for rid in ids:
        row = store.get_run(rid)
        if row is None:
            raise KeyError(rid)
        if row["status"] != "done":
            raise ValueError(f"run {rid} is {row['status']}, not done: only finished runs can be compared")
        rows.append(row)
    cfgs = [_json(r.get("config_json"), {}) for r in rows]
    names = [r.get("name") or r["run_id"] for r in rows]

    metrics = [MetricRow(k, label, vals, kind, plain.meaning(k), why)
               for k, label, kind, vals, why in _metric_values(rows)]

    equity, drawdown = [], []
    no_capital: list[str] = []
    for row, cfg in zip(rows, cfgs):
        series = store.run_nav_series(row["run_id"])
        cap = _capital(cfg.get("capital_paise"))
        if cap is None:
            cap = _capital(_json(row.get("metrics_json"), {}).get("nav_start_paise"))
        dates = [d for d, _ in series]
        navs = [n for _, n in series]
        if navs and cap is None:
            no_capital.append(f"{row.get('name') or row['run_id']}: no starting capital recorded")
        if not navs or cap is None:
            equity.append({"run_id": row["run_id"], "dates": [], "values": []})
            drawdown.append({"run_id": row["run_id"], "dates": [], "values": []})
            continue
        equity.append({"run_id": row["run_id"], "dates": dates, "values": [n / cap for n in navs]})
        drawdown.append({"run_id": row["run_id"], "dates": dates,
                         "values": drawdown_series([float(cap)] + [float(n) for n in navs])})

    per_run = [set(_json(r.get("labels_json"), [])) for r in rows]
    every = sorted(set().union(*per_run))
    labels = {lab: [lab in s for s in per_run] for lab in every}
    differing = [lab for lab in every if len(set(labels[lab])) > 1]

    notes: list[str] = list(no_capital)
    manifests = [_manifest(r) for r in rows]
    notes += [f"{r['run_id']}: {FALLBACK_NOTE}" for r, m in zip(rows, manifests) if m is None]

    def pick(row: dict, m: dict | None, key: str) -> str | None:
        return m.get(key) if m else row.get(key)

    diff: list[DiffRow] = []

    def add(name: str, values: list[str | None], always: bool = True) -> None:
        d = len(set(values)) > 1
        if always or d:
            diff.append(DiffRow(name, values, d))

    def strat(c: dict) -> dict:
        s = c.get("strategy")
        return s if isinstance(s, dict) else {}

    def params(c: dict) -> dict:
        p = strat(c).get("params")
        return p if isinstance(p, dict) else {}

    add("strategy.ref", [_s(strat(c).get("ref")) for c in cfgs])
    for k in sorted({k for c in cfgs for k in params(c)}):
        add(f"strategy.params.{k}", [_s(params(c).get(k)) for c in cfgs], always=False)
    add("fill_model", [_s(c.get("fill_model")) for c in cfgs])
    add("margin.model", [_s((c.get("margin") or {}).get("model")) for c in cfgs])
    add("config_sha256", [hashlib.sha256(_canonical(r.get("config_json")).encode("utf-8")).hexdigest() for r in rows])
    add("bundle_sha256", [pick(r, m, "bundle_sha256") for r, m in zip(rows, manifests)])
    add("packs_sha256", [_s(sorted(m["packs"])) if m and m.get("packs") else None for m in manifests])
    add("engine_build_id", [pick(r, m, "engine_build_id") for r, m in zip(rows, manifests)])

    return RunComparison(ids, names, metrics, equity, drawdown, labels, differing, diff, notes)


def _fmt(v: Any, kind: str) -> str:
    if v is None:
        return "-"
    if kind == "pct":
        return f"{v * 100:.2f}%"
    if kind == "ratio":
        return f"{v:.2f}"
    return f"{int(v):,}"


def format_table(c: RunComparison) -> str:
    """Aligned text. A `*` marks every cell of a row whose values are not all the same."""
    def mark(vals: list[str], differs: bool) -> list[str]:
        return [f"{v}*" if differs else v for v in vals]

    table: list[list[str]] = [["run"] + list(c.run_ids)]
    if c.names != c.run_ids:
        table.append(["name"] + list(c.names))
    for m in c.metrics:
        table.append([m.key] + mark([(why or "-") if v is None else _fmt(v, m.kind)
                                         for v, why in zip(m.values, m.reasons or [None] * len(m.values))],
                                        len(set(m.values)) > 1))
    for lab, present in c.labels.items():
        table.append([f"label {lab}"] + mark(["yes" if p else "no" for p in present], lab in c.differing_labels))
    for d in c.manifest_diff:
        vals = ["-" if v is None else (v if len(v) <= 24 else v[:12] + "..") for v in d.values]
        table.append([d.field] + mark(vals, d.differs))
    widths = [max(len(r[i]) for r in table) for i in range(len(table[0]))]
    lines = [(r[0].ljust(widths[0]) + "  " + "  ".join(x.rjust(w) for x, w in zip(r[1:], widths[1:]))).rstrip()
             for r in table]
    lines.append("* marks a row where the runs differ")
    lines += [f"note: {n}" for n in c.notes]
    return "\n".join(lines)
