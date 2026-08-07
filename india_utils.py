"""
India market helpers for STOCKIFY.

- Currency formatting (Indian: ₹, lakhs, crores, Indian-comma grouping).
- yfinance ticker suffix handling (.NS for NSE, .BO for BSE) with auto-fallback.
"""

from __future__ import annotations

import re
from typing import Optional

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False


RUPEE = "₹"

CRORE = 10_000_000
LAKH = 100_000


def strip_exchange_suffix(ticker: str) -> str:
    """RELIANCE.NS / RELIANCE.BO / RELIANCE.BSE → RELIANCE."""
    return re.sub(r"\.(NS|BO|BSE|NSE)$", "", ticker, flags=re.IGNORECASE).upper()


def to_yfinance_ticker(symbol: str, exchange: str = "NSE") -> str:
    """Apply the appropriate yfinance suffix to a bare NSE/BSE symbol.

    If the symbol already carries a suffix, it is returned unchanged. Otherwise
    .NS (NSE) or .BO (BSE) is appended.
    """
    if not symbol:
        return symbol
    s = symbol.strip()
    if "." in s and re.search(r"\.(NS|BO|BSE|NSE)$", s, flags=re.IGNORECASE):
        return s.upper().replace(".BSE", ".BO").replace(".NSE", ".NS")
    return s.upper() + (".BO" if exchange.upper() == "BSE" else ".NS")


def to_kite_symbol(symbol: str, exchange: str = "NSE") -> str:
    """Format a bare NSE/BSE symbol for kite.ltp() / kite.quote() — "NSE:RELIANCE"."""
    if not symbol:
        return ""
    bare = strip_exchange_suffix(symbol)
    ex = exchange.upper()
    if ex not in ("NSE", "BSE"):
        ex = "NSE"
    return f"{ex}:{bare}"


def fetch_history_with_fallback(symbol: str, period: str = "3mo", interval: str = "1d"):
    """yfinance history() with automatic .NS → .BO fallback for BSE-only listings.

    Returns (DataFrame, resolved_ticker). DataFrame may be empty if neither works.
    """
    if not YFINANCE_AVAILABLE:
        raise ImportError("yfinance not installed")
    base = strip_exchange_suffix(symbol)
    for suffix in (".NS", ".BO"):
        candidate = base + suffix
        try:
            df = yf.Ticker(candidate).history(period=period, interval=interval)
        except Exception:
            df = None
        if df is not None and not df.empty:
            return df, candidate
    return None, base


def format_inr_compact(value, decimals: int = 2) -> str:
    """Format a numeric amount with ₹ + crore/lakh suffix.

    Examples
    --------
    1_234_567_890   → "₹123.46 Cr"
    98_765_432      → "₹9.88 Cr"
    250_000         → "₹2.50 L"
    4_500           → "₹4,500"
    None / NaN      → "—"
    """
    if value is None:
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if n != n:  # NaN
        return "—"
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= CRORE:
        return f"{sign}{RUPEE}{n / CRORE:,.{decimals}f} Cr"
    if n >= LAKH:
        return f"{sign}{RUPEE}{n / LAKH:,.{decimals}f} L"
    return f"{sign}{RUPEE}{format_inr_full(n, decimals=decimals if n < 1 else 0)}"


def format_inr_full(value, decimals: int = 0) -> str:
    """Indian-comma grouping (1,23,45,678) without the ₹ symbol.

    Useful inside tables where the column header already says "₹" / "Amount".
    """
    if value is None:
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if n != n:
        return "—"
    sign = "-" if n < 0 else ""
    n = abs(n)
    int_part = int(n)
    frac = n - int_part
    s = str(int_part)
    if len(s) <= 3:
        grouped = s
    else:
        head, tail = s[:-3], s[-3:]
        # group head by 2s from the right
        head_groups = []
        while len(head) > 2:
            head_groups.append(head[-2:])
            head = head[:-2]
        if head:
            head_groups.append(head)
        grouped = ",".join(reversed(head_groups)) + "," + tail
    if decimals > 0:
        return f"{sign}{grouped}.{int(round(frac * (10 ** decimals))):0{decimals}d}"
    return f"{sign}{grouped}"


def format_price(value, decimals: int = 2) -> str:
    """Inline price string with the ₹ symbol — for quotes, OHLC, levels."""
    if value is None:
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if n != n:
        return "—"
    return f"{RUPEE}{n:,.{decimals}f}"
