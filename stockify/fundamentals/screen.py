"""FS 5: the screen. Each liquid stock against its industry; the order, best first, and a few plain words why.

- **Who is placed.** Every stock of the newest liquid universe (universe.current()) whose newest usable quarter ended
  within STALE_DAYS (200) days of the day (else left out: "results are out of date") and that has at least four of
  its six figures (P/E, P/B, ROE, debt to equity, operating margin, profit growth; ratios.py). A financial (Industry
  "Financial Services") is judged on P/E, P/B, ROE and NII growth and needs three of those four. Left out otherwise:
  "too few figures" ("no results on record" when no quarter is known at all).
- **Peers** (ruling R3): the stock's Industry from the Nifty 500 list, when that industry has at least MIN_PEERS (5)
  placed stocks; otherwise every placed stock ("All stocks (Textiles has too few)"). `peer_group` says which.
- **Order.** Each figure is placed among the peers that have it (0 worst to 1 best; cheaper P/E and P/B and lower
  debt are better). Valuation is the mean over P/E and P/B, quality over the rest; a side with no figure counts the
  middle (0.5), so a missing figure neither helps nor sinks a stock. composite = balance × valuation + (1 − balance)
  × quality; rows run by composite, highest first, ties by symbol. The composite and the per-figure places stay in
  this module: the public rows carry the order alone (owner: no score shown).
- **Reasons.** `short_reason` (at most 90 characters, no numbers) names the stock's two or three strongest points
  against its peers ("Cheaper than its industry on earnings, with rising profits and little debt"), and a value-trap
  warning when one applies. `reasons` holds the warnings (loss-making, no market value, a cheap P/E with falling
  profits or rising debt) and then each figure against its peers' median, strongest first.

`save(day, rows)` keeps a day's list in `screen_runs`; `latest()` reads the newest.
"""
from __future__ import annotations

import json
import statistics
from datetime import date

from stockify import dbconn
from stockify.fundamentals import ratios, store, universe

STALE_DAYS = 200                     # task-4 review: a newest quarter older than this is out of date
MIN_PEERS = 5                        # ruling R3: fewer placed stocks in an industry: compare with all stocks
STRONG = 0.6                         # a figure at least this far up its peers is a point worth naming
DEBT_RISE = 1.1                      # debt to equity up by more than 10 % in a year is "rising debt" ...
DEBT_FLOOR = 0.1                     # ... when it is at least 0.1 (a rise from almost nothing is not a warning)
SHORT_MAX = 90

USED = {False: ("pe", "pb", "roe", "debt_equity", "op_margin", "profit_growth"),
        True: ("pe", "pb", "roe", "nii_growth")}
NEEDED = {False: 4, True: 3}
VALUATION = ("pe", "pb")
LOWER_BETTER = {"pe", "pb", "debt_equity"}


# ── placing ──────────────────────────────────────────────────────────────────

def _place(v: float, values: list[float]) -> float:
    """Where v sits among values (which include it): 0 lowest, 1 highest, ties at their middle; 0.5 alone."""
    if len(values) < 2:
        return 0.5
    less = sum(1 for x in values if x < v)
    equal = sum(1 for x in values if x == v)
    return (less + (equal - 1) / 2) / (len(values) - 1)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.5


def _candidates(day: date) -> tuple[list[dict], list[dict]]:
    placed, left_out = [], []
    for u in universe.current():
        sym, sector = u["symbol"], (u["list_industry"] or None)
        d = ratios.detail(sym, day, sector=sector)
        if d["newest_quarter"] is None:
            left_out.append({"symbol": sym, "reason": "no results on record"})
            continue
        if (day - date.fromisoformat(d["newest_quarter"])).days > STALE_DAYS:
            left_out.append({"symbol": sym, "reason": "results are out of date"})
            continue
        used = USED[d["financial"]]
        if sum(1 for k in used if d["ratios"][k] is not None) < NEEDED[d["financial"]]:
            left_out.append({"symbol": sym, "reason": "too few figures"})
            continue
        placed.append({"symbol": sym, "sector": sector, "detail": d, "used": used})
    return placed, sorted(left_out, key=lambda r: r["symbol"])


