"""The STOCKIFY market daemon (M1.5 recorder; M5 adds paper sessions).

`run_recorder()` records one IST trading day:

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


def _run_job(job: str, day: date, state) -> None:  # noqa: ANN001
    """Jobs whose milestone is not built yet are logged, not faked."""
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
        elif all(r.outcome in ("ok", "skipped", "missing") and r.parsed_ok is not False for r in results)                 and timeutil.now_ns() >= scheduler.plan(day).nse_fetch_until_ns:
            # Past the retry window and NSE still has not published: stop asking for this day.
            log.warning("NSE files for %s still missing after the retry window; giving up "
                        "(run `python -m stockify nse fetch --date %s` later)", day, day)
            state.mark(job, day)
    elif job == "prune":
        from stockify import store
        week_ago = timeutil.now_ns() - 7 * 86_400 * 1_000_000_000
        removed = store.prune_recorder_status(week_ago)
        log.info("pruned %d recorder_status rows older than 7 days", removed)
        state.mark(job, day)


def _kite_token_fresh() -> bool:
    import kite_client
    return kite_client.is_authenticated()


def run_daemon(*, power=None, clock=None, stop: threading.Event | None = None,  # noqa: ANN001
               recorder=None, token_fresh=None, send_reminder=None) -> int:  # noqa: ANN001
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
    record = recorder or (lambda: run_recorder(ticker_spec="live", stop=stop, power=power))
    token_fresh = token_fresh or _kite_token_fresh
    state = scheduler.State()
    from stockify.daemon import reminder
    reminder_kw = {"send": send_reminder} if send_reminder else {}

    while not stop.is_set():
        now = clock.now_ns()
        for job, d in scheduler.missed_jobs(now, state):
            _run_job(job, d, state)

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
                state.mark("session", day)
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
    from stockify.config import loader
    from stockify.daemon.clock import WallClock
    from stockify.daemon.landing import LandingDB
    from stockify.daemon.recorder import Recorder
    from stockify.daemon.subscriptions import SubscriptionManager
    from stockify.daemon.ticker import RecorderSession, direct_call, live_call_in_ticker, make_live_ticker
    from stockify.data import calendar

    stop = stop or threading.Event()
    schedule = loader.load_json("schedule.json")
    replay = ticker_spec != "live"

    if replay:
        from stockify.daemon.replay_ticker import open_replay, parse_spec
        day, _ = parse_spec(ticker_spec)
        ticker, clock = open_replay(ticker_spec, faults=faults)
        landing = LandingDB(day, paths.ticks_replay_file(day))
        call = direct_call
    else:
        import kite_client
        clock = WallClock()
        day = timeutil.ist_today()
        if not calendar.is_trading_day("FO", day):
            log.info("%s is not an NSE trading day; nothing to record", day)
            return exitcodes.OK
        if not kite_client.is_authenticated():
            log.warning("no fresh Kite login: waiting for one (log in at /portfolio)")
            return exitcodes.TOKEN_EXPIRED
        _ensure_refdata(day)
        payload = kite_client.load_cached_token()
        ticker = make_live_ticker(kite_client._api_key(), payload["access_token"])
        landing = LandingDB(day)
        call = live_call_in_ticker

    tokens = _token_map(day)
    universe = loader.load_json("universe.json")
    manager = SubscriptionManager(day, universe, clock, log_rows=landing.log_subscriptions)
    for warning in manager.warnings:
        log.warning("subscriptions: %s", warning)
    recorder = Recorder(landing, tokens.get, clock)
    session = RecorderSession(ticker, recorder, manager, clock, call_in_ticker=call)
    end_ns = timeutil.ist_ns(day, schedule["jobs"]["unsubscribe"])

    if not replay:
        start_ns = timeutil.ist_ns(day, schedule["jobs"]["subscribe"])
        if clock.now_ns() < start_ns:
            log.info("waiting for the subscribe time %s IST", schedule["jobs"]["subscribe"])
            if not clock.sleep_until(start_ns, stop):
                return exitcodes.OK
        if clock.now_ns() >= end_ns:
            log.info("the session is over for %s", day)
            return exitcodes.OK

    recorder.start()
    session.start()
    ticker.connect(threaded=True)
    log.info("recording %s: %d instruments, landing %s", day, len(manager.current), landing.path)
    est = estimate_bytes_per_day(day, manager)
    log.info("expected landing size today: about %.0f MB (assumes about %d chain strikes once "
             "centred and one packet per instrument per second)", est["bytes"] / 1e6, est["chain_instruments"])
    last_power_check = 0
    bands = None
    if not replay:
        from stockify.data import limits
        bands = limits.Snapshotter(day, manager, client=kite_client.get_client(), clock=clock)
    try:
        while not stop.is_set() and not session.stopped.is_set():
            session.tick()
            if bands is not None:
                bands.tick()                    # price bands into daily_limits (M1.5)
            if power is not None and clock.now_ns() - last_power_check >= 60 * 1_000_000_000:
                last_power_check = clock.now_ns()
                if power.on_ac_power() is False:
                    if "on_battery" not in recorder.stats.alarms:
                        recorder.stats.alarms.append("on_battery")
                    log.warning("running on battery during the session: closing the lid would sleep the laptop")
            if replay and ticker.finished.is_set():
                break
            if clock.now_ns() >= end_ns:
                log.info("unsubscribe time reached")
                break
            stop.wait(0.05 if replay else 1.0)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        if not session.stopped.is_set():
            session.end_session()
        try:
            ticker.close()
            if replay:
                ticker.stop()
        except Exception as e:                  # the reactor may already be gone
            log.debug("ticker close: %s", e)
        recorder.stop()
        session.tick()
        landing.close()
        stats = recorder.stats
        log.info("recorded %d packets in %d frames; %d heartbeats; dropped %d; exit %d",
                 stats.packets, stats.frames, stats.heartbeats, stats.dropped_frames, session.exit_code)
    return session.exit_code
