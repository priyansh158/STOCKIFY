"""Run metrics (A4 §8), in exactly the stated expression order.

No `sum()`, `math.fsum` or numpy reductions: Python's `sum()` of floats is
compensated since 3.12, which the C++ engine would not reproduce. Every
reduction is a plain left-to-right loop. Undefined values are None (`null`).
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import NamedTuple


def rate_on(rows: list[dict], d: str) -> float:
    best = None
    for r in rows:
        if r["effective_from"] <= d and (best is None or r["effective_from"] > best["effective_from"]):
            best = r
    return (best["rate_ppb"] / 1_000_000_000) if best else 0.0


_rate_on = rate_on


def max_drawdown(navs: list[float]) -> tuple[float, int]:
    """(max drawdown, longest drawdown duration in days); the peak starts at navs[0]."""
    peak = navs[0]
    max_dd = 0.0
    dur = longest = 0
    for d in range(1, len(navs)):
        if navs[d] >= peak:
            peak = navs[d]
            dur = 0
        else:
            dur += 1
            longest = max(longest, dur)
            dd = 1 - navs[d] / peak
            max_dd = max(max_dd, dd)
    return max_dd, longest


def drawdown_series(navs: list[float]) -> list[float]:
    """1 - NAV_d / peak for each day d = 1..T (the peak starts at navs[0] and is only raised by a new high)."""
    peak = navs[0]
    out = []
    for d in range(1, len(navs)):
        if navs[d] >= peak:
            peak = navs[d]
        out.append(1 - navs[d] / peak)
    return out


class TradeChunk(NamedTuple):
    """One closed slice of a FIFO lot: `qty` units opened at `open_price` and closed at `close_price`.

    `close_fill_id` / `close_order_id` are None when an expiry settlement closed it (D-45)."""
    close_t_ns: int
    close_fill_id: int | None
    close_order_id: int | None
    instrument_id: int
    product: str
    open_side: str
    qty: int
    open_price: int
    close_price: int
    pnl: int


def fifo_trades(fills, closes, products: Mapping[int, str]) -> tuple[list[TradeChunk], int]:
    """FIFO matching of closing fills (and expiry settlements, D-45) against opening fills per (instrument, product).

    `fills`: objects with fill_id, order_id, t_ns, instrument_id, side, qty, price. `closes`: expiry settlements
    (t, iid, product, side, units, price) or None. `products`: order id -> product. Returns the closed chunks in
    flow order and the traded value (fills only, paise). Flow order: (t_ns, is_settle, n)."""
    lots: dict = {}
    chunks: list[TradeChunk] = []
    traded_value = 0
    flow = [(f.t_ns, 0, n, f.instrument_id, products[f.order_id], f.side, f.qty, f.price, f.fill_id, f.order_id)
            for n, f in enumerate(fills)]
    flow += [(t, 1, n, iid, prod, side, units, price, None, None)
             for n, (t, iid, prod, side, units, price) in enumerate(closes or [])]
    flow.sort(key=lambda e: (e[0], e[1], e[2]))
    for t, is_settle, _, iid, product, side, qty, price, fill_id, order_id in flow:
        if not is_settle:
            traded_value += qty * price
        key = (iid, product)
        q = lots.setdefault(key, [])
        sign = 1 if side == "BUY" else -1
        left = qty
        while left and q and q[0][0] != sign:
            open_sign, open_qty, open_price = q[0]
            k = min(left, open_qty)
            chunks.append(TradeChunk(t, fill_id, order_id, iid, product, "BUY" if open_sign == 1 else "SELL",
                                     k, open_price, price, open_sign * k * (price - open_price)))
            left -= k
            if k == open_qty:
                q.pop(0)
            else:
                q[0] = (open_sign, open_qty - k, open_price)
        if left:
            q.append((sign, left, price))
    return chunks, traded_value


def compute(*, capital: int, equity: list[tuple], fills: list, orders: dict, charges: list[dict],
            risk_free: list[dict], rejects: dict, participation_ppm: int, exposure: float | None,
            closes: list | None = None) -> dict:
    """`closes`: expiry settlements (t, iid, product, side, units, price); they close trades too (D-45)."""
    navs = [float(capital)] + [float(row[1]) for row in equity]
    dates = [row[0] for row in equity]
    T = len(equity)
    r = []
    x = []
    for d in range(1, T + 1):
        rd = navs[d] / navs[d - 1] - 1
        r.append(rd)
        x.append(rd - rate_on(risk_free, dates[d - 1]) / 252)

    sharpe = sortino = vol = None
    if T >= 2:
        s = 0.0
        for v in x:
            s += v
        mean = s / T
        ss = 0.0
        for v in x:
            ss += (v - mean) ** 2
        sd = math.sqrt(ss / (T - 1))
        if min(x) == max(x):                  # all equal: the deviation is exactly 0, not rounding noise
            sd = 0.0
        if sd != 0:
            sharpe = mean / sd * math.sqrt(252)
        dn = 0.0
        for v in x:
            m = min(v, 0.0)
            dn += m ** 2
        den = math.sqrt(dn / T)
        if den != 0:
            sortino = mean / den * math.sqrt(252)
        sr = 0.0
        for v in r:
            sr += v
        mr = sr / T
        ssr = 0.0
        for v in r:
            ssr += (v - mr) ** 2
        vol = 0.0 if min(r) == max(r) else math.sqrt(ssr / (T - 1)) * math.sqrt(252)
    cagr = (navs[T] / navs[0]) ** (252 / T) - 1 if T >= 1 and navs[0] > 0 and navs[T] > 0 else None

    max_dd, longest = max_drawdown(navs)
    calmar = cagr / max_dd if cagr is not None and max_dd != 0 else None

    chunks, traded_value = fifo_trades(fills, closes, {oid: o.product for oid, o in orders.items()})
    trades = [c.pnl for c in chunks]
    wins = losses = 0
    gross_win = gross_loss = 0
    total_pnl = 0
    for t in trades:
        total_pnl += t
        if t > 0:
            wins += 1
            gross_win += t
        elif t < 0:
            losses += 1
            gross_loss += t
    n_trades = len(trades)

    by_comp = {k: 0 for k in ("brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp", "other")}
    total_costs = 0
    for c in charges:
        for k in by_comp:
            by_comp[k] += c[k]
        total_costs += c["total"]
    gross = (equity[-1][1] - capital + total_costs) if equity else 0
    mean_nav = None
    if T:
        sn = 0.0
        for v in navs[1:]:
            sn += v
        mean_nav = sn / T
    turnover = (float(traded_value) / mean_nav) * 252 / T if T and mean_nav else None

    return {
        "days": T,
        "nav_start_paise": capital,
        "nav_end_paise": equity[-1][1] if equity else capital,
        "cagr": cagr, "volatility": vol, "sharpe": sharpe, "sortino": sortino,
        "max_drawdown": max_dd if T else None, "max_drawdown_days": longest if T else None, "calmar": calmar,
        "trades": n_trades,
        "win_rate": (wins / n_trades) if n_trades else None,
        "profit_factor": (gross_win / -gross_loss) if gross_loss else None,
        "avg_trade_paise": (total_pnl / n_trades) if n_trades else None,
        "exposure": exposure,
        "turnover": turnover,
        "costs_paise": by_comp, "costs_total_paise": total_costs,
        "gross_pnl_paise": gross,
        "cost_share_of_gross": (total_costs / gross) if gross else None,
        "rejects": dict(sorted(rejects.items())),
        "max_participation": participation_ppm / 1_000_000,
    }
