"""One market day for the whole daemon: the recorder and the paper sessions from one ticker (GK 5.1; M5.1).

`run_market_day()` is `run_recorder()` plus sessions. It records the day exactly as the recorder always did, and, for
each chosen session, opens the session's day (journal first, then engine, then store: `SessionDay`) and feeds it
from the same frames:

    ticker thread   the live Kite ticker (or the replay ticker): `Recorder.on_message` stamps and enqueues each frame, and the
                    recorder's tap hands the stamped item to the dispatcher's queue. Nothing else runs here.
    recorder-writer parses frames into the landing file.
    engine          `Dispatcher.run`: one queue in, one engine thread, journal records for every session.
    main            the health tick, subscription re-centring, band snapshots (live only), power checks.

Recorded tick data is irreplaceable, so a session can never cost the recording: a session that cannot open is
logged, timelined `session_failed` and skipped; a session whose day fails later is isolated by the dispatcher; a
dead engine thread is logged and the recorder carries on.

Shutdown (a stop request: Ctrl+C or Ctrl+Break, or the end of the day), in this order:

    1. stop the ticker (and give back its connection ticket);
    2. drain both queues: the recorder's into the landing file, the dispatcher's into the journals;
    3. fsync the journals (checksums, then records: `JournalWriter.fsync`);
    4. if a stop was requested inside market hours, journal a scope-all GAP(process_stop) start and sync again;
    5. close the journals without finalising them: a day is finalised only after NSE's settlement prices (Task 16);
    6. write `daemon_status.json` (the last frame's number and receive time, the exit code);
    7. return the exit code.

A step that fails is logged and the rest still run, in particular `recorder.stop()` and the landing flush. The stop
handlers stay installed until all of this is over: a second Ctrl+C is ignored, a third forces the exit once the
recorder has stopped. If the engine thread will not stop within `ENGINE_JOIN_S`, steps 3 to 5 are skipped (the main
thread must not touch the dispatcher, journals or store beside a live engine thread), the status carries the alarm
`shutdown_engine_timeout` and the exit code is `ENGINE_STUCK`; the journals stay unfinalised and the next start
resumes them by replay.

The end of the day (the unsubscribe time, or a replay running out) is not a stop request and journals no gap.

Market data for the sessions (RM 5.4): at ARM each session's instruments are pinned FULL under `session:<id>` (a
re-centre never drops them) after a check that the total stays within `kite_limits.json` `ws_max_instruments`; a
universe over it leaves the session ARMED with the timeline row `subscription_cap`. The dispatcher reports each
session's open positions after every feed and they stay pinned until flat. Pin changes run in the ticker's thread
(`call_in_ticker`); the recorder's own subscriptions are never touched. Price bands snapshotted live reach the
journals as `price_band` REFDATA through the dispatcher's queue; a replay uses none.

Not here yet: a session's start-of-day state comes from the previous day's finalisation (Task 16's `next_sod_state`);
until then a day opens with `sod_state = None`. A journal that already holds records is replayed to resume it
(Task 17 adds the check against the stored rows).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import threading
from datetime import date
from pathlib import Path
from typing import Callable

from stockify import paths, store, timeutil
from stockify.daemon import exitcodes, journal, lifecycle
from stockify.daemon.engine_log import LogPump

log = logging.getLogger("stockify.daemon.market_day")

ALIVE = ("ARMED", "RUNNING", "PAUSED")            # the states `--sessions all` runs
MARKET_GROUPS = ("CM", "FO_FUT", "FO_OPT")        # a stop inside any of these is a stop mid-session
MARKET_PHASES = ("PREOPEN", "CONTINUOUS")
ENGINE_JOIN_S = 30.0


def inside_market_hours(now_ns: int) -> bool:
    """True from the pre-open to the close of continuous trading, on a trading day, for any segment a session may
    trade. A stop here leaves frames unseen that the journal must say it lacks."""
    from stockify.data import calendar
    return any(calendar.phase_at(group, now_ns) in MARKET_PHASES for group in MARKET_GROUPS)


class StopSignals:
    """Ctrl+C (SIGINT) and Ctrl+Break (SIGBREAK) ask for a stop, and keep being handled until shutdown is over (GK 5.2).

    The first sets `stop`. The second, while the daemon is shutting down, is logged and ignored. The third forces the
    exit, but only once the recorder has stopped (`recorder_stopped()`): the recorder's queue holds frames that exist
    nowhere else. Off the main thread nothing can be installed, and nothing is."""

    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.count = 0
        self.force_requested = False
        self._recorder_stopped = False
        self._previous: dict = {}

    def install(self) -> Callable[[], None]:
        try:
            self._previous = {sig: signal.signal(sig, self._handle) for sig in (signal.SIGINT, signal.SIGBREAK)}
        except ValueError:
            return lambda: None
        return self.restore

    def restore(self) -> None:
        for sig, old in self._previous.items():
            signal.signal(sig, old)
        self._previous = {}

    def _handle(self, signum, frame) -> None:  # noqa: ANN001
        self.count += 1
        if self.count == 1:
            self.stop.set()
        elif self.count == 2:
            log.warning("already shutting down; the second interrupt is ignored (a third forces the exit once the "
                        "recorder has stopped)")
        else:
            self.force_requested = True
            if self._recorder_stopped:
                self.force_exit()
            else:
                log.warning("forced exit requested; it happens as soon as the recorder has stopped")

    def recorder_stopped(self) -> None:
        self._recorder_stopped = True
        if self.force_requested:
            self.force_exit()

    def force_exit(self) -> None:
        log.error("forced exit")
        os._exit(1)


def install_signal_handlers(stop: threading.Event) -> Callable[[], None]:
    """`StopSignals(stop).install()`: returns the function that puts the old handlers back."""
    return StopSignals(stop).install()


class ReplayRefused(RuntimeError):
    """A replay asked to run paper sessions against the real lab."""


# %LOCALAPPDATA% as the process started, before `--replay-sandbox` (or a test) redirects it: a redirected AppData
# folder (folder redirection, roaming profiles) is still the real lab.
_START_LOCALAPPDATA = os.environ.get("LOCALAPPDATA")


def real_state_dirs() -> list[Path]:
    """Every folder that may be the real lab's state folder: `%LOCALAPPDATA%\STOCKIFY\kite` as the process started,
    and the same under the user's home, whatever the environment says now."""
    dirs = [Path.home() / "AppData" / "Local" / "STOCKIFY" / "kite"]
    if _START_LOCALAPPDATA:
        dirs.append(Path(_START_LOCALAPPDATA) / "STOCKIFY" / "kite")
    return dirs


