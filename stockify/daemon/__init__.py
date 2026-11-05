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


def run_recorder(*, ticker_spec: str = "live", faults: list[str] | None = None,
                 stop: threading.Event | None = None) -> int:
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
    try:
        while not stop.is_set() and not session.stopped.is_set():
            session.tick()
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