def _peers(placed: list[dict]) -> None:
    by_sector: dict[str, list[dict]] = {}
    for p in placed:
        if p["sector"] is not None:
            by_sector.setdefault(p["sector"], []).append(p)
    for p in placed:
        group = by_sector.get(p["sector"], []) if p["sector"] is not None else []
        if len(group) >= MIN_PEERS:
            p["peers"], p["peer_group"], p["whole"] = group, f"Industry: {p['sector']}", False
        else:
            label = f"{p['sector']} has too few" if p["sector"] is not None else "no industry listed"
            p["peers"], p["peer_group"], p["whole"] = placed, f"All stocks ({label})", True


def _strengths(p: dict) -> tuple[dict[str, float], dict[str, float | None]]:
    """({figure: 0 worst .. 1 best among the peers}, {figure: the peers' median or None})."""
    strength, medians = {}, {}
    for k in ratios.KEYS:
        values = [q["detail"]["ratios"][k] for q in p["peers"] if q["detail"]["ratios"][k] is not None]
        medians[k] = statistics.median(values) if values else None
        v = p["detail"]["ratios"][k]
        if k in p["used"] and v is not None:
            s = _place(v, values)
            strength[k] = 1 - s if k in LOWER_BETTER else s
    return strength, medians


# ── words ────────────────────────────────────────────────────────────────────

def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _point(k: str, v: float) -> str | None:
    """The plain words for a strong figure, or None when the figure cannot be put as a strength."""
    if k == "pe":
        return "earnings"
    if k == "pb":
        return "book value"
    if k == "roe":
        return "high returns on equity" if v > 0 else None
    if k == "debt_equity":
        return "no debt" if v == 0 else "little debt"
    if k == "op_margin":
        return "wide margins" if v > 0 else None
    if k == "profit_growth":
        return "rising profits" if v > 0 else None
    if k == "nii_growth":
        return "rising interest income" if v > 0 else None
    return None


def _short(p: dict, strength: dict[str, float], traps: list[str]) -> str:
    r = p["detail"]["ratios"]
    whom = "most stocks" if p["whole"] else "its industry"
    order = sorted(strength, key=lambda k: (-strength[k], ratios.KEYS.index(k)))
    strong = [k for k in order if strength[k] >= STRONG and _point(k, r[k]) is not None]
    val_s = [k for k in strong if k in VALUATION]
    qual_s = [k for k in strong if k not in VALUATION]
    # The best valuation point and the two best quality points; the rest of either fills up to three.
    picks = val_s[:1] + qual_s[:2]
    picks += (val_s[1:] + qual_s[2:])[:3 - len(picks)]
    picks.sort(key=lambda k: (-strength[k], ratios.KEYS.index(k)))
    tail = {"profit": "profits are falling", "nii": "interest income is falling", "debt": "debt is rising"}
    while True:
        val = [_point(k, r[k]) for k in picks if k in VALUATION]
        qual = [_point(k, r[k]) for k in picks if k not in VALUATION]
        if val:
            text = f"Cheaper than {whom} on {_join(val)}" + (f", with {_join(qual)}" if qual else "")
        elif qual:
            text = _join(qual)
            text = text[0].upper() + text[1:]
        else:
            text = f"No figure stands out against {whom}"
        if traps:
            text += ", but " + " and ".join(tail[t] for t in traps)
        if len(text) <= SHORT_MAX:
            return text
        if len(picks) > 1:
            picks = picks[:-1]                   # the weakest point named goes first
        elif len(traps) > 1:
            traps = traps[:1]
        else:
            return text[:SHORT_MAX]              # not reached: one point and one warning fit


def _figure_line(k: str, v: float, median: float | None, whole: bool) -> str:
    whom = "all stocks" if whole else "its industry"
    pct = lambda x: f"{x * 100:.1f}%"  # noqa: E731
    times = lambda x: f"{x:.1f}"  # noqa: E731
    if k == "pe":
        text, fmt = f"Priced at {times(v)} times its last four quarters' profit", times
    elif k == "pb":
        text, fmt = f"Priced at {times(v)} times its book value", times
    elif k == "roe":
        text, fmt = f"Earns {pct(v)} a year on shareholders' equity", pct
    elif k == "debt_equity":
        text, fmt = f"Debt is {v:.2f} times its equity", lambda x: f"{x:.2f}"
    elif k == "op_margin":
        text, fmt = f"Operating margin of {pct(v)}", pct
    elif k == "profit_growth":
        text, fmt = (f"Profit {'up' if v >= 0 else 'down'} {pct(abs(v))} on the same quarter a year earlier",
                     lambda x: f"{'up' if x >= 0 else 'down'} {pct(abs(x))}")
    else:
        text, fmt = (f"Net interest income {'up' if v >= 0 else 'down'} {pct(abs(v))} on the same quarter a year "
                     f"earlier", lambda x: f"{'up' if x >= 0 else 'down'} {pct(abs(x))}")
    return text + (f" ({whom}: {fmt(median)})." if median is not None else ".")