def real_state_dir() -> Path:
    """The home-based real lab folder (kept for callers); `real_state_dirs` lists every candidate."""
    return real_state_dirs()[0]


def check_replay_sandbox() -> None:
    """A replay of a recorded day writes paper sessions (journals, store rows, timelines) as if the day were today.
    Against the real lab that would put a past day into the owner's real sessions, so it is refused unless the
    state folder is somewhere else (tests, or `--replay-sandbox DIR`)."""
    import stockify_paths
    here = stockify_paths.state_dir().resolve()
    if any(here == d.resolve() for d in real_state_dirs()):
        raise ReplayRefused("a replay will not run paper sessions in the real lab's store: give --replay-sandbox "
                            "<dir> (an empty directory elsewhere), or run the recorder alone with --sessions none")


def choose_sessions(session_ids: list[str] | None) -> list[dict]:
    """The session rows to run: every armed, running or paused one (`None`), or the named ones. A name that does not
    exist, or is not alive, is logged and skipped: a typo must not stop the recording."""
    rows = store.list_sessions()
    if session_ids is None:
        return [r for r in rows if r["status"] in ALIVE]
    by_id = {r["session_id"]: r for r in rows}
    chosen = []
    for sid in dict.fromkeys(session_ids):
        row = by_id.get(sid)
        if row is None:
            log.error("session %r does not exist; skipped", sid)
        elif row["status"] not in ALIVE:
            log.warning("session %r is %s, not armed, running or paused; skipped", sid, row["status"])
        else:
            chosen.append(row)
    return chosen


