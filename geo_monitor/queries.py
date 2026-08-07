"""Macro / geo query catalogue + holdings query builder for the news monitor."""
from __future__ import annotations

from typing import Iterable

# Curated India-equity macro / geopolitical monitoring queries.
# Keep specific enough to reduce noise; operators are Google News compatible.
MACRO_QUERIES: list[str] = [
    'RBI OR "repo rate" OR MPC',
    'Fed OR FOMC OR "interest rate" site:reuters.com OR site:bloomberg.com',
    '"USD INR" OR rupee OR "dollar rupee"',
    '"crude oil" OR Brent OR "oil prices"',
    'FII OR DII OR "foreign institutional"',
    "SEBI OR GST OR budget India",
    "monsoon OR rural demand India",
    'tariff OR sanction OR "China+1" OR "Red Sea"',
    'election India OR "political instability" India',
    '"Middle East" OR Israel OR Iran oil',
]


def holding_queries(
    holdings: Iterable[dict],
    *,
    max_holdings: int = 40,
) -> list[str]:
    """Build per-holding Google News queries from portfolio rows.

    Each item may have tradingsymbol and optional name.
    """
    out: list[str] = []
    for h in list(holdings)[:max_holdings]:
        sym = (h.get("tradingsymbol") or h.get("ticker") or "").strip().upper()
        name = (h.get("name") or h.get("company_name") or "").strip()
        if not sym and not name:
            continue
        if name and sym:
            # Exact-ish company name OR ticker
            out.append(f'"{name}" OR {sym}')
        elif name:
            out.append(f'"{name}"')
        else:
            out.append(sym)
    return out


def build_monitor_queries(
    holdings: Iterable[dict] | None = None,
    *,
    include_macro: bool = True,
) -> list[str]:
    qs: list[str] = []
    if include_macro:
        qs.extend(MACRO_QUERIES)
    if holdings is not None:
        qs.extend(holding_queries(holdings))
    # Dedupe preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for q in qs:
        key = q.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(q.strip())
    return unique
