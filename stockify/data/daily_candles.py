"""CW 1.3: nightly daily candles. Kite's adjusted daily candles (K-6) for the screener's liquid universe and the
watchlist, topped up right after the close while the day's Kite login is still valid (spec 2026-11-19, Phase 1).

`targets(day)`: the newest fundamentals universe (`universe.current()`), each stock by its refdata id (a row without
one is looked up by its NSE symbol), then the watchlist's stocks (`_watchlist_ids()`, `watchlist.store.ids()`; an
unreadable watchlist is logged and skipped), each stock once.

`up_to_date(iid, day)`: the bar cache's fetch log already has a daily fetch reaching `day`, made after `day`'s
close (sessions.json, via `calendar.session_close_ns`), so a candle fetched during the session is fetched again.

`run(day, ...)` for each target, in order:

* skipped, with a plain reason: "not in instruments", "not an NSE equity or ETF" (an index, another exchange),
  "a government security, not a stock" (a sovereign gold bond or G-sec, the desk's rule), "no Kite token (delisted
  or suspended)"; an ETF is kept (Kite lists it as an equity), so the watchlist's 52-week alerts have its candles;
* up to date: nothing asked;
* otherwise `history.top_up_daily` (candles.json `history_years`), through this job's own bucket at candles.json
  `rps`, so it never shares the process-wide one.

The login is checked first: none, and nothing is asked (`logged_in` False). A login that stops working mid-run (a 403
or the SDK's token error) stops the run with `logged_in` False. A 429 waits each of candles.json
`rate_limit_backoff_s` in turn and tries the stock again; one more 429 stops the run (`stopped` "rate_limited").
`deadline_ns` (on `clock`) is checked before each stock and, inside the top-up, between chunks (`stopped`
"deadline"). Any other error fails that stock only (`failed`); the rest carry on. `complete` when nothing stopped
the run and nothing failed.

Money paise, time UTC epoch ns; IST only for the trade date. The run makes no orders and writes only bars.sqlite.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from stockify import timeutil
from stockify.data import calendar, history, instruments

log = logging.getLogger("stockify.data.daily_candles")

SEC = 1_000_000_000
CLOSE_GROUP = "CM"                       # the cash market's session close ends a stock's day (sessions.json)

NOT_IN_INSTRUMENTS = "not in instruments"
NOT_NSE_STOCK = "not an NSE equity or ETF"
GOVERNMENT_SECURITY = "a government security, not a stock"
STOCK_KINDS = ("EQ", "ETF")              # refdata stores an ETF as EQ (Kite's dump); "ETF" is defensive, for a
                                         # refdata that one day tells them apart
NO_TOKEN = "no Kite token (delisted or suspended)"


def settings() -> dict:
    from stockify.config import loader
    return loader.load_json("candles.json")


# ── targets ──────────────────────────────────────────────────────────────────

def _watchlist_ids() -> list[int]:
    """The watchlist's instrument ids in list order (`watchlist.store.ids()`, CW 5.1). An unreadable watchlist is
    logged and gives []: the universe's candles are fetched all the same."""
    try:
        from stockify.watchlist import store as watch_store
        return [int(i) for i in watch_store.ids()]
    except Exception as e:  # noqa: BLE001 — the candle job never stops for the watchlist
        log.warning("daily candles: the watchlist could not be read (%s); only the universe's candles are fetched",
                    type(e).__name__)
        return []


def targets(day: date) -> list[tuple[int | None, str, str | None]]:
    """[(instrument_id or None, symbol, ISIN or None)]: the newest liquid universe by symbol, then the watchlist (no
    ISIN), each id once. `day` is the trade date being fetched (the universe is the newest one read, whatever its
    date)."""
    from stockify.fundamentals import universe
    out: list[tuple[int | None, str, str | None]] = []
    seen: set[int] = set()
    for row in universe.current():
        iid = row.get("instrument_id") or instruments.find("NSE", row["symbol"])
        if iid is not None:
            if iid in seen:
                continue
            seen.add(iid)
        out.append((iid, row["symbol"], row.get("isin")))
    for iid in _watchlist_ids():
        if iid in seen:
            continue
        seen.add(iid)
        out.append((iid, (instruments.get(iid) or {}).get("tradingsymbol") or str(iid), None))
    return out


