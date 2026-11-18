"""FS 8: the fundamentals screener's daily job (run by the daemon after the evening's NSE files) and the backfill.

`run(day)` does these steps in order, with a check of the deadline and the request budget before each step that asks
NSE:

1. the universe and its sectors (`universe.refresh`, today's Nifty 500 list and its Industry column) on Fridays, or
   when the newest universe is a week old or there is none. A refresh that cannot be made (cm_eod too short, NSE
   down) keeps the last universe and is logged; NSE refusing stops the run;
2. the results calendar (`calendar.refresh`), every day;
3. the filings listings (`filings.list_for`) of the universe's companies that are due: first those with a results
   meeting in the last LOOKBACK_DAYS days on or after which nothing was filed yet (asked again each day until it is,
   or the meeting falls out of the lookback), then the weekly re-listing, spread over the five weekdays by a stable
   split of the symbols (`relist_weekday`; a company missed on its weekday, e.g. a holiday, is listed once
   OVERDUE_DAYS have passed). A company never listed is left to `stockify fundamentals backfill` (ruling R12): the
   run logs how many remain. `filings_listed` remembers when each was listed, so a cut-short run resumes where it
   stopped; one company's trouble is listed under `failed` and the others go on;
4. the XBRL files of the listed filings (`filings.download_pending`, given the deadline and what is left of the
   budget) and their figures (`xbrl.ingest_pending`);
5. the day's list (`screen.save(day, screen.order(day))`, which keeps the details series too).

`deadline_ns` (UTC ns on the client's Http clock, the daemon's time): checked before each NSE step and before each
company, and handed to the download. Once it has passed, no further NSE request is made and nothing else is done
(`stopped` "deadline"). NSE refusing (`nse_api.Blocked`, or the download's `stopped` "blocked") stops the run the
same way (`stopped` "blocked"). Either way the run is not `complete`, and the daemon tries it again later.

`budget` (ruling R12; the daemon passes fundamentals.json `daily_request_budget`, 900, about 15 minutes at one
request a second): the NSE requests one run may make, warm-ups and retries included. Once it is spent the run asks
NSE nothing more; what is left (companies, files) waits for the next day's run. The run still reads the figures and
saves the day's list, and is `complete` (`budget_spent` says it stopped early).

One run at a time across processes (`hold`): the daemon's job, `fundamentals refresh` and `fundamentals backfill`
take one OS lock, so two never ask NSE at once (two throttles would make two requests a second) or race for the same
files. A run that finds it held raises `Busy`.

The job needs no Kite login (prices are NSE's raw closes in cm_eod), so it has no "not logged in" skip.

`backfill()` is the one-off history: NSE's CM bhavcopy from `filings_from` when cm_eod lacks days of it (market
cap needs the raw closes), then every universe company not listed yet, then the downloads, the figures and the
day's list. It prints its progress, resumes after what is already done, and stops when a download batch makes no
progress (the same files failing again).
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import msvcrt
import os
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from typing import Any

from stockify import dbconn, paths, timeutil
from stockify.data import nse_files
from stockify.fundamentals import calendar as fcal
from stockify.fundamentals import filings, nse_api, screen, store, universe, xbrl

log = logging.getLogger("stockify.fundamentals")

UNIVERSE_WEEKDAY = 4                    # Friday: the Nifty 500 list and its sectors are read again
UNIVERSE_MAX_AGE_DAYS = 7               # ... or whenever the newest universe is a week old
RELIST_WEEKDAYS = 5                     # the weekly re-listing is split over Monday to Friday
RELIST_MIN_GAP_DAYS = 3                 # on its weekday a company listed fewer days ago than this is not listed again
OVERDUE_DAYS = 14                       # a company not listed for two weeks (its weekday a holiday) is listed any day
LOOKBACK_DAYS = 14                      # a results meeting this recent with nothing filed since is asked about daily
DOWNLOAD_BATCH = 100                    # the backfill's XBRL files between two progress lines
LOCK_NAME = "run.lock"
_LOCK_OFFSET = 1 << 20                  # the locked byte lies beyond the file's end (geo_monitor/cycle_guard.py)


def filings_from() -> date:
    """fundamentals.json `filings_from` (2021-04-01): the first period end kept, and the backfill's first day."""
    return date.fromisoformat(nse_api.config()["filings_from"])


