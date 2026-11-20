"""CW 3: live 52-week high and low alerts for the watched stocks, judged beside the recorder (`HighLowWatch`).

Safety first: the recording must never depend on this. The parts and their threads:

    main thread     `HighLowWatch.build(day, tokens)` before the ticker connects: the watched stocks (watchlist.sqlite),
                    their Kite tokens (the day's token map, `daemon._token_map`) and their prior 52-week ranges from the
                    daily-candle cache (`ranges.prior_range(bars, day, ...)` with `day` the IST session date, so the
                    range ends the trading day before). Registered with `recorder.add_tap(watch)` before the connect.
    ticker thread   `watch.put_nowait(item)` is `deque(maxlen=tap_frames).append`: O(1), no lock, cannot raise. When
                    the evaluator falls behind, the oldest frames fall off; harmless, because a quote packet carries the
                    day's running high and low.
    watch-alerts    every POLL_S: walk each frame's packet headers, decode only the watched 44- and 184-byte packets
                    (`kite_packets.parse_packet`, unchanged), judge, record, batch, send.

Judging. Only frames received in the CM CONTINUOUS phase (`calendar.phase_at`), only positive values. A stock is
switched off for the day, with a log line, when its candles are stale (the last daily bar is not the previous trading
day: the nightly candle fetch did not run) or on a basis mismatch: the packet's close (yesterday's close) differs from
the cached last close by more than `basis_tolerance`, which points to a split or bonus ex-date (the cached range is on
the old basis). A new 52-week high is the day's high strictly above the prior high; a new low strictly below the prior
low. One alert per stock, side and day: `store.record_alert` is the once-only record (its unique key survives a
restart), and only a row this run inserted goes into a digest.

Delivery. One digest email per `alert_batch_s` through `notify.post("watch_high_low", ..., key="52w:<day>:<n>")`, at
most `max_alert_emails_per_day` a day; the last one says so and points to the Watchlist page, and later alerts are
stored as `capped`. The day's email count is kept in the lab (`watch_alerts_state.json`) so a restart cannot pass the
cap. Each alert row's delivery becomes `sent` or `failed` once notify has tried.

Containment. Each pass is in a try/except; after MAX_ERRORS errors the evaluator switches itself off for the day (the
tap goes on appending to its bounded deque, which nobody reads). Nothing here calls the recorder, the ticker or the
landing file. A removed stock stops alerting within VERSION_EVERY_S (the evaluator polls `store.version()`); a stock
added during the session is judged from the next session.
"""
from __future__ import annotations

import collections
import json
import logging
import struct
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from stockify import numerics, paths, timeutil
from stockify.daemon import kite_packets

log = logging.getLogger("stockify.watchlist.live")

POLL_S = 1.0                  # how often the evaluator reads the tap
VERSION_EVERY_S = 10          # how often it checks the list for removals (the spec allows 60 s)
MAX_ERRORS = 5                # errors in a day before the evaluator switches itself off
JOIN_S = 2.0                  # how long the shutdown waits for the evaluator's thread
WATCHED_LENGTHS = (44, 184)   # equity quote and full packets (kite_packets.LENGTHS)
GROUP = "CM"
PAGE = "/watchlist"
SEC = 1_000_000_000

_U16 = struct.Struct(">H")
_U32 = struct.Struct(">I")


@dataclass
class Watched:
    instrument_id: int
    symbol: str
    token: int
    high_paise: int
    high_date: str
    low_paise: int
    low_date: str
    last_close_paise: int
    basis_checked: bool = False
    off: str | None = None                       # why the stock is off for the day
    alerted: set[str] = field(default_factory=set)


def state_file():
    """The day's digest count, so a restart cannot pass the daily cap."""
    return paths.lab_dir() / "watch_alerts_state.json"