def _direct(fn: Callable, *args) -> None:
    fn(*args)


def _log_notify(event: str, detail: dict) -> None:
    """Stands in for the owner's notification (Task 25 replaces it): the log is the only channel until then."""
    log.error("NOTIFY %s: %s", event, detail)


class SessionPins:
    """Keeps each session's instruments subscribed (RM 5.4). Owner `session:<id>` pins the bundle's instruments from
    ARM and, while a position is open, its instrument as well (dropped again when flat). Changes to the manager run
    through `call_in_ticker` once `live` (the ticker thread owns the subscriptions); before that nothing is applied,
    for the first connect subscribes everything the manager holds. `apply(diff)` gives a diff to the ticker."""

    def __init__(self, manager, *, cap: int, call_in_ticker: Callable = _direct,  # noqa: ANN001
                 apply: Callable | None = None, notify: Callable[[str, dict], None] | None = None,
                 lock=None) -> None:  # noqa: ANN001
        self.manager = manager
        self.lock = lock if lock is not None else contextlib.nullcontext()   # the recorder session's `sub_lock`
        self.cap = int(cap)
        self.call_in_ticker = call_in_ticker
        self.apply = apply
        self.notify = notify or _log_notify
        self.live = False
        self.refused: dict[str, dict] = {}
        self._base: dict[str, frozenset[int]] = {}
        self._book: dict[str, frozenset[int]] = {}

    @staticmethod
    def owner(session_id: str) -> str:
        return f"session:{session_id}"

    def admit(self, sd) -> bool:  # noqa: ANN001
        """Pin the session's instruments, unless the day's subscriptions would then pass the cap: then nothing is
        pinned, `refused[session_id]` says why, and the owner is told."""
        total = self.manager.count_with(sd.ids)
        if total > self.cap:
            detail = {"total": total, "cap": self.cap, "session_instruments": len(sd.ids)}
            self.refused[sd.session_id] = detail
            self.notify("subscription_cap", {"session_id": sd.session_id, **detail})
            return False
        self._base[sd.session_id] = frozenset(sd.ids)
        self._pin(sd.session_id)
        return True

    def book_changed(self, sd, positions: frozenset[int]) -> None:  # noqa: ANN001
        """The session's open positions (instrument ids): pinned until flat."""
        self._book[sd.session_id] = frozenset(positions)
        self._base.setdefault(sd.session_id, frozenset(sd.ids))
        self._pin(sd.session_id)

    def release(self, sid: str) -> None:
        """The session's day failed: it needs no market data any more. Everything it pinned is released in one diff
        (`set_pin(owner, [])`); what the recorder subscribes for itself stays."""
        self._base.pop(sid, None)
        self._book.pop(sid, None)
        self._pin(sid)

    def _pin(self, sid: str) -> None:
        ids = self._base.get(sid, frozenset()) | self._book.get(sid, frozenset())
        if self.live:
            self.call_in_ticker(self._set, sid, ids, True)
        else:
            self._set(sid, ids, False)

    def _set(self, sid: str, ids: frozenset[int], apply: bool) -> None:
        with self.lock:                                 # computed and applied together, in the ticker's one order
            diff = self.manager.set_pin(ids, self.owner(sid))
            if apply and self.apply is not None and not diff.empty():
                self.apply(diff)