def daily_request_budget() -> int:
    """fundamentals.json `daily_request_budget`: the NSE requests one daemon run may make (ruling R12)."""
    return int(nse_api.config()["daily_request_budget"])


# ── one run at a time ────────────────────────────────────────────────────────

class Busy(Exception):
    """Another fundamentals run (the daemon's job, a refresh or a backfill) holds the lock."""


def _lock_path():  # noqa: ANN202
    return paths.nse_raw_dir("fundamentals") / LOCK_NAME


@contextlib.contextmanager
def hold() -> Iterator[None]:
    """Hold the fundamentals run lock for the block; Busy when another run holds it. An OS lock on one byte (the
    pattern of geo_monitor/cycle_guard.py): Windows frees it when the holder's process ends, so a crash never leaves
    a stale lock. It is per file handle, so two threads of one process exclude each other too."""
    fd = os.open(str(_lock_path()), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            raise Busy("another fundamentals run is in progress") from None
        try:
            yield
        finally:
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _counting(http) -> Iterator[list[int]]:  # noqa: ANN001
    """[requests sent through `http`] for the block, counted on its session, so the Http's own homepage warm-up,
    page warm-ups and retries all count; the session is left as it was after."""
    count = [0]
    session = http.session
    real = session.get
    own = vars(session).get("get") if hasattr(session, "__dict__") else None

    def get(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        count[0] += 1
        return real(*a, **kw)
    session.get = get
    try:
        yield count
    finally:
        if own is None:
            del session.get
        else:
            session.get = own


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


def relist_weekday(symbol: str) -> int:
    """The weekday (Monday 0 to Friday 4) of `symbol`'s weekly re-listing: a stable split by the symbol's sha256."""
    return int(hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:8], 16) % RELIST_WEEKDAYS


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
    """The companies of `symbols` whose listings are read on `day` (step 3 of the module doc), in that order: the
    results meetings first (by symbol), then the weekly re-listing (oldest listed first). Never-listed companies are
    not among them (they are the backfill's)."""
    listed = _listed_all()
    known = [s for s in symbols if s in listed and listed[s] < day]          # listed today already: a retry
    met = sorted(set(known) & _met_without_filing(day))

    def weekly(s: str) -> bool:
        age = (day - listed[s]).days
        return age >= OVERDUE_DAYS or (day.weekday() == relist_weekday(s) and age >= RELIST_MIN_GAP_DAYS)
    stale = sorted((s for s in known if s not in met and weekly(s)), key=lambda s: (listed[s], s))
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


def run(day: date, *, client: nse_api.Client | None = None, deadline_ns: int | None = None,
        budget: int | None = None) -> dict:
    """One daily run for IST trade date `day` (see the module doc). Returns {day, universe, calendar, listed,
    never_listed, failed [{symbol, error}], download, ingest, saved, requests, budget_spent, stopped: None |
    "deadline" | "blocked", complete}. Busy when another run holds the lock."""
    client = client or nse_api.Client()
    with hold(), _counting(client.http) as count:
        out: dict[str, Any] = {"day": day.isoformat(), "universe": "kept", "calendar": None, "listed": 0,
                               "never_listed": 0, "failed": [], "download": None, "ingest": None, "saved": None,
                               "requests": 0, "budget_spent": False, "stopped": None, "complete": False}
        try:
            _nse_steps(day, client, deadline_ns, budget, count, out)
        finally:
            out["requests"] = count[0]
        if out["stopped"] is not None:
            return out
        out["ingest"] = xbrl.ingest_pending()
        rows = screen.order(day)
        screen.save(day, rows)
        out["saved"] = len(rows)
        out["complete"] = not out["failed"]
        return out


def _nse_steps(day: date, client: nse_api.Client, deadline_ns: int | None, budget: int | None,
               count: list[int], out: dict) -> None:
    """Steps 1 to 4's downloads; sets out["stopped"] on a deadline or a refusal, out["budget_spent"] on the budget."""
    clock = client.http.clock

    def can_ask() -> bool:
        if deadline_ns is not None and clock.now_ns() >= deadline_ns:
            log.info("fundamentals for %s: too close to the next session; the rest runs later", day)
            out["stopped"] = "deadline"
            return False
        if budget is not None and count[0] >= budget:
            if not out["budget_spent"]:
                log.info("fundamentals for %s: the day's %d NSE requests are spent; the rest waits for the next run",
                         day, budget)
            out["budget_spent"] = True
            return False
        return True

    def blocked(e: Exception, step: str) -> None:
        log.warning("fundamentals for %s: NSE refused the %s (%s); stopping, tried again later", day, step, e)
        out["stopped"] = "blocked"

    if not can_ask():
        return
    if _universe_due(day):
        try:
            universe.refresh(day, client=client)
            out["universe"] = "refreshed"
        except nse_api.Blocked as e:
            return blocked(e, "Nifty 500 list")
        except (universe.NotEnoughHistory, nse_files.NseError, ValueError) as e:
            log.warning("fundamentals for %s: the universe was not read again (%s); the last one is kept", day, e)
            out["universe"] = f"kept ({_error(e)})"
        if not can_ask():
            return
    try:
        out["calendar"] = fcal.refresh(day, client=client)
    except nse_api.Blocked as e:
        return blocked(e, "results calendar")
    except (nse_files.NseError, ValueError) as e:
        log.warning("fundamentals for %s: the results calendar could not be read: %s", day, e)
        out["failed"].append({"symbol": "(results calendar)", "error": _error(e)})
    symbols = [u["symbol"] for u in universe.current()]
    listed = _listed_all()
    out["never_listed"] = sum(1 for s in symbols if s not in listed)
    if out["never_listed"]:
        log.info("fundamentals for %s: %d companies of the universe were never listed; `stockify fundamentals "
                 "backfill` fills them", day, out["never_listed"])
    since = filings_from()
    for symbol in due_symbols(day, symbols):
        if not can_ask():
            return
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
    if not can_ask():
        return
    limit = None if budget is None else budget - count[0]
    out["download"] = filings.download_pending(client=client, limit=limit, deadline_ns=deadline_ns)
    stopped = out["download"]["stopped"]
    if stopped == "blocked":
        return blocked(RuntimeError("an XBRL file was refused"), "XBRL download")
    if stopped == "deadline":
        out["stopped"] = "deadline"
        log.info("fundamentals for %s: the download stopped before the next session; the rest runs later", day)
    elif stopped == "limit" and budget is not None:
        out["budget_spent"] = True
        log.info("fundamentals for %s: the day's %d NSE requests are spent; the other files wait for the next run",
                 day, budget)


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
    download, ingest, saved, stopped: None | "blocked" | "no universe" | "no progress"}. Busy when another run
    holds the lock."""
    client = client or nse_api.Client()
    with hold():
        return _backfill(since or filings_from(), limit, client, today or timeutil.ist_today(), progress,
                         cm_missing, cm_backfill)


def _backfill(since: date, limit: int | None, client: nse_api.Client, today: date,  # noqa: PLR0913
              progress: Callable[[str], None], cm_missing: Callable[[date, date], list[date]],
              cm_backfill: Callable[[date, date], Any] | None) -> dict:
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
    out["download"] = total
    while True:
        res = filings.download_pending(client=client, limit=DOWNLOAD_BATCH)
        for k in ("downloaded", "skipped", "failed"):
            total[k] += res[k]
        total["stopped"] = res["stopped"]
        progress(f"XBRL files: {total['downloaded']} downloaded, {total['skipped']} already kept, "
                 f"{total['failed']} failed, {len(filings.by_status('listed'))} to go")
        if res["stopped"] != "limit":
            break
        if res["downloaded"] + res["skipped"] == 0:          # the same files failing again: never loop on them
            progress(f"Stopping: the last {res['failed']} tries brought no file (NSE's archive failing on the same "
                     "files). They stay listed; run the backfill again later.")
            out["stopped"] = "no progress"
            return out
    if total["stopped"] == "blocked":
        return refused(RuntimeError("an XBRL file was refused"))
    out["ingest"] = xbrl.ingest_pending()
    progress(f"figures: {out['ingest']}")
    rows = screen.order(today)
    screen.save(today, rows)
    out["saved"] = len(rows)
    progress(f"the list for {today}: {len(rows)} stocks")
    return out