def _emails_sent(day: date) -> int:
    try:
        doc = json.loads(state_file().read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("day") == day.isoformat() and isinstance(doc.get("emails"), int):
            return max(0, doc["emails"])
    except (OSError, ValueError):
        pass
    return 0


def _write_emails_sent(day: date, n: int) -> None:
    p = state_file()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"day": day.isoformat(), "emails": n}), encoding="utf-8")
    tmp.replace(p)


def _default_post(event: str, subject: str, body: str, *, key: str, on_done: Callable) -> bool:
    from stockify.daemon import notify
    return notify.post(event, subject, body, key=key, on_done=on_done)


def _default_bars(instrument_id: int) -> list[dict]:
    from stockify.data import history
    return history.bars(instrument_id, "day")


class HighLowWatch:
    def __init__(self, day: date, watched: dict[int, Watched], *, cfg: dict, clock, version: int,  # noqa: ANN001
                 post: Callable | None = None, phase_at: Callable | None = None) -> None:
        self.day = day
        self.watched = watched                   # token -> Watched; only the evaluator's thread changes it
        self.clock = clock
        self.batch_ns = int(cfg["alert_batch_s"]) * SEC
        self.cap = int(cfg["max_alert_emails_per_day"])
        self.tol_ppm = numerics.to_ppm(str(cfg["basis_tolerance"]))
        self.frames: collections.deque = collections.deque(maxlen=int(cfg["tap_frames"]))
        self.put_nowait = self.frames.append     # the recorder's tap (`Recorder.add_tap`): cannot raise
        self.post = post or _default_post
        if phase_at is None:
            from stockify.data import calendar
            phase_at = calendar.phase_at
        self.phase_at = phase_at
        self.version = version
        self.emails = _emails_sent(day)
        self.batch: list[dict] = []
        self.batch_started: int | None = None
        self.errors = 0
        self.disabled = False
        self._next_version_ns = clock.mono_ns() + VERSION_EVERY_S * SEC
        self._phase_cache: tuple[int, bool] = (-1, False)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── build (main thread, before the connect) ─────────────────────────────────────────────────────────────────────
    @classmethod
    def build(cls, day: date, tokens: dict[int, int], *, clock, cfg: dict | None = None,  # noqa: ANN001
              bars_of: Callable[[int], list[dict]] | None = None, **kw) -> "HighLowWatch | None":
        """The evaluator for `day`, or None when nothing is watched or no watched stock can be judged today.
        `tokens` is the day's token map (Kite token -> instrument id). Raises on a broken store: the caller logs it
        and records without alerts."""
        from stockify.config import loader
        from stockify.data import calendar
        from stockify.watchlist import ranges, store
        cfg = cfg or loader.load_json("watchlist.json")
        bars_of = bars_of or _default_bars
        version = store.version()
        rows = store.rows()[:int(cfg["max_stocks"])]
        if not rows:
            return None
        token_of = {iid: tok for tok, iid in tokens.items()}
        previous = calendar.previous_trading_day(GROUP, day).isoformat()
        watched: dict[int, Watched] = {}
        for r in rows:
            iid, sym = int(r["instrument_id"]), str(r["symbol"])
            token = token_of.get(iid)
            if token is None:
                log.warning("watchlist: %s has no Kite token on %s; no alerts for it today", sym, day)
                continue
            rng = ranges.prior_range(bars_of(iid), day, int(cfg["lookback_days"]), int(cfg["min_history_days"]))
            if rng is None:
                log.info("watchlist: %s has less than a year of daily prices; no 52-week alerts for it yet", sym)
                continue
            if rng["last_close_date"] != previous:
                log.warning("watchlist: %s's daily prices end on %s, not on %s (the nightly candle fetch did not run "
                            "for it): no 52-week alerts for it today", sym, rng["last_close_date"], previous)
                continue
            watched[token] = Watched(iid, sym, token, rng["high_paise"], rng["high_date"], rng["low_paise"],
                                     rng["low_date"], rng["last_close_paise"])
        if not watched:
            log.info("watchlist: no watched stock can be judged today; no 52-week alerts")
            return None
        log.info("watchlist: 52-week alerts on %d stock(s) today", len(watched))
        return cls(day, watched, cfg=cfg, clock=clock, version=version, **kw)

    # ── the thread ──────────────────────────────────────────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="watch-alerts", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.wait(POLL_S) and not self.disabled:
                self.step()
        except BaseException:  # noqa: BLE001 — nothing may leave this thread
            log.critical("watchlist: the alert thread stopped", exc_info=True)

    def stop(self, timeout: float = JOIN_S) -> None:
        """Stop the thread (a short join: it is a daemon thread), then read what is left and send the waiting digest."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("watchlist: the alert thread did not stop within %.0f s; left behind", timeout)
                return
        self.step(final=True)

    # ── one pass ────────────────────────────────────────────────────────────────────────────────────────────────────
    def step(self, *, final: bool = False) -> None:
        """Read the tap, judge, and send a digest that is due (every waiting one when `final`). Never raises."""
        if self.disabled:
            return
        try:
            self._poll_version()
            while True:
                try:
                    item = self.frames.popleft()
                except IndexError:
                    break
                self._frame(item)
            if self.batch and (final or self.clock.mono_ns() - self.batch_started >= self.batch_ns):
                self._send()
        except Exception:  # noqa: BLE001 — never the recorder's problem
            self.errors += 1
            log.error("watchlist: the alert pass failed (%d of %d)", self.errors, MAX_ERRORS, exc_info=True)
            if self.errors >= MAX_ERRORS:
                self.disabled = True
                self.frames.clear()
                log.error("watchlist: 52-week alerts are off for the rest of %s after %d errors; the recording is "
                          "not affected", self.day, self.errors)

    def _poll_version(self) -> None:
        now = self.clock.mono_ns()
        if now < self._next_version_ns:
            return
        self._next_version_ns = now + VERSION_EVERY_S * SEC
        from stockify.watchlist import store
        v = store.version()
        if v == self.version:
            return
        self.version = v
        ids = set(store.ids())
        for token, w in list(self.watched.items()):
            if w.instrument_id not in ids:
                del self.watched[token]
                log.info("watchlist: %s was removed from the list; no more alerts for it", w.symbol)

    def _continuous(self, recv_ns: int) -> bool:
        second = recv_ns // SEC
        if self._phase_cache[0] != second:
            self._phase_cache = (second, self.phase_at(GROUP, recv_ns) == "CONTINUOUS")
        return self._phase_cache[1]

    def _frame(self, item: tuple) -> None:
        _frame_no, recv_ns, _mono, payload, is_binary = item
        if not is_binary or len(payload) < 2 or not self.watched or not self._continuous(recv_ns):
            return
        n = len(payload)
        (count,) = _U16.unpack_from(payload, 0)
        pos = 2
        for _ in range(count):                  # the headers only; a frame that does not add up just ends the walk
            if pos + 2 > n:
                return
            (length,) = _U16.unpack_from(payload, pos)
            pos += 2
            if pos + length > n:
                return
            if length in WATCHED_LENGTHS:
                w = self.watched.get(_U32.unpack_from(payload, pos)[0])
                if w is not None and w.off is None:
                    self._judge(w, kite_packets.parse_packet(bytes(payload[pos:pos + length])), recv_ns)
            pos += length

    def _judge(self, w: Watched, p: dict, recv_ns: int) -> None:
        high, low, close = p["high"] or 0, p["low"] or 0, p["close"] or 0
        if not w.basis_checked:
            if close <= 0:
                return
            if abs(close - w.last_close_paise) * numerics.PPM > self.tol_ppm * w.last_close_paise:
                w.off = "basis"
                log.warning("watchlist: %s's close in the live feed (%d paise) differs from the cached close (%d "
                            "paise) by more than the tolerance (a split or bonus?): no 52-week alerts for it today",
                            w.symbol, close, w.last_close_paise)
                return
            w.basis_checked = True
        if high > 0 and high > w.high_paise and "high52" not in w.alerted:
            self._cross(w, "high52", high, w.high_paise, w.high_date, recv_ns)
        if low > 0 and low < w.low_paise and "low52" not in w.alerted:
            self._cross(w, "low52", low, w.low_paise, w.low_date, recv_ns)

    def _cross(self, w: Watched, kind: str, level: int, prior: int, prior_date: str, recv_ns: int) -> None:
        from stockify.watchlist import store
        w.alerted.add(kind)
        capped = self.emails >= self.cap
        inserted = store.record_alert(w.instrument_id, w.symbol, kind, self.day.isoformat(), level_paise=level,
                                      prior_paise=prior, prior_date=prior_date, at_ns=recv_ns,
                                      delivery="capped" if capped else "pending")
        if not inserted:
            return                              # already recorded today (a restart): never told twice
        log.info("watchlist: %s made a new 52-week %s (%d paise, above/below %d of %s)%s", w.symbol,
                 "high" if kind == "high52" else "low", level, prior, prior_date,
                 "; today's alert emails are used up, so it is on the page only" if capped else "")
        if capped:
            return
        if not self.batch:
            self.batch_started = self.clock.mono_ns()
        self.batch.append({"instrument_id": w.instrument_id, "symbol": w.symbol, "kind": kind, "level": level,
                           "prior": prior, "prior_date": prior_date, "at_ns": recv_ns})

    # ── delivery ────────────────────────────────────────────────────────────────────────────────────────────────────
    def _send(self) -> None:
        items, self.batch, self.batch_started = self.batch, [], None
        n = self.emails + 1
        self.emails = n
        try:
            _write_emails_sent(self.day, n)
        except OSError:
            log.warning("watchlist: the day's email count could not be saved", exc_info=True)
        subject, body = digest(items, last=n >= self.cap, cap=self.cap)
        posted = self.post("watch_high_low", subject, body, key=f"52w:{self.day.isoformat()}:{n}",
                           on_done=lambda error: self._delivered(items, error))
        if not posted:
            self._delivered(items, "queue full")

    def _delivered(self, items: list[dict], error) -> None:  # noqa: ANN001
        """On notify's thread: each alert row says how it went."""
        from stockify.watchlist import store
        for a in items:
            try:
                store.set_delivery(a["instrument_id"], a["kind"], self.day.isoformat(),
                                   "sent" if error is None else "failed")
            except Exception:  # noqa: BLE001
                log.warning("watchlist: the delivery of %s's alert was not stored", a["symbol"], exc_info=True)


def digest(items: list[dict], *, last: bool, cap: int) -> tuple[str, str]:
    """(subject, body) of one digest email: a line per alert, in the order they were found."""
    from stockify.daemon.notify import rupees
    names = ", ".join(dict.fromkeys(a["symbol"] for a in items))
    lines = []
    for a in items:
        when = timeutil.ns_to_ist(a["at_ns"])
        if a["kind"] == "high52":
            what = f"a new 52-week high of {rupees(a['level'])}, above the {rupees(a['prior'])} of {a['prior_date']}"
        else:
            what = f"a new 52-week low of {rupees(a['level'])}, below the {rupees(a['prior'])} of {a['prior_date']}"
        lines.append(f"{a['symbol']}: {what} (seen at {when:%H:%M} IST)")
    body = "\n".join(lines) + ("\n\nThe range is the last 52 weeks of daily prices, up to yesterday's close. "
                               "Each stock is told once per side per day.")
    if last:
        body += (f"\n\nThis is the last of today's {cap} alert emails. Later alerts today are on the Watchlist page "
                 f"({PAGE}) only.")
    return f"[STOCKIFY] New 52-week high or low: {names}", body
