"""HD 4.1: nightly evidence. How often each voting strategy beat simply holding the stock, year by year.

For one holding, `run_for` backtests every voting strategy (`replay.VOTERS`, each with its defaults) and
buy-and-hold (`replay.BASELINE`) on all cached daily bars up to `evidence_end`, with `stockify.oracle.run`: the
Python engine directly, so no `runs` row and no research-registry entry (spec: "the grading backtests add no rows
to the research registry"), with the engine's realistic fills and Zerodha charges.

Scoring (spec, "Evidence = defaults across time"):

* Sharpe = mean / sample deviation of the daily returns of `equity.csv` nav_paise, times sqrt(252), no risk-free
  rate (the same for the strategy and for holding, so the comparison is fair). Undefined (None) with fewer than
  two returns or a flat NAV (no trades); an undefined Sharpe never wins.
* Windows are calendar years (IST trade dates). A year's returns are its own days', the first measured from the
  previous year's last NAV. A partial first or last year counts only with at least MIN_WINDOW_DAYS trading days.
  A window is won when the strategy's Sharpe in it is above buy-and-hold's.
* "Beat overall" = whole-span Sharpe above buy-and-hold's. `grade` turns the counts into strong / some / weak /
  none; `note` says it in one plain sentence.

The backtests end the day before research.json `holdout_from` (or yesterday, if earlier): the holdout stays locked.
Each strategy's run folder has a fixed id per holding (`desk-<instrument>-<module>`), so tonight's run replaces
last night's instead of piling up. `run_for` stores its rows in the desk store and returns them.
"""
from __future__ import annotations

import csv
import math
from datetime import date, timedelta

from stockify import timeutil
from stockify.desk import replay
from stockify.desk import store

VOTERS = replay.VOTERS
BASELINE = replay.BASELINE

CAPITAL_PAISE = 100_000_000                 # ₹10,00,000 (1,00,00,000 paise)
MIN_WINDOW_DAYS = 120                       # trading days a partial first or last year needs to count
MIN_TRADES = 8                              # grade "none" below this many trades...
MIN_YEARS = 3                               # ...or this many years of history
STRONG_PCT, SOME_PCT = 70, 55               # share of windows won, in percent
TRADING_DAYS = 252

# The interface digest's daily config: bar fills with 10 % participation and 5 bp slippage, Zerodha charges.
FILL_MODEL = {"kind": "bar", "limit_touch_policy": "through", "participation": "0.10",
              "slippage": {"kind": "bps", "x": "0.0005"}}
COSTS = {"broker": "zerodha", "allow_unverified": True, "auto_square_off_fee": True, "dp_charges": True}


# ── dates and grading ────────────────────────────────────────────────────────

def evidence_end(today: date) -> date:
    """The last day the evidence may use: yesterday, or the day before research.json `holdout_from` if earlier."""
    from stockify.research import holdout
    end = today - timedelta(days=1)
    hold = holdout.holdout_from()
    return end if hold is None else min(end, hold - timedelta(days=1))


def span_years(first: date, last: date) -> float:
    """Calendar years from the first to the last trading day, both counted."""
    return ((last - first).days + 1) / 365.25


def grade(windows_won: int, windows_total: int, beat_overall: bool, trades: int, years: float) -> str:
    """none: under MIN_TRADES trades, under MIN_YEARS years, or no window to compare; strong: at least 70 % of the
    windows won and beat overall; some: at least 55 % won; else weak."""
    if trades < MIN_TRADES or years < MIN_YEARS or windows_total <= 0:
        return "none"
    if windows_won * 100 >= STRONG_PCT * windows_total and beat_overall:
        return "strong"
    if windows_won * 100 >= SOME_PCT * windows_total:
        return "some"
    return "weak"


def note(windows_won: int, windows_total: int, beat_overall: bool, trades: int, years: float, grade_: str) -> str:
    """The plain sentence the page shows beside the grade."""
    if grade_ == "none":
        if trades < MIN_TRADES or years < MIN_YEARS:
            return (f"Too little to judge: {trades} trade{'' if trades == 1 else 's'} in {years:.1f} years "
                    f"(needs {MIN_TRADES} trades and {MIN_YEARS} years).")
        return "Too little to judge: no year had enough trading days to compare."
    unit = "year" if windows_total == 1 else "years"
    tail = "and over the whole span" if beat_overall else "but not over the whole span"
    return f"Beat holding the stock in {windows_won} of {windows_total} {unit}, {tail}."


# ── Sharpe and yearly windows ────────────────────────────────────────────────

def sharpe(navs: list) -> float | None:
    """Annualised Sharpe of the daily returns of `navs` (oldest first, the first value the base). None when undefined:
    fewer than two returns, a flat series, or a NAV at or below zero."""
    if len(navs) < 3 or any(v <= 0 for v in navs):
        return None
    r = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
    if min(r) == max(r):
        return None
    mean = sum(r) / len(r)
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1))
    if sd == 0 or not math.isfinite(sd):
        return None
    return mean / sd * math.sqrt(TRADING_DAYS)


def window_won(strategy_sharpe: float | None, buy_hold_sharpe: float | None) -> bool:
    return strategy_sharpe is not None and buy_hold_sharpe is not None and strategy_sharpe > buy_hold_sharpe


def _years(equity: list[tuple[str, int]], capital: int) -> dict[int, list]:
    """{year: [base NAV, that year's NAVs...]}: the base is the previous trading day's NAV (capital for the first)."""
    out: dict[int, list] = {}
    prev = capital
    for day, nav in equity:
        y = int(day[:4])
        if y not in out:
            out[y] = [prev]
        out[y].append(nav)
        prev = nav
    return out


