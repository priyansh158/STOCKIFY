"""FS 8: the fundamentals screener's daily job (run by the daemon after the evening's NSE files) and the backfill.

`run(day)` does, in this order, every step that asks NSE preceded by a deadline check:

1. the universe and its sectors (`universe.refresh`, today's Nifty 500 list and its Industry column) on Fridays, or
   when the newest universe is a week old or there is none. A refresh that cannot be made (cm_eod too short, NSE
   down) keeps the last universe and is logged; NSE refusing stops the run;
2. the results calendar (`calendar.refresh`), every day;
3. the filings listings (`filings.list_for`) of the universe's companies that are due: those with a results meeting
   in the last LOOKBACK_DAYS days on or after which nothing was filed yet (asked again each day until it is, or
   the meeting falls out of the lookback), then every company not listed for LIST_EVERY_DAYS (a week).
   `filings_listed` remembers when each was listed, so a pass the deadline cut short resumes where it stopped;
   one company's trouble is listed under `failed` and the others go on;
4. the XBRL files of the listed filings (`filings.download_pending`, given the deadline) and their figures
   (`xbrl.ingest_pending`);
5. the day's list (`screen.save(day, screen.order(day))`, which keeps the details series too).

`deadline_ns` (UTC ns on the client's Http clock, the daemon's time): checked before each NSE step and before each
company, and handed to the download; once it has passed no further NSE request is made and nothing else is done
(`stopped` "deadline"). NSE refusing (`nse_api.Blocked`, or the download's `stopped` "blocked") stops the run the
same way (`stopped` "blocked"). Either way the run is not `complete`, and the daemon tries it again later. The job
needs no Kite login (prices are NSE's raw closes in cm_eod), so it has no "not logged in" skip.

`backfill()` is the one-off history: NSE's CM bhavcopy from `filings_from` when cm_eod lacks days of it (market
cap needs the raw closes), then every universe company not listed yet, then the downloads, the figures and the
day's list. It prints its progress and resumes after what is already done.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from stockify import dbconn, timeutil
from stockify.data import nse_files
from stockify.fundamentals import calendar as fcal
from stockify.fundamentals import filings, nse_api, screen, store, universe, xbrl

log = logging.getLogger("stockify.fundamentals")

UNIVERSE_WEEKDAY = 4                    # Friday: the Nifty 500 list and its sectors are read again
UNIVERSE_MAX_AGE_DAYS = 7               # ... or whenever the newest universe is a week old
LIST_EVERY_DAYS = 7                     # every company's listings are read at least once a week
LOOKBACK_DAYS = 14                      # a results meeting this recent with nothing filed since is asked about daily
DOWNLOAD_BATCH = 100                    # the backfill's XBRL files between two progress lines


def filings_from() -> date:
    return date.fromisoformat(universe.config()["filings_from"]) if hasattr(universe, "config") \
        else date.fromisoformat(nse_api.config()["filings_from"])


# ── when each company was listed ─────────────────────────────────────────────

def listed_on(symbol: str) -> date | None:
    """The IST day `symbol`'s listings were last read in full, or None."""
    row = store._connect().execute("SELECT listed_on FROM filings_listed WHERE symbol = ?", (symbol,)).fetchone()
    return date.fromisoformat(row[0]) if row else None


def note_listed(symbol: str, day: date, at_ns: int = 0) -> None:
    conn = store._connect()
    with dbconn.locked_write(store._lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR REPLACE INTO filings_listed (symbol, listed_on, listed_ns) VALUES (?, ?, ?)",
                     (symbol, day.isoformat(), int(at_ns)))
        conn.execute("COMMIT")


def _listed_all() -> dict[str, date]:
    return {r[0]: date.fromisoformat(r[1])
            for r in store._connect().execute("SELECT symbol, listed_on FROM filings_listed")}


def _met_without_filing(day: date) -> set[str]:
    """Companies with a results meeting from `day` - LOOKBACK_DAYS to `day` and no filing broadcast on or after it."""
    conn = store._connect()
    meetings = conn.execute(
        "SELECT symbol, MAX(meeting_date) FROM results_calendar WHERE meeting_date >= ? AND meeting_date <= ? "
        "GROUP BY symbol", ((day - timedelta(days=LOOKBACK_DAYS)).isoformat(), day.isoformat())).fetchall()
    out = set()
    for symbol, met in meetings:
        newest = conn.execute("SELECT MAX(broadcast_ns) FROM filings WHERE symbol = ?", (symbol,)).fetchone()[0]
        if newest is None or timeutil.ns_to_ist(newest).date().isoformat() < met:
            out.add(symbol)
    return out