def _close_ns(day: date) -> int:
    try:
        return calendar.session_close_ns(CLOSE_GROUP, day)
    except calendar.CalendarError:      # no session rule for the day: only a fetch after it ends will do
        return timeutil.ist_ns(day, "23:59") + 59 * SEC


def up_to_date(instrument_id: int, day: date) -> bool:
    """True when a daily fetch reaching `day` was made after `day`'s close."""
    row = history._connect().execute(
        "SELECT 1 FROM bar_fetch_log WHERE instrument_id=? AND interval='day' AND outcome='ok' AND to_ns>=? "
        "AND fetched_at_ns>=? LIMIT 1",
        (int(instrument_id), timeutil.ist_ns(day, "00:00"), _close_ns(day))).fetchone()
    return row is not None


def _skip_reason(iid: int | None, day: date, symbol: str = "", isin: str | None = None) -> str | None:
    if iid is None:
        return NOT_IN_INSTRUMENTS
    info = instruments.get(iid)
    if info is None:
        return NOT_IN_INSTRUMENTS
    if info.get("exchange") != "NSE" or info.get("kind") not in STOCK_KINDS:
        return NOT_NSE_STOCK
    if instruments.is_government_security(info.get("tradingsymbol") or symbol, isin):
        return GOVERNMENT_SECURITY
    if instruments.token_at(iid, day) is None:
        return NO_TOKEN
    return None


# ── errors ───────────────────────────────────────────────────────────────────

def _login_failed(e: BaseException) -> bool:
    """The login stopped working: this codebase's `KiteAuthError`, or a 403 / the SDK's token error."""
    try:
        import kite_client
        if isinstance(e, kite_client.KiteAuthError):
            return True
    except ImportError:  # pragma: no cover - kite_client is part of the repo
        pass
    from stockify.daemon.ticker import is_auth_failure
    return is_auth_failure(getattr(e, "code", None), e)


def _rate_limited(e: BaseException) -> bool:
    return str(getattr(e, "code", "")).strip() == "429" or "too many requests" in str(e).lower()


class _Counting:
    """The client as `history` sees it: every historical_data call is counted, whether it succeeds or not."""

    def __init__(self, inner):  # noqa: ANN001
        self._inner, self.requests = inner, 0

    def historical_data(self, *args, **kw):  # noqa: ANN002, ANN003, ANN201
        self.requests += 1
        return self._inner.historical_data(*args, **kw)


def _logged_in_default() -> bool:
    try:
        import kite_client
    except ImportError:  # pragma: no cover
        return False
    return kite_client.is_authenticated()


# ── the run ──────────────────────────────────────────────────────────────────