def _row(p: dict, strength: dict[str, float], medians: dict[str, float | None]) -> dict:
    d = p["detail"]
    r = d["ratios"]
    warnings, traps = [], []
    if "loss-making" in d["flags"]:
        warnings.append("Loss-making over its last four quarters, so it has no P/E.")
    if "no market value" in d["flags"]:
        warnings.append("No market value on the day (no recent close, or its share count is being checked), "
                        "so P/E and P/B are left out.")
    if strength.get("pe", 0) >= STRONG:
        if r["profit_growth"] is not None and r["profit_growth"] < 0:
            traps.append("profit")
            warnings.append(f"Cheap on earnings, but profit fell {abs(r['profit_growth']) * 100:.1f}% on the same "
                            "quarter a year earlier: the low price may be deserved.")
        if r["nii_growth"] is not None and r["nii_growth"] < 0:
            traps.append("nii")
            warnings.append(f"Cheap on earnings, but net interest income fell {abs(r['nii_growth']) * 100:.1f}% on "
                            "the same quarter a year earlier: the low price may be deserved.")
        before = d["debt_equity_year_ago"]
        de = r["debt_equity"]
        if de is not None and before is not None and de >= DEBT_FLOOR and de > before * DEBT_RISE:
            traps.append("debt")
            warnings.append(f"Cheap on earnings, but its debt has risen from {before:.2f} to {de:.2f} times its "
                            "equity in a year.")
    order = sorted(strength, key=lambda k: (-strength[k], ratios.KEYS.index(k)))
    lines = [_figure_line(k, r[k], medians[k], p["whole"]) for k in order]
    return {"symbol": p["symbol"], "sector": p["sector"], "peer_group": p["peer_group"], "ratios": dict(r),
            "sector_medians": medians, "reasons": warnings + lines,
            "short_reason": _short(p, strength, traps)}


# ── the screen ───────────────────────────────────────────────────────────────

def run(day: date, *, balance: float = 0.5) -> dict:
    """{"day": ISO, "rows": the public rows best first (as `order`), "left_out": [{"symbol", "reason"}] by symbol}."""
    if not 0 <= balance <= 1:
        raise ValueError(f"balance is {balance}, it must be from 0 (quality only) to 1 (valuation only)")
    placed, left_out = _candidates(day)
    _peers(placed)
    ranked = []
    for p in placed:
        strength, medians = _strengths(p)
        val = _mean([strength[k] for k in VALUATION if k in strength])
        qual = _mean([strength[k] for k in strength if k not in VALUATION])
        ranked.append((-(balance * val + (1 - balance) * qual), p["symbol"], _row(p, strength, medians)))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return {"day": day.isoformat(), "rows": [t[2] for t in ranked], "left_out": left_out}


def order(day: date, *, balance: float = 0.5) -> list[dict]:
    """The placed stocks best first: [{symbol, sector, peer_group, ratios, sector_medians, reasons, short_reason}].
    No score, place or rank is in a row: the list's order is the judgement."""
    return run(day, balance=balance)["rows"]


# ── kept lists ───────────────────────────────────────────────────────────────

def save(day: date, rows: list[dict]) -> None:
    """Keep the day's list (replacing one saved for that day before), position 1 first."""
    iso = day.isoformat() if isinstance(day, date) else str(day)
    values = [(iso, n, r["symbol"], json.dumps(r, sort_keys=True, ensure_ascii=False))
              for n, r in enumerate(rows, start=1)]
    conn = store._connect()
    with dbconn.locked_write(store._lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM screen_runs WHERE day = ?", (iso,))
        conn.executemany("INSERT INTO screen_runs (day, position, symbol, json) VALUES (?, ?, ?, ?)", values)
        conn.execute("COMMIT")


def latest() -> tuple[str | None, list[dict]]:
    """(day, rows in order) of the newest saved list, or (None, [])."""
    conn = store._connect()
    day = conn.execute("SELECT MAX(day) AS d FROM screen_runs").fetchone()["d"]
    if day is None:
        return None, []
    return day, [json.loads(r["json"]) for r in conn.execute(
        "SELECT json FROM screen_runs WHERE day = ? ORDER BY position", (day,))]