def due_symbols(day: date, symbols: list[str]) -> list[str]:
    """The companies of `symbols` whose listings are read on `day` (step 3 of the module doc), in that order:
    the results meetings first (by symbol), then the oldest listed (never listed first)."""
    listed = _listed_all()
    fresh = {s for s, d in listed.items() if d >= day}                       # listed today already: a retry
    met = sorted(s for s in _met_without_filing(day) & set(symbols) if s not in fresh)
    stale = sorted((s for s in symbols if s not in fresh and s not in met
                    and (s not in listed or listed[s] <= day - timedelta(days=LIST_EVERY_DAYS))),
                   key=lambda s: (listed.get(s, date.min), s))
    return met + stale


def _universe_due(day: date) -> bool:
    as_of = store.latest_as_of()
    if as_of is None:
        return True
    read = date.fromisoformat(as_of)
    return read < day and (day.weekday() == UNIVERSE_WEEKDAY or (day - read).days >= UNIVERSE_MAX_AGE_DAYS)


# ── the daily job ────────────────────────────────────────────────────────────

def _error(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def run(day: date, *, client: nse_api.Client | None = None, deadline_ns: int | None = None) -> dict:
    """One daily run for IST trade date `day` (see the module doc). Returns {day, universe, calendar, listed,
    failed [{symbol, error}], download, ingest, saved, stopped: None | "deadline" | "blocked", complete}."""
    client = client or nse_api.Client()
    clock = client.http.clock
    out: dict[str, Any] = {"day": day.isoformat(), "universe": "kept", "calendar": None, "listed": 0, "failed": [],
                           "download": None, "ingest": None, "saved": None, "stopped": None, "complete": False}

    def past() -> bool:
        if deadline_ns is not None and clock.now_ns() >= deadline_ns:
            log.info("fundamentals for %s: too close to the next session; the rest runs later", day)
            out["stopped"] = "deadline"
            return True
        return False

    def blocked(e: Exception, step: str) -> dict:
        log.warning("fundamentals for %s: NSE refused the %s (%s); stopping, tried again later", day, step, e)
        out["stopped"] = "blocked"
        return out

    if past():
        return out
    if _universe_due(day):
        try:
            universe.refresh(day, client=client)
            out["universe"] = "refreshed"
        except nse_api.Blocked as e:
            return blocked(e, "Nifty 500 list")
        except (universe.NotEnoughHistory, nse_files.NseError, ValueError) as e:
            log.warning("fundamentals for %s: the universe was not read again (%s); the last one is kept", day, e)
            out["universe"] = f"kept ({_error(e)})"
        if past():
            return out
    try:
        out["calendar"] = fcal.refresh(day, client=client)
    except nse_api.Blocked as e:
        return blocked(e, "results calendar")
    except (nse_files.NseError, ValueError) as e:
        log.warning("fundamentals for %s: the results calendar could not be read: %s", day, e)
        out["failed"].append({"symbol": "(results calendar)", "error": _error(e)})
    since = filings_from()
    for symbol in due_symbols(day, [u["symbol"] for u in universe.current()]):
        if past():
            return out
        try:
            filings.list_for(symbol, client=client, since=since)
        except nse_api.Blocked as e:
            return blocked(e, f"filings listing of {symbol}")
        except (nse_files.NseError, ValueError) as e:
            log.warning("fundamentals for %s: the filings of %s could not be listed: %s", day, symbol, e)
            out["failed"].append({"symbol": symbol, "error": _error(e)})
            continue
        note_listed(symbol, day, clock.now_ns())
        out["listed"] += 1
    if past():
        return out
    out["download"] = filings.download_pending(client=client, deadline_ns=deadline_ns)
    if out["download"]["stopped"] == "blocked":
        return blocked(RuntimeError("an XBRL file"), "XBRL download")
    if out["download"]["stopped"] == "deadline":
        out["stopped"] = "deadline"
        log.info("fundamentals for %s: the download stopped before the next session; the rest runs later", day)
        return out
    out["ingest"] = xbrl.ingest_pending()
    rows = screen.order(day)
    screen.save(day, rows)
    out["saved"] = len(rows)
    out["complete"] = not out["failed"]
    return out


# ── the backfill ─────────────────────────────────────────────────────────────

def cm_missing(start: date, end: date) -> list[date]:
    """The CM trading days from `start` to `end` that refdata cm_eod has no EQ row for."""
    from stockify.data import calendar as dcal
    from stockify.data import refdata
    have = {r[0] for r in refdata._connect().execute(
        "SELECT DISTINCT trade_date FROM cm_eod WHERE trade_date >= ? AND trade_date <= ? AND series = 'EQ'",
        (start.isoformat(), end.isoformat()))}
    return [d for d in dcal.trading_days("CM", start, end) if d.isoformat() not in have]


def backfill(*, since: date | None = None, limit: int | None = None, client: nse_api.Client | None = None,
             today: date | None = None, progress: Callable[[str], None] = print,
             cm_missing: Callable[[date, date], list[date]] = cm_missing,
             cm_backfill: Callable[[date, date], Any] | None = None) -> dict:
    """The one-off history (module doc). `limit` caps the companies listed in this run. Returns {listed, failed,
    download, ingest, saved, stopped: None | "blocked" | "no universe"}."""
    client = client or nse_api.Client()
    today = today or timeutil.ist_today()
    since = since or filings_from()
    end = today - timedelta(days=1)
    out: dict[str, Any] = {"listed": 0, "failed": [], "download": None, "ingest": None, "saved": None,
                           "stopped": None}

    def refused(e: Exception) -> dict:
        progress(f"NSE refused the request ({e}). Nothing is lost: run the backfill again later and it resumes.")
        out["stopped"] = "blocked"
        return out

    lacking = cm_missing(since, end)
    if lacking:
        progress(f"cm_eod lacks {len(lacking)} trading days from {since} to {end}: fetching NSE's CM files first")
        if cm_backfill is None:
            def cm_backfill(start: date, stop: date) -> Any:
                return nse_files.backfill(start, stop, ("cm",), http=client.http,
                                          progress=lambda r: progress(f"cm {r.trade_date} {r.outcome} {r.detail}"))
        cm_backfill(since, end)
    if not universe.current():
        try:
            universe.refresh(today, client=client)
        except nse_api.Blocked as e:
            return refused(e)
        except (universe.NotEnoughHistory, nse_files.NseError, ValueError) as e:
            progress(f"No universe yet, and it could not be read: {e}")
            out["stopped"] = "no universe"
            return out
    listed = _listed_all()
    todo = [u["symbol"] for u in universe.current() if u["symbol"] not in listed]
    if limit is not None:
        todo = todo[:limit]
    for i, symbol in enumerate(todo, 1):
        try:
            rows = filings.list_for(symbol, client=client, since=since)
        except nse_api.Blocked as e:
            return refused(e)
        except (nse_files.NseError, ValueError) as e:
            progress(f"[{i}/{len(todo)}] {symbol}: not listed ({_error(e)})")
            out["failed"].append({"symbol": symbol, "error": _error(e)})
            continue
        note_listed(symbol, today, client.http.clock.now_ns())
        out["listed"] += 1
        progress(f"[{i}/{len(todo)}] {symbol}: {len(rows)} filings")
    total = {"downloaded": 0, "skipped": 0, "failed": 0, "stopped": None}
    while True:
        res = filings.download_pending(client=client, limit=DOWNLOAD_BATCH)
        for k in ("downloaded", "skipped", "failed"):
            total[k] += res[k]
        total["stopped"] = res["stopped"]
        progress(f"XBRL files: {total['downloaded']} downloaded, {total['skipped']} already kept, "
                 f"{total['failed']} failed, {len(filings.by_status('listed'))} to go")
        if res["stopped"] != "limit":
            break
    out["download"] = total
    if total["stopped"] == "blocked":
        return refused(RuntimeError("an XBRL file"))
    out["ingest"] = xbrl.ingest_pending()
    progress(f"figures: {out['ingest']}")
    rows = screen.order(today)
    screen.save(today, rows)
    out["saved"] = len(rows)
    progress(f"the list for {today}: {len(rows)} stocks")
    return out
