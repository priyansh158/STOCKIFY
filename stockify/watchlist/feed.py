"""CW 3: the watchlist's stocks join the recorder's feed.

`merged(universe, day)` returns a copy of the recorder's universe (universe.json) with `watchlist_quote`: the watched
stocks as "NSE:SYMBOL", in list order, at most `max_stocks` (watchlist.json). `SubscriptionManager` subscribes them in
QUOTE mode with the reason `watch:SYMBOL`; a stock the universe already holds keeps its own tier and reason.

It is called once, in `run_market_day`'s live branch, before the subscription plan is built: a stock added during the
session is recorded (and alerted on) from the next session. A replay never calls it: a replay's subscriptions are built
from universe.json alone, so a watched stock is replayed only when the universe itself holds it.

It sits on the recorder's start-up path, so it is bounded and fails soft. The list is read through its own read-only
connection that never waits for a lock (`timeout=0`; no migration is run), on a helper thread given BUDGET_S: a
watchlist.sqlite that is missing, corrupt, locked, being migrated or slow is logged and the universe comes back
unchanged.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import date

log = logging.getLogger("stockify.watchlist.feed")

KEY = "watchlist_quote"
BUDGET_S = 2.0                # the longest the recorder's start waits for the watchlist


def _read_symbols(cap: int) -> list[str]:
    """The first `cap` watched symbols in list order, read-only and without waiting for a lock; [] when the store
    does not exist yet."""
    from stockify import paths
    path = paths.watchlist_db()
    if not path.exists():
        return []
    conn = sqlite3.connect(paths.readonly_uri(path), uri=True, timeout=0, check_same_thread=False)
    try:
        return [str(r[0]) for r in conn.execute(
            "SELECT symbol FROM watchlist ORDER BY position, instrument_id LIMIT ?", (int(cap),))]
    finally:
        conn.close()


def merged(universe: dict, day: date, *, budget_s: float | None = None) -> dict:
    """`universe` plus `watchlist_quote` (a new dict; `universe` itself is not changed). Never raises; returns within
    the budget (BUDGET_S) whatever the store does."""
    try:
        from stockify.config import loader
        cap = int(loader.load_json("watchlist.json")["max_stocks"])
        box: dict = {}

        def read() -> None:
            try:
                box["symbols"] = _read_symbols(cap)
            except BaseException as e:  # noqa: BLE001 — reported by the caller
                box["error"] = e

        limit = BUDGET_S if budget_s is None else budget_s
        helper = threading.Thread(target=read, name="watch-feed", daemon=True)
        helper.start()
        helper.join(limit)
        if helper.is_alive():
            log.error("watchlist: the list did not answer within %.1f s; the watched stocks are not added to today's "
                      "feed and the recording goes on without them", limit)
            return universe
        if "error" in box:
            raise box["error"]
        symbols = box["symbols"][:cap]
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
