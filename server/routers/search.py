"""GET /api/search?q=… — ticker / company name resolver.

Mirrors the legacy Streamlit `yahoo_search_suggestions` ordering:
  1. Kite instruments cache (authoritative for current NSE/BSE symbols)
  2. yfinance fallback (only when Kite is unconfigured / not authenticated
     with an empty instruments cache — Kite returning [] for an authed user
     means "definitely not listed", so we don't double-search and surface
     delisted garbage)

Canonical ticker returned is the **bare NSE symbol** (e.g. "RELIANCE"). The
yfinance/Kite-specific suffix is applied later, at each API-call boundary,
via `india_utils.to_yfinance_ticker` / `to_kite_symbol`.
"""
from __future__ import annotations

import importlib.util
import logging
from typing import Iterable

from fastapi import APIRouter, Query

import kite_client
from india_utils import strip_exchange_suffix
from server.schemas import SearchHit

# yfinance is imported inside `_yfinance_hits`, the only place that uses it: it loads a large tree of
# packages, and the API should not run that code at start-up for a fallback that Kite usually makes
# unnecessary. The flag only says whether the package is installed; finding it does not import it.
try:
    _YFINANCE_AVAILABLE = importlib.util.find_spec("yfinance") is not None
except (ImportError, ValueError):  # pragma: no cover — yfinance is in requirements.txt
    _YFINANCE_AVAILABLE = False

log = logging.getLogger("stockify.search")

router = APIRouter(prefix="/api/search", tags=["search"])

# yfinance returns these exchange codes for NSE/BSE listings.
_YF_NSE_EXCHANGES: set[str] = {"NSI"}
_YF_BSE_EXCHANGES: set[str] = {"BSE", "BOM"}
_YF_PRIMARY: set[str] = _YF_NSE_EXCHANGES | _YF_BSE_EXCHANGES


def _kite_rows_to_hits(rows: Iterable[dict]) -> list[SearchHit]:
    """Map kite_client instrument rows to SearchHit DTOs (bare ticker)."""
    hits: list[SearchHit] = []
    for r in rows:
        symbol = (r.get("tradingsymbol") or "").upper()
        if not symbol:
            continue
        exch_raw = (r.get("exchange") or "").upper()
        exch: str = "NSE" if exch_raw == "NSE" else "BSE"
        try:
            tok = int(r.get("instrument_token") or 0) or None
        except (TypeError, ValueError):
            tok = None
        hits.append(
            SearchHit(
                ticker=symbol,
                name=r.get("name") or symbol,
                exchange=exch,  # type: ignore[arg-type]
                sector=None,
                instrument_type=r.get("instrument_type") or "EQ",
                source="kite",
                instrument_token=tok,
            )
        )
    return hits


def _yfinance_hits(query: str, limit: int) -> list[SearchHit]:
    """Fallback search via yfinance — only NSE/BSE equities surface."""
    if not _YFINANCE_AVAILABLE:
        return []
    try:
        import yfinance as yf
    except ImportError:
        return []
    try:
        quotes = yf.Search(query, max_results=limit).quotes  # type: ignore[attr-defined]
    except Exception as e:
        log.warning("yfinance search failed for %r: %s", query, e)
        return []
    hits: list[SearchHit] = []
    for q in quotes:
        if q.get("quoteType") != "EQUITY":
            continue
        yf_exch = q.get("exchange", "")
        if yf_exch not in _YF_PRIMARY:
            continue
        exch: str = "NSE" if yf_exch in _YF_NSE_EXCHANGES else "BSE"
        raw_symbol = q.get("symbol", "") or ""
        bare = strip_exchange_suffix(raw_symbol)
        if not bare:
            continue
        hits.append(
            SearchHit(
                ticker=bare,
                name=q.get("longname") or q.get("shortname") or bare,
                exchange=exch,  # type: ignore[arg-type]
                sector=None,
                instrument_type="EQ",
                source="yfinance",
            )
        )
    return hits


@router.get("", response_model=list[SearchHit])
async def search(
    q: str = Query("", description="Free-text query — symbol or company name"),
    limit: int = Query(12, ge=1, le=50),
) -> list[SearchHit]:
    """Resolve a query against the NSE/BSE universe.

    Returns up to `limit` matches. Empty list when the query is too short
    (<2 chars) or no source recognizes it.
    """
    query = (q or "").strip()
    if len(query) < 2:
        return []

    # 1. Kite-first — authoritative when authed and cache is populated.
    if kite_client.is_enabled():
        try:
            kite_rows = kite_client.resolve_symbol(query, limit=limit)
        except Exception as e:
            log.warning("kite_client.resolve_symbol failed for %r: %s", query, e)
            kite_rows = []
        if kite_rows:
            return _kite_rows_to_hits(kite_rows)
        # If Kite is authed AND the instruments cache is populated but
        # returned nothing, trust Kite — don't fall through to yfinance.
        try:
            if (kite_client.is_authenticated()
                    and kite_client.instruments_row_count() > 0):
                return []
        except Exception:
            pass

    # 2. yfinance fallback (Kite disabled / unauthed / empty cache).
    return _yfinance_hits(query, limit)