def band_sink(dispatcher, sessions: list):  # noqa: ANN001
    """`Snapshotter(on_band=...)`: a band snapshotted live becomes a `price_band` REFDATA record, queued (stamped `t`,
    first in first out with every frame) for each session whose bundle carries the instrument."""
    from stockify.data import packs

    def on_band(iid: int, lower: int, upper: int, t: int) -> None:
        for sd in sessions:
            if iid in sd.ids:
                dispatcher.put_inject(t, sd.session_id, packs.RANK[packs.REFDATA_K],
                                      packs.enc_refdata(iid, t, "price_band", {
                                          "instrument_id": iid, "upper_paise": upper, "lower_paise": lower,
                                          "source": "kite_quote"}))
    return on_band


def open_sessions(rows: list[dict], day: date, clock, log_sink=None,  # noqa: ANN001
                  pins: SessionPins | None = None) -> list:
    """Open each chosen session's day. One that cannot open is timelined and left out; the others go on. With `pins`,
    a session whose instruments would take the subscriptions past the cap does not run: an ARMED one stays ARMED,
    with the timeline row `subscription_cap`."""
    from stockify.daemon.session_day import SessionDay
    opened = []
    for row in rows:
        sid, now = row["session_id"], clock.now_ns()
        sd = SessionDay(sid, day, row["config"], mono=clock.mono_ns, log_sink=log_sink)
        over_cap = False
        try:
            jpath = paths.journal_file(sid, day)
            if jpath.exists() and jpath.stat().st_size > journal.HEADER_SIZE:
                if journal.is_finalised(jpath):
                    log.info("session %s: %s is finalised; nothing to run today", sid, day)
                    continue
                result = sd.replay()                  # a restart: rebuild the engine and the store from the journal
                log.info("session %s: resumed %s from its journal (%d records)", sid, day, result["records"])
                over_cap = pins is not None and not pins.admit(sd)
            else:
                sd.open(None, row["pause_reasons"], now)
                over_cap = pins is not None and not pins.admit(sd)
                if row["status"] == "ARMED" and not over_cap:
                    lifecycle.start_continuous(sid, wall_ns=now, engine_ns=now)
        except Exception as e:  # noqa: BLE001 — never cost the recording
            log.exception("session %s cannot run %s: %r", sid, day, e)
            try:
                store.add_timeline(sid, "session_failed", {"error": repr(e), "day": day.isoformat()},
                                   wall_ns=now, engine_ns=now)
            except Exception:  # noqa: BLE001
                log.warning("no timeline row for the failed session %s", sid, exc_info=True)
            sd.close()
            continue
        if over_cap:
            detail = {**pins.refused[sid], "day": day.isoformat()}
            log.error("session %s: %d subscriptions would pass the cap of %d; it does not run %s", sid,
                      detail["total"], detail["cap"], day)
            try:
                store.add_timeline(sid, "subscription_cap", detail, wall_ns=now, engine_ns=now)
            except Exception:  # noqa: BLE001
                log.warning("no timeline row for session %s", sid, exc_info=True)
            sd.close()
            continue
        opened.append(sd)
    return opened


