"""Shared set-up for the ratio and screen tests: companies with five quarters of filings, a price and a universe row."""
from __future__ import annotations

import hashlib
from datetime import date, timedelta

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify import timeutil
from stockify.data import calendar as nse_calendar
from stockify.data import refdata
from stockify.fundamentals import filings, pit, store

DAY = date(2025, 12, 1)                                # a Monday, before the holdout (2026-08-01)
CR = 10_000_000 * 100                                  # ₹1 crore in paise
SHARES = 1_000_000_000                                 # 100 crore shares: ₹1 of price is ₹100 crore of value
FV = 1000                                              # face value ₹10 in paise


class ScreenCase(SandboxedDBTest):
    db_modules = (store, refdata)

    def setUp(self):
        super().setUp()
        self.n = 0
        self.universe: list[dict] = []

    def put(self, symbol: str, period_end: str, fields: dict, *, basis: str = "consolidated") -> str:
        self.n += 1
        url = f"https://nsearchives.nseindia.in/corporate/xbrl/INDAS_{self.n}.xml"
        end = date.fromisoformat(period_end)
        b = timeutil.ist_ns(end + timedelta(days=40), "20:00")
        fid = filings.filing_id(symbol, period_end, "Q", basis, b, url)
        row = {"filing_id": fid, "symbol": symbol, "period_start": None, "period_end": period_end,
               "period_kind": "Q", "basis": basis, "broadcast_ns": b, "broadcast_source": "broadCastDate",
               "source": "results", "seq_number": str(self.n), "xbrl_url": url,
               "xbrl_sha256": hashlib.sha256(url.encode()).hexdigest(), "xbrl_file": None, "status": "parsed",
               "reason": None}
        conn = store._connect()
        conn.execute(f"INSERT INTO filings ({', '.join(filings.COLUMNS)}) "
                     f"VALUES ({', '.join('?' * len(filings.COLUMNS))})", tuple(row[c] for c in filings.COLUMNS))
        conn.executemany("INSERT INTO figures (filing_id, field, value_paise, value_raw) VALUES (?, ?, ?, ?)",
                         [(fid, k, v, f"test {v}") for k, v in fields.items()])
        return fid

    def company(self, symbol: str, industry: str, *, price: int | None = 50000, profit: int = 250,
                prior: int = 200, equity: int = 6000, equity_prev: int = 4000, debt: int | None = 1500,
                debt_prev: int | None = None, revenue: int = 1000, op: int = 300, newest: str = "2025-09-30",
                financial: bool = False, nii: int = 300, nii_prior: int = 250, listed: bool = True) -> None:
        """Five quarters to `newest` (amounts in ₹ crore): profit each quarter, `prior` in the quarter a year before
        the newest; the balance sheet on the Sep / Mar rows (equity on the newest, equity_prev on the two before);
        debt split 40 / 60 into current and non-current. A financial files interest earned and expended instead of
        revenue and borrowings. `price` (paise) is the close on DAY and on the newest period end; None: no close."""
        ends = [pit.shift_quarter(date.fromisoformat(newest), -k).isoformat() for k in range(4, -1, -1)]
        iid = len(self.universe) + 1
        for k, end in enumerate(ends):
            p = prior if k == 0 else profit
            f = {"profit_owners": p * CR, "net_profit": p * CR, "paid_up_equity": SHARES * FV, "face_value": FV}
            if financial:
                f.update({"interest_earned": ((nii_prior if k == 0 else nii) + 700) * CR,
                          "interest_expended": 700 * CR, "other_income": 50 * CR})
            else:
                f.update({"revenue_ops": revenue * CR, "other_income": 100 * CR, "total_income": (revenue + 100) * CR,
                          "total_expenses": (revenue - op) * CR, "finance_costs": 0, "depreciation": 0})
            if end[5:7] in ("03", "09"):
                eq = equity if k == 4 else equity_prev
                f["total_equity"] = eq * CR
                d = debt if k == 4 else (debt if debt_prev is None else debt_prev)
                if d is not None and not financial:
                    f["borrowings_current"] = d * 40 // 100 * CR
                    f["borrowings_noncurrent"] = (d - d * 40 // 100) * CR
            self.put(symbol, end, f)
        if listed:
            refdata._connect().execute(
                "INSERT INTO instruments (instrument_id, exchange, kind, tradingsymbol, first_seen, last_seen, "
                "source) VALUES (?, 'NSE', 'EQ', ?, '2021-01-01', '2026-07-31', 'kite_dump')", (iid, symbol))
            if price is not None:
                anchor = nse_calendar.previous_trading_day("CM", date.fromisoformat(newest) + timedelta(days=1))
                for d in sorted({anchor, DAY}):
                    refdata._connect().execute(
                        "INSERT INTO cm_eod (trade_date, instrument_id, series, close_paise, prev_close_paise, "
                        "volume, turnover_paise) VALUES (?, ?, 'EQ', ?, 0, 1, 1)", (d.isoformat(), iid, price))
        self.universe.append({"symbol": symbol, "instrument_id": iid if listed else None, "isin": f"INE{iid:09d}",
                              "company": f"{symbol} Ltd.", "median_value_paise": 9 * CR, "liquid": 1,
                              "list_industry": industry})
        store.put_universe("2026-07-31", self.universe)
