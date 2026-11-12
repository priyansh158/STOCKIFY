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
from datetime import date

import kite_client  # noqa: F401  — first: patches the Kite SDK's client class to paper only (LCB 5.7)
from stockify import log as slog
from stockify import paths, timeutil
from stockify.daemon import exitcodes

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
    """Jobs whose milestone is not built yet are logged, not faked. `now_ns` is the daemon clock's time (default:
    the wall clock): the retry windows are judged against it."""
    now_ns = timeutil.now_ns() if now_ns is None else now_ns
    if job == "compaction":
        from stockify.data import compact
        try:
            res = compact.compact(day)
            if res.status == "failed":
                log.error("compaction of %s failed: %s; the landing file is kept", day, res.error)
            for warning in compact.disk_warnings():
                log.warning("disk: %s", warning)
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
                        "(run `python -m stockify nse fetch --date %s` later)", day, day)
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
                        "(run `python -m stockify nse margin-files --date %s` later)", day, day)
            state.mark(job, day)
    elif job == "prune":
        from stockify import store
        week_ago = now_ns - 7 * 86_400 * 1_000_000_000
        removed = store.prune_recorder_status(week_ago)
        log.info("pruned %d recorder_status rows older than 7 days", removed)
        state.mark(job, day)


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
    tried: dict[tuple[str, date], int] = {}                  # (job, day) -> last attempt, for retry_every_min

    def catch_up() -> None:
        """The scheduler's missed jobs, each at most once per its retry interval. Also what the wait for a Kite login
        runs on every poll (LCB 5.2): a missing login never holds up the earlier days' work."""
        now = clock.now_ns()
        for job, d in scheduler.missed_jobs(now, state):
            last = tried.get((job, d))
            if last is not None and now < last + scheduler.retry_every_ns(job):
                continue
            tried[(job, d)] = now
            _run_job(job, d, state, now)

    record = recorder or (lambda: run_market_day(ticker_spec="live", stop=stop, power=power,
                                                 session_ids=session_ids, token_fresh=token_fresh,
                                                 on_idle=catch_up))
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
            _daily_summary(today, state)
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
