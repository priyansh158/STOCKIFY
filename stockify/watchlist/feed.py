"""CW 3: the watchlist's stocks join the recorder's feed.

`merged(universe, day)` returns a copy of the recorder's universe (universe.json) with `watchlist_quote`: the watched
stocks as "NSE:SYMBOL", in list order, at most `max_stocks` (watchlist.json). `SubscriptionManager` subscribes them in
QUOTE mode with the reason `watch:SYMBOL`; a stock the universe already holds keeps its own tier and reason.

It is called once, in `run_market_day`'s live branch, before the subscription plan is built: a stock added during the
session is recorded (and alerted on) from the next session. A replay never calls it, so a replayed day subscribes
exactly what its recording did.

It fails soft: the recording must never depend on the watchlist. Any error (a corrupt watchlist.sqlite, a bad
watchlist.json) is logged and the universe comes back unchanged.
"""
from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger("stockify.watchlist.feed")

KEY = "watchlist_quote"


def merged(universe: dict, day: date) -> dict:
    """`universe` plus `watchlist_quote` (a new dict; `universe` itself is not changed). Never raises."""
    try:
        from stockify.watchlist import store
        cap = store.max_stocks()
        symbols = store.symbols()[:cap]
    except Exception as e:  # noqa: BLE001 — the recording goes on without the watchlist
        log.error("watchlist: the watched stocks are not added to today's feed (%s); the recording goes on without "
                  "them", type(e).__name__, exc_info=True)
        return universe
    if not symbols:
        return universe
    out = dict(universe)
    out[KEY] = [f"NSE:{s}" for s in symbols]
    log.info("watchlist: %d watched stock(s) join the feed for %s in quote mode", len(symbols), day)
    return out
