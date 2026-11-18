"""FS 5: a stock's ratios on a day, from the figures known on that day (pit.py) and nothing later.

Every figure is the company's one basis on the day (pit.basis_on). Amounts are int paise; the ratios are floats
(dimensionless, or times).

- **P/E** = market cap ÷ TTM profit to owners (four consecutive filed quarters, pit.ttm). A TTM profit of zero or
  less gives no P/E and the flag "loss-making".
- **P/B** = market cap ÷ total equity of the balance-sheet row (below); None when equity ≤ 0, with the flag
  "negative equity" (then ROE and debt to equity are None too; the screen places all three worst, ruling R9 d).
- **Balance-sheet row** (ruling R6): the newest quarter row known on the day that carries total equity (a Sep or Mar
  quarter). Debt to equity reads its borrowings from that same row.
- **ROE** = TTM profit to owners ÷ the mean of the total equity of the last two balance-sheet rows (just the one when
  only one is known); None when that is ≤ 0. A loss gives a negative ROE.
- **Debt to equity** = (current + non-current borrowings) ÷ total equity of the row; None when the row files neither
  (a missing figure is not read as no debt) or equity ≤ 0.
- **Operating margin** = TTM (total income − other income − total expenses + finance costs + depreciation) ÷ TTM
  revenue from operations; None unless all six are on the four quarters and revenue > 0.
- **Profit growth** = the newest quarter's profit to owners ÷ the same quarter a year earlier − 1; None when that
  earlier profit is ≤ 0 (growth from a loss means nothing) or either quarter is missing.
- **Financials** (Industry "Financial Services"): **NII growth** = (interest earned − interest expended) of the newest
  quarter ÷ the same of the quarter a year earlier − 1 (None when the earlier NII is ≤ 0); debt to equity and
  operating margin are None. Profit growth is computed (shown, and read for the falling-profit warning) but the
  screen does not place financials on it.
- **Market cap** is pit.market_cap: None when the stock has no close in the 6 trading days to the day or its share
  count is held after a fall no split explains; then there is no P/E or P/B, and the flag "no market value".

`detail()` adds what the screen needs beyond the ratios: the flags, the newest quarter's period end (for the
staleness rule), and debt to equity a year before the balance-sheet row (for the rising-debt warning).
"""
from __future__ import annotations

from datetime import date

from stockify.fundamentals import pit, universe

FINANCIAL_SECTOR = "Financial Services"
KEYS = ("pe", "pb", "roe", "debt_equity", "op_margin", "profit_growth", "nii_growth")
OP_FIELDS = ("total_income", "other_income", "total_expenses", "finance_costs", "depreciation", "revenue_ops")


def _ratio(num: int | None, den: int | None) -> float | None:
    if num is None or den is None or den <= 0:
        return None
    return num / den


def _growth(now: int | None, before: int | None) -> float | None:
    if now is None or before is None or before <= 0:
        return None
    return now / before - 1


def _quarter_back(quarters: list[dict], end: str, k: int) -> dict | None:
    want = pit.shift_quarter(date.fromisoformat(end), -k).isoformat()
    return next((q for q in quarters if q["period_end"] == want), None)


def _debt(fields: dict) -> int | None:
    parts = [fields[k] for k in ("borrowings_current", "borrowings_noncurrent") if k in fields]
    return sum(parts) if parts else None


def _nii(q: dict | None) -> int | None:
    if q is None or "interest_earned" not in q["fields"] or "interest_expended" not in q["fields"]:
        return None
    return q["fields"]["interest_earned"] - q["fields"]["interest_expended"]


def _profit(q: dict | None) -> int | None:
    return None if q is None else q["fields"].get("profit_owners")


def detail(symbol: str, day: date, *, sector: str | None = None) -> dict:
    """{"ratios": {KEYS: float | None}, "flags": ["no market value", "loss-making", "negative equity"],
    "financial": bool,
    "newest_quarter": ISO period end | None, "debt_equity_year_ago": float | None}. `sector` defaults to
    universe.sector_of(symbol)."""
    if sector is None:
        sector = universe.sector_of(symbol)
    financial = sector == FINANCIAL_SECTOR
    out = {k: None for k in KEYS}
    flags: list[str] = []
    quarters = pit.known_on(symbol, day)["quarters"]
    newest = quarters[0] if quarters else None

    profit = pit.ttm(symbol, day, "profit_owners")
    mcap = pit.market_cap(symbol, day)
    if mcap is None:
        flags.append("no market value")
    if profit is not None and profit <= 0:
        flags.append("loss-making")
    else:
        out["pe"] = _ratio(mcap, profit)

    sheets = [q for q in quarters if "total_equity" in q["fields"]]
    sheet = sheets[0] if sheets else None
    equity = None if sheet is None else sheet["fields"]["total_equity"]
    out["pb"] = _ratio(mcap, equity)
    if equity is not None and equity <= 0:
        flags.append("negative equity")
    if sheets:
        last_two = [q["fields"]["total_equity"] for q in sheets[:2]]
        mean_equity = sum(last_two) / len(last_two)
        if profit is not None and mean_equity > 0 and equity > 0:
            out["roe"] = profit / mean_equity

    year_ago_de = None
    if newest is not None:
        out["profit_growth"] = _growth(_profit(newest), _profit(_quarter_back(quarters, newest["period_end"], 4)))
    if financial:
        if newest is not None:
            out["nii_growth"] = _growth(_nii(newest), _nii(_quarter_back(quarters, newest["period_end"], 4)))
    else:
        if sheet is not None:
            out["debt_equity"] = _ratio(_debt(sheet["fields"]), equity)
            before = _quarter_back(quarters, sheet["period_end"], 4)
            if before is not None and "total_equity" in before["fields"]:
                year_ago_de = _ratio(_debt(before["fields"]), before["fields"]["total_equity"])
        ttms = {f: pit.ttm(symbol, day, f) for f in OP_FIELDS}
        if all(v is not None for v in ttms.values()):
            op = (ttms["total_income"] - ttms["other_income"] - ttms["total_expenses"] + ttms["finance_costs"]
                  + ttms["depreciation"])
            out["op_margin"] = _ratio(op, ttms["revenue_ops"])

    return {"ratios": out, "flags": flags, "financial": financial,
            "newest_quarter": None if newest is None else newest["period_end"],
            "debt_equity_year_ago": year_ago_de}


def for_stock(symbol: str, day: date, *, sector: str | None = None) -> dict:
    """{pe, pb, roe, debt_equity, op_margin, profit_growth, nii_growth}: floats or None (see the module docstring)."""
    return detail(symbol, day, sector=sector)["ratios"]