def write_status(day: date, exit_code: int, landing, clock, *, stop_requested: bool, sessions: list,  # noqa: ANN001
                 failed: dict, alarms: list[str], skipped: dict | None = None) -> None:
    """`daemon_status.json`: what the daemon last knew, for the next start and for the owner. Written whole, then
    renamed into place."""
    last_frame = landing.max_frame_no()
    row = landing.conn.execute("SELECT IFNULL(MAX(recv_ns), 0) FROM packets WHERE frame_no = ?",
                               (last_frame,)).fetchone()
    status = {"schema": "stockify.daemon_status/1", "day": day.isoformat(), "exit_code": exit_code,
              "last_frame_no": last_frame, "last_frame_recv_ns": int(row[0]), "stopped_ns": clock.now_ns(),
              "stop_requested": stop_requested, "alarms": sorted(alarms),
              "skipped": {sid: {"reason": "subscription_cap", **detail} for sid, detail in sorted((skipped or {}).items())},
              "sessions": {sd.session_id: {"journal_records": sd.journal_records, "failed": sd.session_id in failed}
                           for sd in sessions}}
    target = paths.daemon_status_file()
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def run_market_day(*, ticker_spec: str = "live", faults: list[str] | None = None,
                   stop: threading.Event | None = None, power=None,  # noqa: ANN001
                   session_ids: list[str] | None = None,
                   token_fresh: Callable[[], bool] | None = None) -> int:
    """Record one IST trading day and run the chosen paper sessions on it (`session_ids`: None = every armed,
    running or paused session, [] = none, which is the recorder alone). Returns an exit code from `exitcodes`."""
    from stockify import daemon
    from stockify.config import loader
    from stockify.daemon.clock import WallClock
    from stockify.daemon.dispatcher import Dispatcher
    from stockify.daemon.encoder import FrameEncoder
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
        if session_ids != []:
            check_replay_sandbox()
        day, _ = parse_spec(ticker_spec)
        ticker, clock = open_replay(ticker_spec, faults=faults)
        landing = LandingDB(day, paths.ticks_replay_file(day))
        call = direct_call
        tokens = daemon._token_map(day)
        universe = loader.load_json("universe.json")
        manager = SubscriptionManager(day, universe, clock, log_rows=landing.log_subscriptions)
    else:
        import kite_client
        clock = WallClock()
        day = timeutil.ist_today()
        if not calendar.is_trading_day("FO", day):
            log.info("%s is not an NSE trading day; nothing to record", day)
            return exitcodes.OK
        if not (token_fresh or kite_client.is_authenticated)():
            log.warning("no fresh Kite login: waiting for one (log in at /portfolio)")
            return exitcodes.TOKEN_EXPIRED
        daemon._ensure_refdata(day)
        payload = kite_client.load_cached_token()
        from stockify.daemon import conn_budget
        try:
            ticker = make_live_ticker(kite_client._api_key(), payload["access_token"], role="daemon")
        except conn_budget.BudgetExceeded as e:
            log.error("no live ticker: %s", e)
            return exitcodes.RECONNECT_EXHAUSTED
        # Start-up checks stay fail-fast: the token map and the universe are loaded, and the subscription
        # plan built, before the wait for the subscribe time, so a bad one stops the run now (M3).
        try:
            tokens = daemon._token_map(day)
            universe = loader.load_json("universe.json")
            manager = SubscriptionManager(day, universe, clock)
        except BaseException:
            daemon._release_ticket(ticker)
            raise
        # The day's landing file is created only once this run is going to record (I-13): a run that
        # ends here, stopped while waiting or started after the unsubscribe time, leaves nothing on disk.
        start_ns = timeutil.ist_ns(day, schedule["jobs"]["subscribe"])
        if clock.now_ns() < start_ns:
            log.info("waiting for the subscribe time %s IST", schedule["jobs"]["subscribe"])
            if not clock.sleep_until(start_ns, stop):
                daemon._release_ticket(ticker)
                return exitcodes.OK
        if clock.now_ns() >= timeutil.ist_ns(day, schedule["jobs"]["unsubscribe"]):
            log.info("the session is over for %s", day)
            daemon._release_ticket(ticker)
            return exitcodes.OK
        landing = LandingDB(day)
        manager.log_rows = landing.log_subscriptions
        call = live_call_in_ticker

    for warning in manager.warnings:
        log.warning("subscriptions: %s", warning)
    recorder = Recorder(landing, tokens.get, clock)
    manager.log_rows = recorder.log_subscriptions       # the writer thread stores them, never the ticker's (RM 5.5)
    session = RecorderSession(ticker, recorder, manager, clock, call_in_ticker=call)
    end_ns = timeutil.ist_ns(day, schedule["jobs"]["unsubscribe"])

    # The paper sessions: opened before the first frame, fed through the recorder's tap and the ticker's gap taps.
    pump = LogPump() if session_ids != [] else None       # the engines' log lines, off the engine thread
    pins = SessionPins(manager, cap=int(loader.load_json("kite_limits.json")["ws_max_instruments"]),
                       call_in_ticker=call, apply=lambda diff: session._apply(diff) if session.connected else None,
                       lock=session.sub_lock)
    sessions = open_sessions(choose_sessions(session_ids), day, clock, pump, pins) if pump is not None else []
    dispatcher = engine_thread = None
    engine_stop = threading.Event()
    engine_failed: list[str] = []
    if sessions:
        dispatcher = Dispatcher(sessions, FrameEncoder(tokens), clock=clock, cfg=loader.load_json("paper.json"),
                                replay=replay)
        recorder.add_tap(dispatcher)
        session.gap_taps.append(dispatcher.put_gap)
        dispatcher.on_book = pins.book_changed
        dispatcher.on_failed = pins.release
        pins.live = True                        # from here the ticker thread owns every pin change

        def engine_main() -> None:
            try:
                dispatcher.run(engine_stop)
            except BaseException as e:  # noqa: BLE001 — the recording goes on without it
                engine_failed.append(repr(e))
                log.critical("the engine thread died: %r; the recording continues, the sessions do not", e,
                             exc_info=True)

        engine_thread = threading.Thread(target=engine_main, name="engine", daemon=True)

    recorder.start()
    session.start()
    if engine_thread is not None:
        engine_thread.start()
    ticker.connect(threaded=True)
    log.info("recording %s: %d instruments, landing %s; %d paper session(s)", day, len(manager.current), landing.path,
             len(sessions))
    est = daemon.estimate_bytes_per_day(day, manager)
    log.info("expected landing size today: about %.0f MB (assumes about %d chain strikes once "
             "centred and one packet per instrument per second)", est["bytes"] / 1e6, est["chain_instruments"])
    last_power_check = 0
    bands = None
    if not replay:
        from stockify import kite_readonly
        from stockify.data import limits
        bands = limits.Snapshotter(day, manager, client=kite_readonly.client(), clock=clock,
                                   on_band=band_sink(dispatcher, sessions) if dispatcher is not None else None)
    stop_requested = False
    signals = StopSignals(stop)
    restore_signals = signals.install()
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
                    log.warning("running on battery during the session: recording continues until the battery "
                                "runs out (Windows then hibernates; the stop is recorded as a gap)")
            if replay and ticker.finished.is_set():
                break
            if clock.now_ns() >= end_ns:
                log.info("unsubscribe time reached")
                break
            stop.wait(0.05 if replay else 1.0)
        stop_requested = stop.is_set()
    except KeyboardInterrupt:
        log.info("interrupted")
        stop_requested = True
        stop.set()
    finally:
        try:                                    # the handlers stay until shutdown is over: a second Ctrl+C is ignored
            stuck = _shutdown(ticker=ticker, replay=replay, session=session, recorder=recorder, landing=landing,
                              clock=clock, day=day, sessions=sessions, dispatcher=dispatcher,
                              engine_thread=engine_thread, engine_stop=engine_stop, engine_failed=engine_failed,
                              stop_requested=stop_requested, signals=signals, pump=pump, skipped=pins.refused)
        finally:
            restore_signals()
    return exitcodes.ENGINE_STUCK if stuck else session.exit_code


