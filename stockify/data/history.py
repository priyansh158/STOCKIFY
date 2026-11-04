"""Kite historical candles (M1.4 builds the cached, chunked fetcher on top).

Moved here from the retired `technical_analyst.py` in M0.1. These helpers
are private building blocks: resolve a bare NSE/BSE symbol to an instrument
token and pull one window of candles through `kite_client.historical`.

Rows are returned as Kite gives them (`date` is a datetime, prices are the
exchange's rupee floats). Conversion to integer paise and UTC nanoseconds
(invariants I-3, I-4) belongs to the bar cache in M1.4, not here.
"""
from __future__ import annotations

from datetime import datetime

try:
    import kite_client
    KITE_AVAILABLE = True
except ImportError:  # pragma: no cover - kite_client is part of the repo
    kite_client = None  # type: ignore
    KITE_AVAILABLE = False

from india_utils import strip_exchange_suffix


# Repo interval string -> Kite Connect interval string
_INTERVAL_TO_KITE = {
    "1d":  "day",
    "1wk": "week",
    "1mo": "month",
    "1h":  "60minute",
    "30m": "30minute",
    "15m": "15minute",
    "5m":  "5minute",
    "3m":  "3minute",
    "1m":  "minute",
}


def _resolve_token(symbol: str) -> int | None:
    """Instrument token for a bare symbol, trying NSE then BSE."""
    bare = strip_exchange_suffix(symbol)
    for exch in ("NSE", "BSE"):
        token = kite_client.get_instrument_token(bare, exchange=exch)
        if token:
            return token
    return None


def _kite_rows(rows: list[dict]) -> list[dict]:
    """Normalise Kite historical_data rows; drops rows without a date or
    with non-numeric fields."""
    out: list[dict] = []
    for r in rows:
        dt = r.get("date")
        if not dt:
            continue
        try:
            out.append({
                "date": dt,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r.get("volume") or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _fetch_candles_kite(symbol: str, from_dt: datetime, to_dt: datetime,
                        interval: str) -> list[dict]:
    """One window of candles for `symbol`; `[]` when Kite is unavailable,
    the interval is unknown, or the symbol does not resolve."""
    if not KITE_AVAILABLE or not kite_client.is_authenticated():
        return []
    kite_interval = _INTERVAL_TO_KITE.get(interval)
    if not kite_interval:
        return []
    token = _resolve_token(symbol)
    if not token:
        return []
    return _kite_rows(kite_client.historical(token, from_dt, to_dt, kite_interval))
