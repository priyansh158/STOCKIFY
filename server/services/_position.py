"""Thin helper around `portfolio_sync.anonymous_position_blurb` — kept
isolated so the analysis_pipeline doesn't import portfolio_sync directly,
which would require kite_client at import time. (Lazy resolution.)
"""
from __future__ import annotations

import portfolio_sync


def anonymous_position_block_for(ticker: str) -> str:
    """Return the anonymised position blurb for `ticker`, or "" if no holding.

    Per CLAUDE.md: this block must NEVER use 'you' / 'I' / 'your' / 'user' —
    `portfolio_sync.anonymous_position_blurb` enforces that.
    """
    bare = ticker.strip().upper()
    if not bare:
        return ""
    try:
        h = portfolio_sync.holding_for(bare)
        if not h:
            return ""
        return portfolio_sync.anonymous_position_blurb(h)
    except Exception:
        return ""
