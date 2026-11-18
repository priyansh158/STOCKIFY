"""FS 5: the screen. Each liquid stock against its industry; the order, best first, and a few plain words why.

- **Who is placed.** Every stock of the newest liquid universe (universe.current()), checked in this order:
  no quarter known at all ("no results on record", or "its results could not be read (held back: <reason>)" when a
  held filing was known by the day); newest usable quarter ended more than STALE_DAYS (200) days
  before the day ("results are out of date"); no market value on the day, from no close, a held share count or no
  shares filed (ruling R9 b: "no market price to judge it by on this day"); fewer than four of its six figures (P/E,
  P/B, ROE, debt to equity, operating margin, profit growth; ratios.py) ("too few figures"). A financial (Industry
  "Financial Services") is judged on P/E, P/B, ROE and NII growth and needs three of those four. A loss-maker's P/E
  and a negative-equity P/B count as figures (their worst place, below).
- **Peers** (ruling R3): the stock's Industry from the Nifty 500 list, when that industry has at least MIN_PEERS (5)
  placed stocks; otherwise every placed stock ("All stocks (Textiles has too few)"). `peer_group` says which.
- **Order** (ruling R9). Each figure is placed among the peers from 0 (worst) to 1 (best); a cheaper P/E or P/B and
  lower debt are better. A loss-maker (TTM owners' profit ≤ 0) takes P/E place 0; negative equity (R9 d, a red flag)
  takes place 0 on P/B, debt to equity and ROE.
  The peers' places count them below every real P/E or P/B. Any other missing figure counts 0.5, the peers' middle,
  never above it. Valuation is the mean over P/E and P/B and quality the mean over the rest. composite =
  balance × valuation + (1 − balance) × quality. Rows run by composite, highest first, ties by symbol. The composite
  and the places stay in this module: the public rows carry the order alone (owner: no score shown).
- **Reasons.** `short_reason` is at most 90 characters and has no numbers. It opens with any warning ("Loss-making;
  ...", "Profits falling and debt rising; ...", ruling I2). It then names the stock's two or three strongest filed
  points against its peers in relative words ("cheaper than its industry on earnings, with less debt and wider
  margins"). An absolute word is used only where it is true outright: "debt-free" / "no debt" (D/E 0), "rising profits"
  (growth > 0). A stock warned of rising debt is never also said to have less debt. `reasons` holds the warnings
  (loss-making, negative equity, a cheap P/E with falling profits or rising debt), then each filed figure against its
  peers' median, strongest first.

`save(day, rows)` keeps a day's list in `screen_runs`; `latest()` reads the newest.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import date

from stockify import dbconn
from stockify.fundamentals import pit, ratios, series, store, universe

log = logging.getLogger(__name__)

STALE_DAYS = 200                     # task-4 review: a newest quarter older than this is out of date
MIN_PEERS = 5                        # ruling R3: fewer placed stocks in an industry: compare with all stocks
STRONG = 0.6                         # a figure at least this far up its peers is a point worth naming
DEBT_RISE = 1.1                      # debt to equity up by more than 10 % in a year is "rising debt" ...
DEBT_FLOOR = 0.1                     # ... when it is at least 0.1 (a rise from almost nothing is not a warning)
SHORT_MAX = 90
MISSING = 0.5                        # ruling R9 c: a missing figure counts as the peers' middle
NO_PRICE = "no market price to judge it by on this day"
MINUS = "−"                     # the true minus of the page's figures (web/src/lib/fundamentals.ts)

USED = {False: ("pe", "pb", "roe", "debt_equity", "op_margin", "profit_growth"),
        True: ("pe", "pb", "roe", "nii_growth")}
NEEDED = {False: 4, True: 3}
VALUATION = ("pe", "pb")
LOWER_BETTER = {"pe", "pb", "debt_equity"}
# Ruling R9 a, d: these figures take the worst place, not none, when the stock carries the flag.
WORST_FLAG = {"pe": "loss-making", "pb": "negative equity", "debt_equity": "negative equity",
              "roe": "negative equity"}


# ── placing ──────────────────────────────────────────────────────────────────

def _place(v: float, values: list[float]) -> float:
    """Where v sits among values (which include it): 0 lowest, 1 highest, ties at their middle; 0.5 alone."""
    if len(values) < 2:
        return 0.5
    less = sum(1 for x in values if x < v)
    equal = sum(1 for x in values if x == v)
    return (less + (equal - 1) / 2) / (len(values) - 1)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else MISSING


def _has(d: dict, k: str) -> bool:
    """The figure counts: filed, or a loss / negative equity that takes the worst place."""
    return d["ratios"][k] is not None or (k in WORST_FLAG and WORST_FLAG[k] in d["flags"])


def _held_reason(symbol: str, day: date) -> str | None:
    """The reason of the newest held filing of `symbol` known on `day` (usable by then; one NSE lists no broadcast
    time for counts once its period has ended); None when it has none."""
    rows = store._connect().execute(
        "SELECT period_end, broadcast_ns, reason FROM filings WHERE symbol = ? AND status = 'held' "
        "ORDER BY period_end DESC, broadcast_ns DESC", (symbol,)).fetchall()
    for period_end, broadcast_ns, reason in rows:
        known = (pit.usable_day(broadcast_ns) <= day if broadcast_ns is not None
                 else date.fromisoformat(period_end) < day)
        if known:
            return reason or "no reason recorded"
    return None


def left_out_reason(d: dict, day: date, symbol: str | None = None) -> str | None:
    """Why the screen leaves a stock out on `day`, from its ratios.detail on that day; None when it is placed. With
    no quarter known, a held filing known by `day` is named ("its results could not be read (held back: ...)")."""
    if d["newest_quarter"] is None:
        held = _held_reason(symbol, day) if symbol is not None else None
        return "no results on record" if held is None else f"its results could not be read (held back: {held})"
    if (day - date.fromisoformat(d["newest_quarter"])).days > STALE_DAYS:
        return "results are out of date"
    if "no market value" in d["flags"]:
        return NO_PRICE
    if sum(1 for k in USED[d["financial"]] if _has(d, k)) < NEEDED[d["financial"]]:
        return "too few figures"
    return None


def _candidates(day: date) -> tuple[list[dict], list[dict]]:
    placed, left_out = [], []
    for u in universe.current():
        sym, sector = u["symbol"], (u["list_industry"] or None)
        d = ratios.detail(sym, day, sector=sector)
        reason = left_out_reason(d, day, sym)
        if reason is None:
            placed.append({"symbol": sym, "sector": sector, "detail": d, "used": USED[d["financial"]]})
            continue
        left_out.append({"symbol": sym, "reason": reason})
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


def _strengths(p: dict) -> tuple[dict[str, float], dict[str, float], dict[str, float | None]]:
    """(filed: {figure: 0 worst .. 1 best among the peers} for the figures it filed, the ones words may name;
    counted: every figure it is judged on, a loss / negative equity at 0 and a missing one at MISSING;
    medians: {figure: the peers' median of filed values, or None})."""
    filed, counted, medians = {}, {}, {}
    d = p["detail"]
    for k in ratios.KEYS:
        values = [q["detail"]["ratios"][k] for q in p["peers"] if q["detail"]["ratios"][k] is not None]
        medians[k] = statistics.median(values) if values else None
        if k not in p["used"]:
            continue
        if k in WORST_FLAG:                  # the loss-makers / negative equities among the peers sit below all
            worst = -math.inf if k not in LOWER_BETTER else math.inf
            values = [q["detail"]["ratios"][k] for q in p["peers"] if q["detail"]["ratios"][k] is not None
                      and WORST_FLAG[k] not in q["detail"]["flags"]]
            values += [worst] * sum(1 for q in p["peers"] if WORST_FLAG[k] in q["detail"]["flags"])
        v = d["ratios"][k]
        if k in WORST_FLAG and WORST_FLAG[k] in d["flags"]:
            counted[k] = 0.0
        elif v is not None:
            s = _place(v, values)
            filed[k] = counted[k] = 1 - s if k in LOWER_BETTER else s
        else:
            counted[k] = MISSING
    return filed, counted, medians


# ── words ────────────────────────────────────────────────────────────────────

def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _point(k: str, v: float) -> tuple[str, bool] | None:
    """(plain words for a strong figure, whether they compare with the peers), or None when the figure cannot be put
    as a strength. Comparing words read with "than its industry"; the others are true outright."""
    if k == "pe":
        return "earnings", True
    if k == "pb":
        return "book value", True
    if k == "roe":
        return ("a higher return on equity", True) if v > 0 else None
    if k == "debt_equity":
        return ("debt-free", False) if v == 0 else ("less debt", True)
    if k == "op_margin":
        return ("wider margins", True) if v > 0 else None
    if k == "profit_growth":
        return ("rising profits", False) if v > 0 else None
    if k == "nii_growth":
        return ("rising interest income", False) if v > 0 else None
    return None


def _body(picks: list[str], r: dict, whom: str) -> str:
    """The points as one phrase: a head, then ", with" and noun phrases ("debt-free" heads, "no debt" follows)."""
    val = [_point(k, r[k])[0] for k in picks if k in VALUATION]
    comps = [_point(k, r[k])[0] for k in picks if k not in VALUATION and _point(k, r[k])[1]]
    plain = [_point(k, r[k])[0] for k in picks if k not in VALUATION and not _point(k, r[k])[1]]
    noun = lambda w: "no debt" if w == "debt-free" else w  # noqa: E731
    if val:
        head, rest = f"cheaper than {whom} on {_join(val)}", comps + plain      # "with less debt": than {whom}
    elif comps:
        head, rest = f"{_join(comps)} than {whom}", plain
    elif plain:
        head, rest = plain[0], plain[1:]
    else:
        return f"no figure stands out against {whom}"
    return head + (f", with {_join([noun(w) for w in rest])}" if rest else "")


def _short(p: dict, filed: dict[str, float], warns: list[str], traps: list[str]) -> str:
    r = p["detail"]["ratios"]
    whom = "most stocks" if p["whole"] else "its industry"
    order = sorted(filed, key=lambda k: (-filed[k], ratios.KEYS.index(k)))
    strong = [k for k in order if filed[k] >= STRONG and _point(k, r[k]) is not None
              and not (k == "debt_equity" and "debt" in traps)]                   # M4: never "less debt, but rising"
    val_s = [k for k in strong if k in VALUATION]
    qual_s = [k for k in strong if k not in VALUATION]
    # The best valuation point and the two best quality points; the rest of either fills up to three.
    picks = val_s[:1] + qual_s[:2]
    picks += (val_s[1:] + qual_s[2:])[:3 - len(picks)]
    picks.sort(key=lambda k: (-filed[k], ratios.KEYS.index(k)))
    lead = {"profit": "profits falling", "nii": "interest income falling", "debt": "debt rising"}
    heads = warns + [lead[t] for t in traps]
    while True:
        text = (_join(heads) + "; " if heads else "") + _body(picks, r, whom)
        if len(text) <= SHORT_MAX or (not picks and len(heads) <= 1):
            return (text[0].upper() + text[1:])[:SHORT_MAX]
        if picks:
            picks = picks[:-1]                   # the weakest point named goes first; warnings stay
        else:
            heads = heads[:-1]


def _change(x: float) -> str:
    if x == -1:
        return "fell to zero"
    if x < -1:
        return "swung to a loss"                 # M3: the earlier figure was positive, the newest is below zero
    return f"{'up' if x >= 0 else 'down'} {abs(x) * 100:.1f} %"


def _num(x: float, digits: int) -> str:
    """A figure as the page prints it: a true minus (U+2212), never a hyphen."""
    return f"{x:.{digits}f}".replace("-", MINUS)


def _figure_line(k: str, v: float, median: float | None, whole: bool) -> str:
    whom = "all stocks" if whole else "its industry"
    pct = lambda x: f"{_num(x * 100, 1)} %"  # noqa: E731 — the page's figure style, "12.3 %"
    times = lambda x: _num(x, 1)  # noqa: E731
    if k == "pe":
        text, fmt = f"Priced at {times(v)} times its last four quarters' profit", times
    elif k == "pb":
        text, fmt = f"Priced at {times(v)} times its book value", times
    elif k == "roe":
        text, fmt = f"Earns {pct(v)} a year on shareholders' equity", pct
    elif k == "debt_equity":
        text, fmt = f"Debt is {_num(v, 2)} times its equity", lambda x: _num(x, 2)
    elif k == "op_margin":
        text, fmt = f"Operating margin of {pct(v)}", pct
    elif k == "profit_growth":
        text, fmt = f"Profit {_change(v)} on the same quarter a year earlier", _change
    else:
        text, fmt = f"Net interest income {_change(v)} on the same quarter a year earlier", _change
    if median is None:
        return text + "."
    if k in ("profit_growth", "nii_growth") and median <= -1:
        return text + f" (half or more of {whom} fell to zero or a loss)."
    return text + f" ({whom}: {fmt(median)})."


def _row(p: dict, filed: dict[str, float], medians: dict[str, float | None]) -> dict:
    d = p["detail"]
    r = d["ratios"]
    warnings, warns, traps = [], [], []
    if "loss-making" in d["flags"]:
        warns.append("loss-making")
        warnings.append("Loss-making over its last four quarters, so it has no P/E.")
    if "negative equity" in d["flags"]:
        warns.append("negative equity")
        warnings.append("Its liabilities exceed its assets (negative equity), so P/B, debt to equity and return "
                        "on equity count against it.")
    if filed.get("pe", 0) >= STRONG:
        for k, name, what in (("profit_growth", "profit", "profit"), ("nii_growth", "nii", "net interest income")):
            g = r[k]
            if g is not None and g < 0:
                traps.append(name)
                warnings.append(f"Cheap on earnings, but {what} {_change(g).replace('down', 'fell')} on the same "
                                "quarter a year earlier: the low price may be deserved.")
        before = d["debt_equity_year_ago"]
        de = r["debt_equity"]
        if de is not None and before is not None and de >= DEBT_FLOOR and de > before * DEBT_RISE:
            traps.append("debt")
            warnings.append(f"Cheap on earnings, but its debt has risen from {_num(before, 2)} to {_num(de, 2)} times its "
                            "equity in a year.")
    order = sorted(filed, key=lambda k: (-filed[k], ratios.KEYS.index(k)))
    lines = [_figure_line(k, r[k], medians[k], p["whole"]) for k in order]
    return {"symbol": p["symbol"], "sector": p["sector"], "peer_group": p["peer_group"], "ratios": dict(r),
            "sector_medians": medians, "reasons": warnings + lines,
            "short_reason": _short(p, filed, warns, traps)}


# ── the screen ───────────────────────────────────────────────────────────────

def run(day: date, *, balance: float = 0.5) -> dict:
    """{"day": ISO, "rows": the public rows best first (as `order`), "left_out": [{"symbol", "reason"}] by symbol}."""
    if not 0 <= balance <= 1:
        raise ValueError(f"balance is {balance}, it must be from 0 (quality only) to 1 (valuation only)")
    placed, left_out = _candidates(day)
    _peers(placed)
    ranked = []
    for p in placed:
        filed, counted, medians = _strengths(p)
        val = _mean([counted[k] for k in VALUATION if k in counted])
        qual = _mean([counted[k] for k in counted if k not in VALUATION])
        ranked.append((-(balance * val + (1 - balance) * qual), p["symbol"], _row(p, filed, medians)))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return {"day": day.isoformat(), "rows": [t[2] for t in ranked], "left_out": left_out}


def order(day: date, *, balance: float = 0.5) -> list[dict]:
    """The placed stocks best first: [{symbol, sector, peer_group, ratios, sector_medians, reasons, short_reason}].
    No score, place or rank is in a row: the list's order is the judgement."""
    return run(day, balance=balance)["rows"]


# ── kept lists ───────────────────────────────────────────────────────────────

def save(day: date, rows: list[dict]) -> None:
    """Keep the day's list (replacing one saved for that day before), position 1 first, with the universe it was
    computed from. Each listed stock's details series are computed and kept first (series.precompute). If that
    fails the order is still kept (it is what the page lists): the day's series are dropped and the details page
    shows each stock's own figures without medians until the next save."""
    day = day if isinstance(day, date) else date.fromisoformat(str(day))
    iso = day.isoformat()
    try:
        series.precompute(day, rows, min_peers=MIN_PEERS)
        kept = True
    except Exception as e:  # noqa: BLE001 — the order must not be lost to the details' charts
        log.warning("fundamentals: the details series for %s were not kept (%s: %s); the list is saved without "
                    "them", iso, type(e).__name__, e)
        kept = False
    values = [(iso, n, r["symbol"], json.dumps(r, sort_keys=True, ensure_ascii=False))
              for n, r in enumerate(rows, start=1)]
    as_of = store.latest_as_of()
    conn = store._connect()
    with dbconn.locked_write(store._lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM screen_runs WHERE day = ?", (iso,))
        if not kept:
            conn.execute("DELETE FROM screen_series WHERE day = ?", (iso,))
        conn.executemany("INSERT INTO screen_runs (day, position, symbol, json) VALUES (?, ?, ?, ?)", values)
        conn.execute("INSERT OR REPLACE INTO screen_days (day, universe_as_of) VALUES (?, ?)", (iso, as_of))
        conn.execute("COMMIT")


def universe_of(day: str) -> str | None:
    """The universe.as_of the list of `day` (ISO) was computed from; None when not kept."""
    r = store._connect().execute("SELECT universe_as_of FROM screen_days WHERE day = ?", (str(day),)).fetchone()
    return None if r is None else r["universe_as_of"]


def latest() -> tuple[str | None, list[dict]]:
    """(day, rows in order) of the newest saved list, or (None, [])."""
    conn = store._connect()
    day = conn.execute("SELECT MAX(day) AS d FROM screen_runs").fetchone()["d"]
    if day is None:
        return None, []
    return day, [json.loads(r["json"]) for r in conn.execute(
        "SELECT json FROM screen_runs WHERE day = ? ORDER BY position", (day,))]
