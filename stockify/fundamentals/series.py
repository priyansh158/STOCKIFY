"""FS 9: a stock's figures quarter by quarter, for the details page's charts, each against its peers' median.

Every point is what the market knew on `day` (pit.known_on: the latest 8 usable quarters, the company's one basis).
Per quarter end:

- **Revenue**, **net profit** (profit to owners, else net profit) and, for a financial, **net interest income**
  (interest earned − interest expended): the quarter's own figures, int paise.
- **Operating margin** and **return on equity**: as ratios.py computes them, with the four quarters ending at that
  quarter (TTM: four consecutive filed quarters, none derived) and the balance-sheet rows at or before it. So only the
  newest five of eight quarters can have them.
- **Debt to equity**: on the balance-sheet quarters (Sep, Mar) only, as ratios.py. None for a financial, as there.
- **P/E** and **P/B**: pit.market_cap on the quarter's usable day (what the ratio was when those results came out)
  ÷ the TTM profit to owners ending at that quarter, or the total equity of the newest balance-sheet row at or before
  it. A loss or negative equity gives no point, as in ratios.py.

`peer_series` puts the peers' median beside each of the stock's points: the median over the peers (the stock among
them) that have that figure at the same quarter end; money medians in whole paise (the middle two's mean rounded
down). Points run oldest first; a figure with no point at all is left out.
"""
from __future__ import annotations

import statistics
from datetime import date

from stockify.fundamentals import pit, ratios

# (key, label, unit, for financials, for others)
FIGURES = (("revenue", "Revenue", "paise", False, True),
           ("net_profit", "Net profit", "paise", True, True),
           ("nii", "Net interest income", "paise", True, False),
           ("op_margin", "Operating margin", "fraction", False, True),
           ("roe", "Return on equity", "fraction", True, True),
           ("debt_equity", "Debt to equity", "times", False, True),
           ("pe", "P/E", "times", True, True),
           ("pb", "P/B", "times", True, True))


def _ttm(by_end: dict[str, dict], end: str, field: str) -> int | None:
    total = 0
    for k in range(4):
        q = by_end.get(pit.shift_quarter(date.fromisoformat(end), -k).isoformat())
        if q is None or q["derived_from"] is not None or field not in q["fields"]:
            return None
        total += q["fields"][field]
    return total


def quarterly(symbol: str, day: date, *, financial: bool) -> dict[str, dict[str, int | float]]:
    """{figure key: {period_end: value}} for the quarters known on `day`; a figure a quarter cannot give is absent."""
    quarters = pit.known_on(symbol, day)["quarters"]
    by_end = {q["period_end"]: q for q in quarters}
    out: dict[str, dict[str, int | float]] = {k: {} for k, *_ in FIGURES}

    def put(key: str, end: str, value: int | float | None) -> None:
        if value is not None:
            out[key][end] = value

    for q in quarters:
        end, f = q["period_end"], q["fields"]
        put("net_profit", end, f.get("profit_owners", f.get("net_profit")))
        profit = _ttm(by_end, end, "profit_owners")
        sheets = [s for s in quarters if s["period_end"] <= end and "total_equity" in s["fields"]]
        equity = sheets[0]["fields"]["total_equity"] if sheets else None
        if sheets and profit is not None:
            mean_equity = sum(s["fields"]["total_equity"] for s in sheets[:2]) / len(sheets[:2])
            put("roe", end, profit / mean_equity if mean_equity > 0 else None)
        if financial:
            put("nii", end, ratios._nii(q))
        else:
            put("revenue", end, f.get("revenue_ops"))
            if "total_equity" in f:
                put("debt_equity", end, ratios._ratio(ratios._debt(f), f["total_equity"]))
            ttms = {k: _ttm(by_end, end, k) for k in ratios.OP_FIELDS}
            if all(v is not None for v in ttms.values()):
                op = (ttms["total_income"] - ttms["other_income"] - ttms["total_expenses"] + ttms["finance_costs"]
                      + ttms["depreciation"])
                put("op_margin", end, ratios._ratio(op, ttms["revenue_ops"]))
        if (profit is not None and profit > 0) or (equity is not None and equity > 0):
            mcap = pit.market_cap(symbol, date.fromisoformat(q["usable_day"]))
            put("pe", end, ratios._ratio(mcap, profit))
            put("pb", end, ratios._ratio(mcap, equity))
    return out


def _median(values: list, unit: str):  # noqa: ANN202
    if not values:
        return None
    if unit != "paise":
        return statistics.median(values)
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) // 2


def peer_series(symbol: str, day: date, *, financial: bool, peers: list[tuple[str, bool]],
                cache: dict | None = None) -> list[dict]:
    """[{key, label, unit, points: [{period_end, value, sector_median}]}] for the figures `symbol` has, oldest point
    first. `peers`: (symbol, financial) of the peer group, the stock among them; [] gives no medians. `cache` holds
    each stock's `quarterly` for the request."""
    cache = {} if cache is None else cache

    def figures(sym: str, fin: bool) -> dict:
        if sym not in cache:
            cache[sym] = quarterly(sym, day, financial=fin)
        return cache[sym]

    own = figures(symbol, financial)
    others = [figures(s, fin) for s, fin in peers]
    out = []
    for key, label, unit, for_fin, for_other in FIGURES:
        if not (for_fin if financial else for_other) or not own[key]:
            continue
        points = [{"period_end": end, "value": own[key][end],
                   "sector_median": _median([p[key][end] for p in others if end in p[key]], unit)}
                  for end in sorted(own[key])]
        out.append({"key": key, "label": label, "unit": unit, "points": points})
    return out