def run(day: date, *, client=None, deadline_ns: int | None = None, now: Callable[[], int] | None = None,  # noqa: ANN001
        clock=None, logged_in: Callable[[], bool] | None = None) -> dict:  # noqa: ANN001
    """The nightly candles for IST trade date `day` (see the module doc). `clock` paces the requests and the 429
    waits (default the wall clock); `now` reads the deadline's time (default `clock.now_ns`). Returns {targets,
    fetched, up_to_date, skipped [{symbol, reason}], failed [{symbol, error}], refetched [symbols], requests,
    complete, logged_in, stopped: None | "login" | "deadline" | "rate_limited"}."""
    if clock is None:
        from stockify.daemon.clock import WallClock
        clock = WallClock()
    now = now or clock.now_ns
    out: dict = {"targets": 0, "fetched": 0, "up_to_date": 0, "skipped": [], "failed": [], "refetched": [],
                 "requests": 0, "complete": False, "logged_in": True, "stopped": None}
    if not (logged_in or _logged_in_default)():
        log.info("candles for %s: no Kite login; tried again after the next login", day)
        out["logged_in"] = False
        return out
    if deadline_ns is not None and now() >= deadline_ns:            # past it: no client, no targets, no request
        log.info("candles for %s: too close to the next session; runs later", day)
        out["stopped"] = "deadline"
        return out
    cfg = settings()
    if client is None:
        from stockify import kite_readonly
        client = kite_readonly.client()
    counting = _Counting(client)
    bkt = history.TokenBucket(float(Decimal(cfg["rps"])), clock)
    waits = [int(w) for w in cfg["rate_limit_backoff_s"]]
    todo = targets(day)
    out["targets"] = len(todo)
    try:
        for iid, symbol, isin in todo:
            if deadline_ns is not None and now() >= deadline_ns:
                out["stopped"] = "deadline"
                break
            try:
                reason = _skip_reason(iid, day, symbol, isin)
            except Exception as e:  # noqa: BLE001 — a lookup that raises fails this stock only
                log.exception("candles for %s: %s could not be looked up", day, symbol)
                out["failed"].append({"symbol": symbol, "error": f"{type(e).__name__}: {e}"})
                continue
            if reason is not None:
                out["skipped"].append({"symbol": symbol, "reason": reason})
                continue
            if up_to_date(iid, day):
                out["up_to_date"] += 1
                continue
            stop = _one(iid, symbol, day, cfg, counting, bkt, clock, now, deadline_ns, waits, out)
            if stop is not None:
                out["stopped"] = stop
                if stop == "login":
                    out["logged_in"] = False
                break
    finally:
        out["requests"] = counting.requests
    out["complete"] = out["stopped"] is None and not out["failed"]
    log.info("candles for %s: %d targets, %d fetched, %d up to date, %d skipped, %d failed, %d Kite requests%s", day,
             out["targets"], out["fetched"], out["up_to_date"], len(out["skipped"]), len(out["failed"]),
             out["requests"], f"; stopped: {out['stopped']}" if out["stopped"] else "")
    return out


def _one(iid: int, symbol: str, day: date, cfg: dict, client, bkt, clock, now, deadline_ns,  # noqa: ANN001
         waits: list[int], out: dict) -> str | None:
    """Top up one stock, retrying after each 429 wait. Returns why the run must stop, or None to go on."""
    for attempt in range(len(waits) + 1):
        try:
            res = history.top_up_daily(iid, day, years=int(cfg["history_years"]), client=client, bkt=bkt,
                                       deadline_ns=deadline_ns, today=day)
        except Exception as e:  # noqa: BLE001 — sorted below: the login, a 429, or this stock's own trouble
            if _login_failed(e):
                log.warning("candles for %s: Kite refused the login at %s; stopping until the next login", day,
                            symbol)
                return "login"
            if _rate_limited(e):
                if attempt == len(waits):
                    log.warning("candles for %s: Kite still says too many requests at %s after waiting %s s; "
                                "stopping, tried again later", day, symbol, " + ".join(map(str, waits)))
                    return "rate_limited"
                clock.sleep_until(clock.now_ns() + waits[attempt] * SEC)
                if deadline_ns is not None and now() >= deadline_ns:
                    return "deadline"
                continue
            log.warning("candles for %s: %s failed: %s", day, symbol, type(e).__name__)
            out["failed"].append({"symbol": symbol, "error": f"{type(e).__name__}: {e}"})
            return None
        if res.outcome == "stopped":
            return "deadline"
        if res.outcome == "no_token":
            out["skipped"].append({"symbol": symbol, "reason": NO_TOKEN})
            return None
        out["fetched"] += 1
        if res.refetched:
            out["refetched"].append(symbol)
        return None
    return None  # pragma: no cover - the loop always returns
