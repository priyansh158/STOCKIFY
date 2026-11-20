"""The STOCKIFY market daemon (M1.5 recorder; M5 adds paper sessions).

`run_market_day()` (market_day.py) records one IST trading day and runs the paper sessions on it; `run_recorder()` is
the same with no sessions. Recording:

1. make sure today's instruments are synced (live; needs a fresh Kite login),
2. open the day's landing file and the subscription manager,
3. connect the ticker (live KiteTicker, or the replay ticker for development),
4. tick the session about once a second: re-centre chains, detect stalls,
   write health rows,
5. at the unsubscribe time (schedule.json, 15:45 IST) unsubscribe and stop.

It returns an exit code from `stockify.daemon.exitcodes` for the launcher.
A replay writes to `ticks/replay/`, never to the real landing file.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import date

import kite_client  # noqa: F401  — first: patches the Kite SDK's client class to paper only (LCB 5.7)
from stockify import log as slog
from stockify import paths, timeutil
from stockify.daemon import exitcodes
from stockify.commands import stockify_command

IDLE_INTERVAL_SEC = 60

_log = slog.get("daemon")
log = logging.getLogger("stockify.daemon")


def run_forever(*, once: bool = False) -> None:
    """The M0 idle stub, kept for `daemon --once` smoke tests."""
    _log.info("STOCKIFY daemon started (stub: no recorder or sessions yet)")
    import time
    try:
        while True:
            _log.info("idle")
            if once:
                return
            time.sleep(IDLE_INTERVAL_SEC)
    except KeyboardInterrupt:
        _log.info("STOCKIFY daemon stopped")


def _token_map(day: date) -> dict[int, int]:
    from stockify.data import refdata
    rows = refdata._connect().execute(
        "SELECT instrument_token, instrument_id FROM instrument_tokens "
        "WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
        (day.isoformat(), day.isoformat())).fetchall()
    return {r[0]: r[1] for r in rows}


def _ensure_refdata(day: date) -> None:
    from stockify.data import instruments, refdata
    newest = refdata._connect().execute(
        "SELECT MAX(last_seen) FROM instruments WHERE exchange='NFO' AND source='kite_dump'").fetchone()[0]
    if newest == day.isoformat():
        return
    log.info("syncing instruments for %s", day)
    instruments.sync(day, instruments.download_dumps())
    import kite_client
    kite_client.refresh_instruments(force=True)


def _is_token_failure(e: BaseException) -> bool:
    """A failure about the login: this codebase's `KiteAuthError`, or the SDK's `TokenException` / a 403 (Task 19's
    `is_auth_failure`, which matches the SDK's class by name and never imports it)."""
    from stockify.daemon.ticker import is_auth_failure
    return isinstance(e, kite_client.KiteAuthError) or is_auth_failure(getattr(e, "code", None), e)


def refresh_refdata(day: date, clock, stop: threading.Event,  # noqa: ANN001
                    on_idle: Callable[[], None] | None = None,
                    token_fresh: Callable[[], bool] | None = None) -> bool | None:
    """The `refdata` job (CDM 5.1): the day's instruments, which every session's day bundle is resolved from, so no
    bundle is made before this returns. A failure is retried every `token_dependent_retry.every_min` until
    `until` (09:10 IST); after that a failure that is not about the login leaves D-1's refdata in use and returns True
    (the bundles are labelled `refdata_stale`). A failure about the login (`_is_token_failure`) marks the token
    rejected and waits for a later login (`waiting_for_login` set, the owner told), then runs at once, until the
    session group's close. `on_idle` (the catch-up) runs between attempts. Returns False when fresh, True when stale,
    None when `stop` ended the wait."""
    from stockify.daemon import scheduler, token_watch
    p = scheduler.plan(day)
    step = scheduler.token_retry_ns()
    while True:
        try:
            _ensure_refdata(day)
            return False
        except Exception as e:  # noqa: BLE001 — a refresh failure never costs the day
            token = _is_token_failure(e)
            now = clock.now_ns()
            giving_up = (not token and now >= p.refdata_until_ns) or now >= p.unsubscribe_ns
            if giving_up:
                log.error("refdata for %s failed with %s: giving up; D-1's instruments stay in use (refdata_stale)",
                          day, type(e).__name__, exc_info=True)
                return True
            log.warning("refdata for %s failed (%s); trying again", day, type(e).__name__, exc_info=True)
            if token and token_fresh is not None:
                token_watch.write_status(rejected_token_mtime_ns=token_watch.token_mtime_ns())
                if not token_watch.wait_for_login(clock, stop, token_fresh=token_fresh,
                                                  on_idle=on_idle or (lambda: None), until_ns=p.unsubscribe_ns):
                    return None if stop.is_set() else True
                token_watch.login_accepted()
                continue
            if on_idle is not None:
                try:
                    on_idle()
                except Exception:  # noqa: BLE001
                    log.exception("a job run between refdata attempts failed")
            now = clock.now_ns()
            nxt = now + step
            if now < p.refdata_until_ns:
                nxt = min(nxt, p.refdata_until_ns)
            if not clock.sleep_until(nxt, stop):
                return None


# Rough per-row cost in the landing file: raw packet + parsed columns + index overhead.
ROW_OVERHEAD_BYTES = 160
PACKETS_PER_INSTRUMENT_PER_S = 1.0          # K-4: about one or two snapshots per second


def estimate_bytes_per_day(day: date, manager) -> dict:  # noqa: ANN001
    """Bytes/day estimate for the M1.5 owner gate; the first real day replaces it (RECON)."""
    from stockify.data import calendar
    full = sum(1 for s in manager.static.values() if s.mode == "full")
    quote = sum(1 for s in manager.static.values() if s.mode == "quote")
    chain_full = sum(2 * (2 * c.n_full + 1) for c in manager.chains)
    chain_quote = sum(2 * 2 * max(0, c.n_quote - c.n_full) for c in manager.chains)
    phases = calendar.phases("FO_FUT", day) or calendar.phases("FO_FUT", calendar.next_trading_day("FO", day))
    seconds = (phases[-1].end_ns - phases[0].start_ns) / 1e9
    per_s = ((full + chain_full) * (184 + ROW_OVERHEAD_BYTES)
             + (quote + chain_quote) * (44 + ROW_OVERHEAD_BYTES)) * PACKETS_PER_INSTRUMENT_PER_S
    return {"bytes": per_s * seconds, "full": full + chain_full, "quote": quote + chain_quote,
            "chain_instruments": chain_full + chain_quote, "seconds": seconds}


RETRY_SLEEP_CHECK_S = 15 * 60


def _run_job(job: str, day: date, state, now_ns: int | None = None) -> None:  # noqa: ANN001
    """Run one scheduler job for `day` (compaction, candles, quality, nse_fetch, margin_files, prune, finalise, parity,
    daily_summary, desk, fundamentals, backup). A job marks itself done in `state` when it is finished or has given up; one that is to be
    tried again returns without marking. `now_ns` is the daemon clock's time (default: the wall clock): the retry
    windows are judged against it."""
    now_ns = timeutil.now_ns() if now_ns is None else now_ns
    if job == "compaction":
        from stockify.data import compact
        try:
            res = compact.compact(day)
            if res.status == "failed":
                log.error("compaction of %s failed: %s; the landing file is kept", day, res.error)
            warnings = compact.disk_warnings()
            for warning in warnings:
                log.warning("disk: %s", warning)
            if warnings:                              # one email per drive per day; `post` only enqueues
                from stockify import paths
                from stockify.daemon import notify
                notify.disk_low(day, paths.lab_dir().drive, warnings)
            deleted = compact.apply_retention(day)
            if deleted:
                log.info("retention: deleted verified landing files for %s", ", ".join(deleted))
        except Exception as e:                       # never keep the laptop up over a compaction error
            log.exception("compaction of %s raised: %s", day, e)
        # Marked done either way so a failure cannot block sleep; the landing
        # file stays (tick_days status 'landing') and `compact --date` retries it.
        state.mark(job, day)
    elif job == "quality":
        from stockify.data import quality
        try:
            result = quality.report(day)
            d = result["day"]
            log.info("quality %s: %d instruments, %d packets, %d silent minutes; summary %s",
                     day, d["instruments"], d["packets"], d["silent_minutes"], result["summary_path"])
        except quality.QualityError as e:
            log.info("quality report for %s skipped: %s", day, e)
        except Exception as e:                       # a report must never keep the laptop up
            log.exception("quality report for %s failed: %s", day, e)
        state.mark(job, day)
    elif job == "nse_fetch":
        from stockify.daemon import scheduler
        from stockify.data import nse_files
        try:
            results = nse_files.fetch(day)
        except Exception as e:                       # network or config trouble: retried on the next pass
            log.exception("NSE fetch for %s failed: %s", day, e)
            return
        for r in results:
            log.info("NSE %s %s: %s %s", r.dataset, r.trade_date, r.outcome, r.detail)
        if nse_files.complete(results):
            state.mark(job, day)
        elif all(r.outcome in ("ok", "skipped", "missing") and r.parsed_ok is not False for r in results)                 and now_ns >= scheduler.plan(day).nse_fetch_until_ns:
            # Past the retry window and NSE still has not published: stop asking for this day.
            log.warning("NSE files for %s still missing after the retry window; giving up "
                        "(run `%s` later)", day, stockify_command(f"nse fetch --date {day}"))
            state.mark(job, day)
    elif job == "margin_files":
        from stockify.daemon import scheduler
        from stockify.data import nse_margin_files
        try:
            res = nse_margin_files.fetch_day(day)
        except Exception as e:                       # network or config trouble: retried on the next pass
            log.exception("margin files for %s failed: %s", day, e)
            return
        log.info("margin files %s: stored %d, already archived %d, missing %d, rejected %d, errors %d",
                 day, len(res.stored), len(res.unchanged), len(res.missing), len(res.rejected), len(res.errors))
        if nse_margin_files.complete(res):
            state.mark(job, day)
        elif now_ns >= scheduler.plan(day).margin_files_until_ns:
            log.warning("NSE margin files for %s still incomplete after the retry window; giving up "
                        "(run `%s` later)", day, stockify_command(f"nse margin-files --date {day}"))
            state.mark(job, day)
    elif job == "prune":
        from stockify import store
        week_ago = now_ns - 7 * 86_400 * 1_000_000_000
        removed = store.prune_recorder_status(week_ago)
        log.info("pruned %d recorder_status rows older than 7 days", removed)
        state.mark(job, day)
    elif job == "finalise":
        _finalise_job(day, state, now_ns)
    elif job == "parity":
        _parity_job(day, state)
    elif job == "daily_summary":
        _daily_summary(day, state)
    elif job == "candles":
        _candles_job(day, state, now_ns)
    elif job == "desk":
        _desk_job(day, state, now_ns)
    elif job == "fundamentals":
        _fundamentals_job(day, state, now_ns)
    elif job == "backup":
        _backup_job(day, state)


def _day_sessions(day: date) -> list[tuple[str, dict]]:
    """(session id, its row for `day`) of every session that opened `day`, in id order."""
    from stockify import store
    d = day.isoformat()
    out = []
    for s in store.list_sessions():
        row = store.get_session_day(s["session_id"], d)
        if row is not None:
            out.append((s["session_id"], row))
    return sorted(out, key=lambda x: x[0])


def _finalise_job(day: date, state, now_ns: int) -> None:  # noqa: ANN001
    """Finalise each session that opened `day` (D-1's SETTLE records, engine end of day, stored outputs). The day waits
    for NSE's evening files, then goes at the next trading day's `day_bundles` time. One session's trouble never stops
    another. The job is done when every session is finalised or has failed for good; `waiting`, `busy` and raised
    errors are tried again (finalise's own claim keeps a second scheduler out)."""
    from stockify.daemon import finalise, scheduler
    from stockify.data import calendar
    cutoff_passed = now_ns >= scheduler.plan(calendar.next_trading_day("FO", day)).day_bundles_ns
    files_complete = state.done("nse_fetch", day)
    complete = True
    for sid, row in _day_sessions(day):
        if row["status"] in ("finalised", finalise.FAILED_STATUS):      # a day the dispatcher failed is still finalised
            continue                                                    # from its journal (C-1); only a failed finalise stops
        try:
            res = finalise.finalise_day(sid, day, files_complete=files_complete, cutoff_passed=cutoff_passed)
        except Exception:  # noqa: BLE001
            log.exception("finalising %s for %s raised; it will be tried again", sid, day)
            complete = False
            continue
        if res.status == "failed":
            log.error("session %s cannot finalise %s: %s", sid, day, res.error)
            try:
                finalise.record_failure(sid, day, res.error, wall_ns=now_ns)
            except Exception:  # noqa: BLE001
                log.exception("could not record the failed finalise of session %s", sid)
                complete = False
        elif res.status != "finalised":
            complete = False
    if complete:
        state.mark("finalise", day)


PARITY_TRIES = 8                                    # `skipped` results retried this many times a day, then given up
BACKUP_TRIES = 3                                    # backup runs with copy errors, tried this many times a day


def _tries(state, name: str, day: date, limit: int) -> int:  # noqa: ANN001
    return sum(state.done(f"{name}_try{n}", day) for n in range(1, limit + 1))


def _parity_job(day: date, state) -> None:  # noqa: ANN001
    """Journal and recorder parity of each finalised session-day. `skipped` (the day could not be read now) is tried
    again, at most PARITY_TRIES times a day: then one `parity_gave_up` timeline row per session names the reason and
    the day is left. A pass or a fail is a result, recorded by the parity module, and is not run twice."""
    from stockify import parity, store
    d = day.isoformat()
    why: dict[str, list[str]] = {}
    for sid, row in _day_sessions(day):
        if row["status"] != "finalised":
            continue
        try:
            if not row.get("parity"):
                res = parity.journal_parity(sid, day)
                if res.outcome == "skipped":
                    why.setdefault(sid, []).append(f"journal: {res.summary.get('reason')}")
            if store.get_parity_result(sid, d, "recorder") is None:
                res = parity.recorder_parity(sid, day)
                if res.outcome == "skipped":
                    why.setdefault(sid, []).append(f"recorder: {res.summary.get('reason')}")
        except Exception as e:  # noqa: BLE001
            log.exception("parity of %s for %s raised; it will be tried again", sid, day)
            why.setdefault(sid, []).append(f"{type(e).__name__}: {e}")
    if not why:
        state.mark("parity", day)
        return
    n = _tries(state, "parity", day, PARITY_TRIES) + 1
    state.mark(f"parity_try{n}", day)
    if n < PARITY_TRIES:
        return
    for sid, reasons in why.items():
        log.error("parity of %s for %s: given up after %d tries (%s)", sid, day, n, "; ".join(reasons))
        try:
            store.add_timeline(sid, "parity_gave_up", {"day": d, "tries": n, "reason": "; ".join(reasons)},
                               wall_ns=timeutil.now_ns(), engine_ns=None)
        except Exception:  # noqa: BLE001
            log.exception("no timeline row for session %s", sid)
    state.mark("parity", day)


def _backup_job(day: date, state) -> None:  # noqa: ANN001
    """The nightly verified backup (GK 5.7). No target, or a target the backup refuses, is a logged skip. A run with
    copy errors (or an error raised) is tried again on later passes, at most BACKUP_TRIES times a day."""
    from pathlib import Path
    from stockify import backup
    from stockify.config import loader
    target = (loader.load_json("storage.json").get("backup") or {}).get("target")
    if not target:
        log.info("backup: no target configured (storage.json backup.target); skipped")
        state.mark("backup", day)
        return
    errors: list[str] = []
    try:
        res = backup.run(Path(target), day=day)
        errors = [str(e) for e in (getattr(res, "errors", None) or [])]
    except backup.BackupRefused as e:
        log.error("backup to %s refused: %s; skipped", target, e)
        state.mark("backup", day)
        return
    except Exception as e:  # noqa: BLE001 — never costs the day
        log.exception("backup to %s raised", target)
        errors = [f"{type(e).__name__}: {e}"]
    if not errors:
        state.mark("backup", day)
        return
    n = _tries(state, "backup", day, BACKUP_TRIES) + 1
    state.mark(f"backup_try{n}", day)
    for error in errors:
        log.error("backup (try %d of %d): %s", n, BACKUP_TRIES, error)
    if n >= BACKUP_TRIES:
        log.error("backup for %s: given up after %d tries", day, n)
        state.mark("backup", day)


DESK_MARGIN_NS = 10 * 60 * 1_000_000_000              # the desk job stops this long before the next session's preparation


def _desk_job(day: date, state, now_ns: int) -> None:  # noqa: ANN001
    """The holdings desk's nightly grading and closing views (HD 8, `stockify.desk.nightly`). It stops between
    holdings DESK_MARGIN_NS before the next session's preparation (the earlier of `refdata` and `day_bundles` in
    schedule.json), so it never delays the refdata, the day bundles or the recorder; what is left runs on a later pass. No Kite login, a stop at that deadline, or a raised error: tried again, not marked. Holdings
    that failed: tried again every `retry_every_min` until the window's `until`, then given up (marked)."""
    from stockify.daemon import scheduler
    from stockify.desk import nightly
    nxt = scheduler.plan(scheduler.session_day(now_ns))
    deadline = min(nxt.refdata_ns, nxt.day_bundles_ns) - DESK_MARGIN_NS
    try:
        started = timeutil.now_ns()                   # the daemon clock's `now_ns`, moved on by the time spent since
        res = nightly.run(day, deadline_ns=deadline, now=lambda: now_ns + (timeutil.now_ns() - started))
    except Exception:  # noqa: BLE001 — the desk never costs the daemon anything
        log.exception("the desk job for %s raised; it will be tried again", day)
        return
    if not res.get("logged_in", True):
        return                                        # `nightly.run` logged it: tried again on a later pass
    log.info("desk for %s: %d holdings, %d analysed, %d skipped, %d failed", day, res["holdings"], res["analysed"],
             len(res["skipped"]), len(res["failed"]))
    if res["complete"]:
        state.mark("desk", day)
    elif res["failed"] and now_ns >= scheduler.window_end_ns("desk", day):
        log.warning("desk for %s: %s still failing after the retry window; giving up for the day", day,
                    ", ".join(f["symbol"] for f in res["failed"]))
        state.mark("desk", day)


def _candles_job(day: date, state, now_ns: int) -> None:  # noqa: ANN001
    """The nightly daily candles (CW 1, `stockify.data.daily_candles`), at 16:40 on the day's Kite login. Like the
    desk it stops DESK_MARGIN_NS before the next session's preparation (the earlier of `refdata` and `day_bundles`),
    judged on the daemon clock: the job's clock starts at `now_ns`, and its waits are real.

    An older day whose newer day's 16:40 has passed is not fetched on its own: the newer day's top-up runs through
    it, so it is marked (superseded) once that newer day is done, and the newer run marks it when it completes. No
    Kite login (before the run or during it), a stop at the deadline, or a raised error: tried again, not marked (the
    morning catch-up runs it after the login). Stocks that failed, or Kite still saying too many requests: tried
    again every `retry_every_min` until the window's `until`, then given up (marked, the stocks logged)."""
    from stockify.daemon import scheduler
    from stockify.daemon.clock import OffsetClock, WallClock
    from stockify.data import daily_candles
    newest = scheduler.latest_due_day("candles", now_ns)
    if newest is not None and newest > day:
        if state.done("candles", newest):
            log.info("candles for %s: superseded by the run for %s, which topped up through it", day, newest)
            state.mark("candles", day)
        return                                        # the newer day's run covers this one
    nxt = scheduler.plan(scheduler.session_day(now_ns))
    deadline = min(nxt.refdata_ns, nxt.day_bundles_ns) - DESK_MARGIN_NS
    try:
        res = daily_candles.run(day, deadline_ns=deadline, clock=OffsetClock(WallClock(), now_ns))
    except Exception:  # noqa: BLE001 — the candles never cost the daemon anything
        log.exception("the candles job for %s raised; it will be tried again", day)
        return
    if not res.get("logged_in", True):
        return                                        # `daily_candles.run` logged it: tried again after the login
    if res["complete"]:
        state.mark("candles", day)
        for job, older in scheduler.missed_jobs(now_ns, state):
            if job == "candles" and older < day:
                log.info("candles for %s: superseded by the run for %s, which topped up through it", older, day)
                state.mark("candles", older)
        return
    if res["stopped"] == "deadline":
        return                                        # the rest runs on a later pass
    if now_ns >= scheduler.window_end_ns("candles", day) and (res["failed"] or res["stopped"] == "rate_limited"):
        why = (", ".join(f["symbol"] for f in res["failed"]) + " still failing" if res["failed"]
               else "Kite still saying too many requests")
        log.warning("candles for %s: %s after the retry window; giving up for the day (the next evening's top-up "
                    "fetches what is missing)", day, why)
        state.mark("candles", day)


def _fundamentals_job(day: date, state, now_ns: int) -> None:  # noqa: ANN001
    """The fundamentals screener's daily run (FS 8, `stockify.fundamentals.job`): universe, results calendar,
    filings, XBRL figures and the day's list. Like the desk it stops DESK_MARGIN_NS before the next session's
    preparation (the earlier of `refdata` and `day_bundles`), judged on the daemon clock: `now_ns` is the time this
    job started (the catch-up reads it per job) and the NSE client's Http runs on it, so no NSE request is made after
    the deadline. The day's runs share fundamentals.json `daily_request_budget` (R12): once it is spent the day's
    list is saved and the job is marked, whatever failed (it waits for tomorrow). A stop at the deadline, another
    run holding the lock (a CLI refresh or backfill) or a raised error: tried again, not marked. NSE refusing
    (blocked): tried again until the window's `until`, then given up for the day (marked, logged). Companies that failed: tried again every `retry_every_min` until the window's `until`, then given up
    (marked)."""
    from stockify.daemon import scheduler
    from stockify.daemon.clock import OffsetClock, WallClock
    from stockify.data import nse_files
    from stockify.fundamentals import job, nse_api
    nxt = scheduler.plan(scheduler.session_day(now_ns))
    deadline = min(nxt.refdata_ns, nxt.day_bundles_ns) - DESK_MARGIN_NS
    try:
        client = nse_api.Client(nse_files.Http(clock=OffsetClock(WallClock(), now_ns)))
        res = job.run(day, client=client, deadline_ns=deadline, budget=job.daily_request_budget())
    except job.Busy:
        log.info("fundamentals for %s: another fundamentals run (a refresh or backfill) is in progress; tried again "
                 "later", day)
        return
    except Exception:  # noqa: BLE001 — the screener never costs the daemon anything
        log.exception("the fundamentals job for %s raised; it will be tried again", day)
        return
    log.info("fundamentals for %s: universe %s, %s calendar meetings, %d companies listed, %d failed, list of %s, "
             "%s NSE requests%s; stopped: %s", day, res.get("universe"), res.get("calendar"), res.get("listed", 0),
             len(res["failed"]), res.get("saved"), res.get("requests"),
             " (the day's budget spent)" if res.get("budget_spent") else "", res["stopped"])
    if res["stopped"] == "blocked" and now_ns >= scheduler.window_end_ns("fundamentals", day):
        log.warning("fundamentals for %s: NSE still refused after the retry window; giving up for the day (the "
                    "next evening's run picks up what is left)", day)
        state.mark("fundamentals", day)
        return
    if res["stopped"] is not None:
        return                                        # `job.run` logged it: tried again on a later pass
    if res["complete"]:
        state.mark("fundamentals", day)
    elif res.get("budget_spent"):                     # the day's requests are spent: a retry would ask nothing (R12)
        log.warning("fundamentals for %s: the day's NSE request budget is spent; %s left for tomorrow's run", day,
                    ", ".join(f["symbol"] for f in res["failed"]))
        state.mark("fundamentals", day)
    elif res["failed"] and now_ns >= scheduler.window_end_ns("fundamentals", day):
        log.warning("fundamentals for %s: %s still failing after the retry window; giving up for the day", day,
                    ", ".join(f["symbol"] for f in res["failed"]))
        state.mark("fundamentals", day)


SUMMARY_TRIES = 5                                   # sends of one day's summary before the daemon gives up
_summary_flight: dict[date, list] = {}               # day -> [] while a send is in flight, then [its error or None]


def _daily_summary(day: date, state) -> None:  # noqa: ANN001
    """Tell the owner what the day's sessions did, once every session has finalised (or failed): finalising waits for
    NSE's settlement files, so this can be a later pass. The send is posted to the notifier's thread (this loop never
    waits for a mail server); a pass collects its outcome. A failed send is tried again on a later pass, at most
    SUMMARY_TRIES times a day, then logged once and given up. Nothing here stops the daemon."""
    if state.done("daily_summary", day):
        return
    from stockify.daemon import notify
    try:
        outcome = _summary_flight.get(day)
        if outcome is not None:
            if not outcome:
                return                                    # still being sent
            del _summary_flight[day]
            if outcome[0] is None:
                state.mark("daily_summary", day)
                return
        tries = sum(state.done(f"daily_summary_try{n}", day) for n in range(1, SUMMARY_TRIES + 1))
        if tries >= SUMMARY_TRIES:
            log.error("the daily summary for %s was not sent after %d tries: giving up", day, tries)
            state.mark("daily_summary", day)
            return
        if not notify.summary_ready(day):
            return
        flight: list = []
        if notify.post_daily_summary(day, flight.append):
            _summary_flight[day] = flight
            state.mark(f"daily_summary_try{tries + 1}", day)
    except Exception:  # noqa: BLE001
        log.exception("the daily summary failed")


def _kite_token_fresh() -> bool:
    import kite_client
    return kite_client.is_authenticated()


def run_daemon(*, power=None, clock=None, stop: threading.Event | None = None,  # noqa: ANN001
               recorder=None, token_fresh=None, send_reminder=None,  # noqa: ANN001
               session_ids: list[str] | None = None) -> int:
    """The live daemon: wait for the session, record it, then hibernate (D-32).

    Each pass handles one of three situations:

    1. today's session is over: run its post-session jobs, then try to sleep;
    2. the next session's wake time is still ahead (night, weekend, holiday):
       try to sleep until it;
    3. otherwise: wait for keep-awake time, hold the laptop awake, record the
       session, then continue with 1.

    Returns an exit code for the launcher: SLEEP (75) with next_wake.json
    written, TOKEN_EXPIRED (76) to wait for a Kite login, or the recorder's code.
    """
    from stockify.daemon import scheduler
    from stockify.daemon.clock import WallClock
    from stockify.daemon.keepawake import Power

    power = power or Power()
    clock = clock or WallClock()
    stop = stop or threading.Event()
    state = scheduler.State()
    from stockify.daemon import notify
    notify.resume_outbox()                                   # what an earlier run could not email (RM 7.2)
    tried: dict[tuple[str, date], int] = {}                  # (job, day) -> last attempt, for retry_every_min

    def catch_up() -> None:
        """The scheduler's missed jobs, each at most once per its retry interval. Also what the wait for a Kite login
        runs on every poll (LCB 5.2): a missing login never holds up the earlier days' work."""
        for job, d in scheduler.missed_jobs(clock.now_ns(), state):
            now = clock.now_ns()                             # each job gets the time it starts at, not the pass's:
            in_session = scheduler.session_in_progress(now)  # a job after a long one judges its deadline on it (C1)
            if in_session and job in scheduler.HELD_DURING_SESSION:
                continue                                     # not beside the live recorder: after the close (M-2)
            if not scheduler.runnable(job, d, state):
                continue                                     # parity and the summary wait for finalise
            last = tried.get((job, d))
            if last is not None and now < last + scheduler.retry_every_ns(job):
                continue
            tried[(job, d)] = now
            try:
                _run_job(job, d, state, now)
            except Exception:  # noqa: BLE001 — a failing job never stops the others
                log.exception("job %s for %s failed", job, d)

    def before_open(day: date) -> None:
        """What the open itself needs, unthrottled: D-1 finalised, so D's sessions start from its state."""
        from stockify.daemon import finalise
        prev = finalise.previous_trading_day(day)
        if not state.done("finalise", prev):
            _run_job("finalise", prev, state, clock.now_ns())

    record = recorder or (lambda: run_market_day(ticker_spec="live", stop=stop, power=power,
                                                 session_ids=session_ids, token_fresh=token_fresh,
                                                 on_idle=catch_up, before_open=before_open))
    token_fresh = token_fresh or _kite_token_fresh
    from stockify.daemon import reminder
    reminder_kw = {"send": send_reminder} if send_reminder else {}

    while not stop.is_set():
        now = clock.now_ns()
        catch_up()

        today = timeutil.ns_to_ist(now).date()
        if calendar_is_trading(today) and now >= scheduler.plan(today).unsubscribe_ns:
            obligations = today                                   # situation 1
            for job in ("compaction", "quality", "prune"):
                if not state.done(job, today):
                    _run_job(job, today, state)
        else:
            day = scheduler.session_day(now)
            p = scheduler.plan(day)
            wake = scheduler.next_wake_ns(now)
            if now < wake and wake <= p.keep_awake_ns:
                obligations = None                                # situation 2
            else:                                                 # situation 3
                if now < p.keep_awake_ns:
                    log.info("session %s starts soon; waiting until %s IST", day,
                             timeutil.ns_to_ist(p.keep_awake_ns).strftime("%H:%M"))
                    if not reminder.wait_with_reminders(clock, stop, p.keep_awake_ns, day, state,
                                                        scheduler.schedule(), token_fresh=token_fresh,
                                                        **reminder_kw):
                        return exitcodes.OK
                power.hold()
                try:
                    code = record()
                finally:
                    power.release()
                if code != exitcodes.OK:
                    return code
                continue                                          # now situation 1

        result = _sleep_or_wait(clock, power, state, obligations, stop)
        if result is not None:
            return result
    return exitcodes.OK


def _sleep_or_wait(clock, power, state, day, stop):  # noqa: ANN001
    """Hibernate when allowed (return SLEEP), or wait and return None to re-plan."""
    from stockify.daemon import scheduler

    now = clock.now_ns()
    flag = scheduler.stay_awake_flag()
    ok, why = scheduler.should_sleep(now, day, state, idle_s=power.idle_seconds(),
                                     stay_awake=flag.exists())
    wake = scheduler.next_wake_ns(now)
    if ok:
        scheduler.write_next_wake(wake)
        log.info("hibernating until %s IST", timeutil.ns_to_ist(wake).strftime("%Y-%m-%d %H:%M"))
        return exitcodes.SLEEP
    if why.startswith("stay_awake") or why.startswith("sleep_after_close disabled"):
        if flag.exists():
            flag.unlink()
        log.info("not sleeping (%s); staying up until %s IST", why,
                 timeutil.ns_to_ist(wake).strftime("%Y-%m-%d %H:%M"))
        return None if clock.sleep_until(wake, stop) else exitcodes.OK
    log.info("not sleeping yet: %s; checking again in 15 min", why)
    until = min(wake, now + RETRY_SLEEP_CHECK_S * 1_000_000_000)
    return None if clock.sleep_until(until, stop) else exitcodes.OK


def calendar_is_trading(day: date) -> bool:
    from stockify.data import calendar
    return calendar.is_trading_day("FO", day)


def run_recorder(*, ticker_spec: str = "live", faults: list[str] | None = None,
                 stop: threading.Event | None = None, power=None) -> int:  # noqa: ANN001
    """The recorder alone: `run_market_day` with no paper sessions (GK 5.1)."""
    from stockify.daemon import market_day
    return market_day.run_market_day(ticker_spec=ticker_spec, faults=faults, stop=stop, power=power, session_ids=[])


def run_market_day(**kwargs) -> int:
    """Record the day and run the paper sessions on it; see `stockify.daemon.market_day`."""
    from stockify.daemon import market_day
    return market_day.run_market_day(**kwargs)


def _release_ticket(ticker) -> None:  # noqa: ANN001
    """Give back a live ticker's connection-budget ticket (M5.14); replay tickers hold none."""
    ticket = getattr(ticker, "budget_ticket", None)
    if ticket is not None:
        ticket.release()
