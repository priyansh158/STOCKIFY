"""`report.md` and `equity.png` for a run folder (M2.6). Human summary; not hashed."""
from __future__ import annotations

import csv
import json
from pathlib import Path

LABEL_TEXT = {
    "eod_approx": "fills at the next day's close from end-of-day files (approximate, A4 §4.9)",
    "fill:approximate": "snapshot LTP fills (approximate, A4 §4.2)",
    "charges:unverified_rows": "some charge rows are not yet verified against Zerodha (Z-3)",
    "costs:scaled": "every brokerage, tax and fee was multiplied by costs.scale for a what-if test (FW 6.1)",
    "settlement_fallback": "an expiry settled at the last underlying price: no FSP arrived (A4 §7.2)",
    "specs:equity_tick_assumed": "equity tick assumed 1 paisa before its first recorded spec (D-43)",
}


def _rupees(paise) -> str:  # noqa: ANN001
    if paise is None:
        return "–"
    sign = "-" if paise < 0 else ""
    p = abs(int(paise))
    whole, frac = divmod(p, 100)
    s = f"{whole:,}"
    # Indian grouping: last three digits, then pairs
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{sign}₹{s}.{frac:02d}"


def _pct(x) -> str:  # noqa: ANN001
    return "–" if x is None else f"{x * 100:.2f}%"


def _num(x, digits: int = 2) -> str:  # noqa: ANN001
    return "–" if x is None else f"{x:.{digits}f}"


def equity_png(folder: Path) -> Path | None:
    rows = list(csv.DictReader((folder / "equity.csv").open(encoding="utf-8")))
    if not rows:
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from datetime import date
    xs = [date.fromisoformat(r["trade_date"]) for r in rows]
    ys = [int(r["nav_paise"]) / 100 for r in rows]
    fig, ax = plt.subplots(figsize=(8, 3.2), dpi=110)
    ax.plot(xs, ys, linewidth=1.2, color="#2f6fdf")
    ax.set_ylabel("NAV (₹)")
    ax.grid(alpha=0.3)
    ax.spines[["top", "right"]].set_visible(False)
    fig.autofmt_xdate()
    fig.tight_layout()
    out = folder / "equity.png"
    fig.savefig(out)
    plt.close(fig)
    return out


def _gap_lines(folder: Path) -> list[str]:
    """The days on which a pack had instruments with no events, and the frames the recorder dropped on a full queue,
    as listed in the bundle (nothing is filled in)."""
    path = folder / "bundle.json"
    if not path.exists():
        return []
    days: dict[str, set[int]] = {}
    spans: list[dict] = []
    for p in json.loads(path.read_text(encoding="utf-8")).get("packs", []):
        for day, ids in p.get("gaps", {}).get("days", {}).items():
            days.setdefault(day, set()).update(ids)
        spans += p.get("recorder_gaps", {}).get("spans", [])
    out: list[str] = []
    if days:
        out += (["## Data gaps", "",
                 f"`data:gaps={len(days)}`: on these days some instruments had no data in the packs. Nothing was filled in.", ""]
                + [f"- {day}: instrument ids {', '.join(str(i) for i in sorted(ids))}" for day, ids in sorted(days.items())]
                + [""])
    if spans:
        known = all(s.get("frames") is not None for s in spans)
        total = str(sum(s["frames"] for s in spans)) if known else "unknown"
        out += ["## Dropped frames", "",
                f"`data:dropped_frames={total}`: the recorder's queue was full and refused these frames, so the packs "
                "lack them for every instrument. Nothing was filled in.", ""]
        for s in sorted(spans, key=lambda s: (s["day"], s.get("start_ns") or 0)):
            first, last = s.get("first_frame_no"), s.get("last_frame_no")
            frames = "frame numbers unknown" if first is None else f"frames {first}-{last}"
            out.append(f"- {s['day']}: {frames} ({s.get('frames') if s.get('frames') is not None else 'unknown'} dropped)")
        out.append("")
    return out


def write(folder: Path) -> Path:
    m = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    run = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    png = equity_png(folder)
    labels = manifest.get("labels", [])
    lines = [f"# {run['name']}", "",
             f"`{run['strategy'].get('ref', run['strategy']['kind'])}` · {m['days']} trading days · "
             f"engine {manifest.get('engine')} · run `{manifest['run_id']}`", ""]
    if labels:
        lines += ["**Labels**", ""] + [f"- `{lab}`: {LABEL_TEXT.get(lab, '')}".rstrip(": ") for lab in labels] + [""]
    gaps = _gap_lines(folder)
    if gaps:
        lines += gaps
    lines += ["## Headline", "", "| metric | value |", "|---|---|",
              f"| Start → end NAV | {_rupees(m['nav_start_paise'])} → {_rupees(m['nav_end_paise'])} |",
              f"| CAGR | {_pct(m['cagr'])} |", f"| Volatility | {_pct(m['volatility'])} |",
              f"| Sharpe / Sortino | {_num(m['sharpe'])} / {_num(m['sortino'])} |",
              f"| Max drawdown (days) | {_pct(m['max_drawdown'])} ({m['max_drawdown_days']}) |",
              f"| Calmar | {_num(m['calmar'])} |",
              f"| Trades / win rate / profit factor | {m['trades']} / {_pct(m['win_rate'])} / {_num(m['profit_factor'])} |",
              f"| Average trade | {_rupees(round(m['avg_trade_paise'])) if m['avg_trade_paise'] is not None else '–'} |",
              f"| Exposure / turnover | {_pct(m['exposure'])} / {_num(m['turnover'])} |",
              f"| Max participation used | {_pct(m['max_participation'])} |", ""]
    if png:
        lines += ["![equity](equity.png)", ""]
    lines += ["## Costs", "", "| component | amount |", "|---|---|"]
    for k, v in m["costs_paise"].items():
        if v:
            lines.append(f"| {k} | {_rupees(v)} |")
    lines += [f"| **total** | **{_rupees(m['costs_total_paise'])}** |",
              f"| share of gross P&L ({_rupees(m['gross_pnl_paise'])}) | {_pct(m['cost_share_of_gross'])} |", ""]
    if m["rejects"]:
        lines += ["## Rejected orders", ""] + [f"- `{k}`: {v}" for k, v in m["rejects"].items()] + [""]
    lines += ["## Run", "",
              f"- Fill model: `{run['fill_model']['kind']}`; price basis `{run['price_basis']}`; seed {run['seed']}",
              f"- Bundle `{manifest['bundle_sha256'][:16]}…`; packs " + ", ".join(f"`{p[:12]}…`" for p in manifest["packs"]),
              f"- Python {manifest.get('python')} on {manifest.get('os')}; {manifest.get('duration_ms')} ms",
              "- Deterministic artifacts and their SHA-256: `determinism.json`", ""]
    out = folder / "report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