def _shutdown(*, ticker, replay: bool, session, recorder, landing, clock, day: date, sessions: list,  # noqa: ANN001
              dispatcher, engine_thread, engine_stop: threading.Event, engine_failed: list[str],
              stop_requested: bool, signals: StopSignals, pump: LogPump | None, skipped: dict | None = None) -> bool:
    """The shutdown steps in the module doc. Returns True when the engine thread would not stop: then the main
    thread touches neither the dispatcher nor the journals nor the store (it would race the live thread, or hang
    on whatever the thread is stuck in), leaves the journals unfinalised for the next start to resume by replay,
    and still closes the landing file and writes the status file."""
    from stockify import daemon
    # 1. the ticker: whatever goes wrong here, the recorder below still stops and flushes
    try:
        if not session.stopped.is_set():
            session.end_session()
    except Exception:  # noqa: BLE001
        log.exception("ending the recorder session failed; stopping the recorder anyway")
    try:
        ticker.close()
        if replay:
            ticker.stop()
    except Exception as e:                      # the reactor may already be gone
        log.debug("ticker close: %s", e)
    daemon._release_ticket(ticker)
    # 2. both queues: the recorder's into the landing file, then the dispatcher's into the journals
    try:
        recorder.stop()
    except Exception:  # noqa: BLE001
        log.exception("stopping the recorder failed")
    signals.recorder_stopped()                  # only now may a third Ctrl+C force the exit
    try:
        session.tick()
    except Exception:  # noqa: BLE001
        log.exception("the last health tick failed")
    try:
        recorder.flush()                        # subscription rows logged since the writer stopped
    except Exception:  # noqa: BLE001
        log.exception("the last landing flush failed")
    stuck = False
    if engine_thread is not None:
        engine_stop.set()
        engine_thread.join(ENGINE_JOIN_S)
        stuck = engine_thread.is_alive()
        if stuck:
            log.error("the engine thread did not stop within %.0f s: not draining beside it, journals left "
                      "unfinalised (the next start resumes them by replay)", ENGINE_JOIN_S)
    if not stuck:
        if dispatcher is not None:
            try:
                dispatcher.drain()
            except Exception:  # noqa: BLE001 — the journals are closed and synced below regardless
                log.exception("draining the dispatcher failed")
        # 3. the journals, synced
        _each(sessions, "sync", lambda sd: sd.fsync())
        # 4. a stop inside market hours: the journal says it stopped seeing frames (never via the queue)
        if stop_requested and sessions and dispatcher is not None and inside_market_hours(clock.now_ns()):
            try:
                dispatcher.journal_process_stop(clock.now_ns())
                dispatcher.drain()
            except Exception:  # noqa: BLE001
                log.exception("journaling the process stop failed")
            _each(sessions, "sync", lambda sd: sd.fsync())
        # 5. closed, never finalised: that waits for NSE's settlement prices
        _each(sessions, "close", lambda sd: sd.close(finalise=False))
    if pump is not None:
        pump.close()
    stats = recorder.stats
    log.info("recorded %d packets in %d frames; %d heartbeats; dropped %d; exit %d",
             stats.packets, stats.frames, stats.heartbeats, stats.dropped_frames, session.exit_code)
    # 6. the status file
    alarms = list(stats.alarms) + (list(dispatcher.stats.alarms) if dispatcher is not None else [])
    alarms += ["engine_thread_died"] if engine_failed else []
    alarms += ["shutdown_engine_timeout"] if stuck else []
    code = exitcodes.ENGINE_STUCK if stuck else session.exit_code
    try:
        write_status(day, code, landing, clock, stop_requested=stop_requested, sessions=[] if stuck else sessions,
                     failed=dict(dispatcher.stats.failed) if dispatcher is not None else {}, alarms=alarms,
                     skipped=skipped)
    except Exception:  # noqa: BLE001
        log.exception("could not write %s", paths.daemon_status_file())
    try:
        landing.close()
    except Exception:  # noqa: BLE001
        log.exception("closing the landing file failed")
    return stuck


def _each(sessions: list, what: str, action: Callable) -> None:
    for sd in sessions:
        try:
            action(sd)
        except Exception:  # noqa: BLE001 — one session's trouble never stops the others
            log.exception("session %s: %s failed", sd.session_id, what)