def windows(strategy_equity: list[tuple[str, int]], buy_hold_equity: list[tuple[str, int]], *, capital: int,
            min_days: int = MIN_WINDOW_DAYS) -> list[dict]:
    """Calendar-year windows ({year, sharpe, buy_hold_sharpe, won}), oldest first, from (trade_date, nav_paise) rows.
    A year counts when the strategy has at least `min_days` trading days in it (a whole year always has)."""
    mine, theirs = _years(strategy_equity, capital), _years(buy_hold_equity, capital)
    out = []
    for y in sorted(mine):
        if len(mine[y]) - 1 < min_days:
            continue
        s = sharpe(mine[y])
        b = sharpe(theirs[y]) if y in theirs else None
        out.append({"year": y, "sharpe": s, "buy_hold_sharpe": b, "won": window_won(s, b)})
    return out


# ── the backtests ────────────────────────────────────────────────────────────

def _module(ref: str) -> str:
    return ref.partition(":")[0].rpartition(".")[2]


def run_id(instrument_id: int, ref: str) -> str:
    """The fixed run folder of one strategy on one holding: each night's run replaces the last."""
    return f"desk-{int(instrument_id)}-{_module(ref)}"


def config(ref: str, params: dict, symbol: str, first: date, last: date, *, name: str = "desk") -> dict:
    """The oracle run config of one desk backtest: `ref` with `params` on `symbol` ("NSE:X"), daily bars first..last."""
    return {
        "schema": "stockify.run/1", "name": name,
        "strategy": {"kind": "python", "ref": ref, "params": {"symbol": symbol, **params}},
        "data": {"packs": [{"spec": {"dataset": "bars", "interval": "day", "instruments": [symbol],
                                     "from": first.isoformat(), "to": last.isoformat()}}]},
        "price_basis": "adjusted", "capital_paise": CAPITAL_PAISE,
        "fill_model": {**FILL_MODEL, "slippage": dict(FILL_MODEL["slippage"])},
        "latency_ms": 0, "costs": dict(COSTS), "margin": {"model": "flat"},
        "policies": {"preopen": "queue", "cas": "none", "freeze": "slice", "max_slices": 10, "rms_square_off": False},
        "mark": "ltp", "seed": 42, "unlock_holdout": None,
    }


def _equity(folder) -> list[tuple[str, int]]:  # noqa: ANN001
    with open(folder / "equity.csv", encoding="utf-8", newline="") as f:
        return [(row["trade_date"], int(row["nav_paise"])) for row in csv.DictReader(f)]


def _num(x) -> float | None:  # noqa: ANN001
    """A metric as a finite float, or None."""
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _backtest(instrument_id: int, ref: str, params: dict, symbol: str, first: date, last: date):  # noqa: ANN202
    from stockify import oracle
    rid = run_id(instrument_id, ref)
    res = oracle.run(config(ref, params, symbol, first, last, name=rid), run_id=rid)
    return res.metrics, _equity(res.folder)


def run_for(instrument_id: int, symbol: str, today: date, *, voters=None) -> list[dict]:  # noqa: ANN001
    """Grade every voting strategy (default `VOTERS`) on one holding against buy-and-hold, over all cached daily bars
    up to `evidence_end(today)`. Stores the rows (one per strategy, trade_date = today) and returns them in voter
    order; [] when the bar cache has no daily bars for the stock up to then."""
    voters = VOTERS if voters is None else tuple(voters)
    sym = symbol if ":" in symbol else f"NSE:{symbol}"
    end = evidence_end(today)
    bars = replay.daily_bars(instrument_id, until_ns=timeutil.ist_ns(end, "00:00"))
    if not bars:
        return []
    first = timeutil.ns_to_ist(bars[0].start_ns).date()
    last = timeutil.ns_to_ist(bars[-1].start_ns).date()
    bh_metrics, bh_equity = _backtest(instrument_id, BASELINE[0], dict(BASELINE[1]), sym, first, last)
    bh_sharpe = sharpe([CAPITAL_PAISE] + [nav for _, nav in bh_equity])
    rows = []
    for ref, extra in voters:
        params = {"symbol": sym, **extra}
        m, eq = _backtest(instrument_id, ref, dict(extra), sym, first, last)
        if not eq:
            continue
        s = sharpe([CAPITAL_PAISE] + [nav for _, nav in eq])
        ws = windows(eq, bh_equity, capital=CAPITAL_PAISE)
        won, total, trades = sum(w["won"] for w in ws), len(ws), int(m.get("trades") or 0)
        beat = window_won(s, bh_sharpe)
        years = span_years(first, last)
        g = grade(won, total, beat, trades, years)
        rows.append({
            "trade_date": today.isoformat(), "instrument_id": int(instrument_id), "ref": ref, "params": params,
            "from_date": first.isoformat(), "to_date": last.isoformat(), "trades": trades,
            "cagr": _num(m.get("cagr")), "buy_hold_cagr": _num(bh_metrics.get("cagr")),
            "sharpe": s, "buy_hold_sharpe": bh_sharpe,
            "max_drawdown": _num(m.get("max_drawdown")), "win_rate": _num(m.get("win_rate")),
            "windows": ws, "windows_won": won, "windows_total": total, "grade": g,
            "note": note(won, total, beat, trades, years, g),
        })
    store.put_evidence(rows)
    return rows
