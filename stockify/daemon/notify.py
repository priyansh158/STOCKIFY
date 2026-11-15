"""Notifications and the daily summary (RW 5.1; M5.8).

    notify.send("parity_failure", subject, body, key="s1:2026-11-17")   # -> None, or why it failed; never raises
    notify.post(...)                                                    # the same, from any thread: it only enqueues
    notify.daily_summary(day)                                           # -> (subject, body), or None; store only

`stockify/config/notify.json` names the channel (`email`, or `null` for log only) and a switch per event. With no
channel, or the event switched off, a notification is only logged. Email goes through the geo monitor's SMTP sender
(`geo_monitor.email_notify`; its settings come from the environment). Nothing is logged but an error's class: a
transport error can quote a host or an address. SMTP not set up is `NotConfigured`: logged once per process, and the
notification is skipped. The owner asked for email only (2026-11-11): there is no other channel.

`_blocked()` refuses real delivery whenever `tests.support` is in `sys.modules` (every unit test, and any dev tool
that imports it into a production process: it fails closed).

A notifier failure never reaches the caller. The engine thread and the recorder's ticker thread call `post`, which
only puts the message on a bounded queue for a daemon thread. `key` makes a message once-only for the process: a
"still open" condition must not tell the owner again (callers pass a key, or do not call at all).
"""
from __future__ import annotations

import atexit
import json
import logging
import queue
import sys
import threading
from datetime import date, timedelta
from typing import Callable

from stockify import store, timeutil
from stockify.config import loader

log = logging.getLogger("stockify.daemon.notify")

EVENTS = loader.NOTIFY_EVENTS
QUEUE_MAX = 100
MAX_BODY_CHARS = 20_000                           # a sane cap for one email; the log has the rest
CUT_NOTE = "... (cut: see the log)"
Sender = Callable[[str, str, str], None]          # (channel, subject, body); raises on failure

_lock = threading.Lock()
_seen: set[tuple[str, str]] = set()
_queue: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None
_pending = 0
_idle = threading.Condition(_lock)
_not_configured_noted = False


class NotConfigured(RuntimeError):
    """The channel cannot be used: SMTP is not set up. Its text is a fixed sentence, safe to log."""


def settings() -> dict:
    return loader.load_json("notify.json")


# ── delivery ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _blocked() -> bool:
    """The unit tests load `tests.support`; they must never reach a real mail server or the internet."""
    return "tests.support" in sys.modules


def fit(body: str) -> str:
    """`body` cut to MAX_BODY_CHARS (with a note) so one huge row cannot make the mail fail."""
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[:MAX_BODY_CHARS - len(CUT_NOTE)] + CUT_NOTE


def _send_email(subject: str, body: str) -> None:
    if _blocked():
        raise RuntimeError("real delivery is blocked under test")
    from geo_monitor import email_notify
    if not email_notify.email_configured():
        raise NotConfigured("email not configured")
    email_notify.send_email(subject, fit(body))


def _default_sender(channel: str, subject: str, body: str) -> None:
    if channel != "email":
        raise RuntimeError(f"unknown channel {channel!r}")
    _send_email(subject, body)


# ── sending ──────────────────────────────────────────────────────────────────────────────────────────────────────

def send(event: str, subject: str, body: str, *, sender: Sender | None = None, key: str | None = None) -> str | None:
    """Tell the owner. None when it was sent or only logged (no channel, event off, or `key` already sent); else
    why it was not, as the error's class name (never its text: that can hold a secret); "email not configured" when
    SMTP is not set up. Never raises."""
    try:
        return _send(event, subject, body, sender, key)
    except Exception as e:  # noqa: BLE001 — a notifier failure never blocks the daemon
        log.error("notification %s failed: %s", event, type(e).__name__)
        return type(e).__name__


def _send(event, subject, body, sender, key):  # noqa: ANN001, ANN202
    if event not in EVENTS:
        log.error("unknown notification event %r (subject: %s)", event, subject)
        return "unknown event"
    cfg = settings()
    channel = cfg["channel"]
    if channel is None or not cfg["events"][event]:
        log.info("NOTIFY %s (log only): %s", event, subject)
        return None
    if key is not None:
        with _lock:
            if (event, key) in _seen:
                log.info("NOTIFY %s: already sent for %s", event, key)
                return None
    try:
        (sender or _default_sender)(channel, subject, fit(body))
    except NotConfigured as e:
        _note_not_configured(event, e)
        return str(e)
    except Exception as e:  # noqa: BLE001
        log.error("notification %s by %s failed: %s", event, channel, type(e).__name__)
        return type(e).__name__
    if key is not None:
        with _lock:
            _seen.add((event, key))
    log.info("NOTIFY %s sent by %s: %s", event, channel, subject)
    return None


def _note_not_configured(event: str, e: NotConfigured) -> None:
    global _not_configured_noted
    with _lock:
        first, _not_configured_noted = not _not_configured_noted, True
    if first:
        log.warning("notifications are not sent: %s (NotConfigured); set the SMTP variables the geo monitor uses. "
                    "Seen on %s; reported once per process", e, event)


def send_test(*, sender: Sender | None = None) -> str | None:
    """The owner's check (`python -m stockify notify test`): one message through the chosen channel whatever the event
    switches say. None when it was sent; else why not (an error's class, or that no channel is chosen)."""
    try:
        channel = settings()["channel"]
        if channel is None:
            return "no channel is set in stockify/config/notify.json"
        (sender or _default_sender)(channel, "[STOCKIFY] Test notification",
                                    "If you can read this, STOCKIFY can reach you by " + channel + ".")
        return None
    except Exception as e:  # noqa: BLE001
        log.error("test notification failed: %s", type(e).__name__)
        return type(e).__name__


def _run() -> None:
    global _pending
    while True:
        item = _queue.get()
        try:
            result = send(item["event"], item["subject"], item["body"], key=item["key"])
            if item.get("on_done") is not None:
                item["on_done"](result)
        except BaseException:  # noqa: BLE001 — the worker outlives any one message
            log.error("notification worker error")
        finally:
            with _idle:
                _pending -= 1
                _idle.notify_all()


def post(event: str, subject: str, body: str, *, key: str | None = None, on_done: Callable | None = None) -> bool:
    """`send` from any thread (the engine's, the ticker's): it only enqueues; a daemon thread delivers. False when
    the queue is full (the message is dropped and logged). Never raises, never blocks. `on_done(error)` runs on the
    delivery thread with `send`'s result (None: sent or only logged)."""
    global _worker, _pending
    try:
        with _idle:
            if _pending >= QUEUE_MAX:
                log.error("notification queue full: %s dropped (%s)", event, subject)
                return False
            _pending += 1
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_run, name="stockify-notify", daemon=True)
                _worker.start()
        _queue.put({"event": event, "subject": subject, "body": body, "key": key, "on_done": on_done})
        return True
    except Exception as e:  # noqa: BLE001
        log.error("notification %s could not be queued: %s", event, type(e).__name__)
        return False


def flush(timeout: float = 10.0) -> bool:
    """Wait until every posted message was handled (True) or `timeout` seconds passed (False)."""
    with _idle:
        return _idle.wait_for(lambda: _pending == 0, timeout)


def reset() -> None:
    """Forget what was sent (tests, and a new daemon pass)."""
    flush(5)
    global _not_configured_noted
    with _lock:
        _seen.clear()
        _not_configured_noted = False


atexit.register(flush, 5.0)


# ── the daily summary ────────────────────────────────────────────────────────────────────────────────────────────

def rupees(paise: int) -> str:
    """₹ with Indian grouping (₹1,00,000); the paise only when there are some."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(int(paise)), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join([head, *groups, tail])
    return f"{sign}₹{digits}" + (f".{frac:02d}" if frac else "")


def _symbol(instrument_id: int) -> str:
    try:
        from stockify.data import instruments
        row = instruments.get(instrument_id)
        return str(row["tradingsymbol"]) if row else f"instrument {instrument_id}"
    except Exception:  # noqa: BLE001 — a name is a nicety
        return f"instrument {instrument_id}"


def _failed(sid: str, day: str) -> bool:
    """The session's day could not be finalised (`finalised_failed`), or it has a `session_failed` row from that IST day."""
    row = store.get_session_day(sid, day)
    if row is not None and row.get("status") == "finalised_failed":
        return True
    d = date.fromisoformat(day)
    lo, hi = timeutil.ist_ns(d, "00:00"), timeutil.ist_ns(d + timedelta(days=1), "00:00")
    return any(r["kind"] == "session_failed" and lo <= (r["wall_ns"] or 0) < hi
               for r in store.timeline(sid, limit=1_000_000))


def summary_ready(day: date) -> bool:
    """Every session that opened `day` has finalised it or failed it, and there is at least one: the summary then
    names them all (a day where every session failed is told too, as that)."""
    d = day.isoformat()
    done = waiting = 0
    for s in store.list_sessions():
        row = store.get_session_day(s["session_id"], d)
        if row is None:
            continue
        if row.get("status") == "finalised" or _failed(s["session_id"], d):
            done += 1
        else:
            waiting += 1
    return done > 0 and waiting == 0


def _every_session_failed(day: str) -> bool:
    rows = [(s["session_id"], store.get_session_day(s["session_id"], day)) for s in store.list_sessions()]
    rows = [(sid, row) for sid, row in rows if row is not None]
    return bool(rows) and all(row.get("status") != "finalised" and _failed(sid, day) for sid, row in rows)


def _session_lines(session: dict, day: str) -> list[str] | None:
    sid = session["session_id"]
    end = store.session_day_end(sid, day)
    equity = end["equity"]
    if equity is None:
        if _failed(sid, day):
            return [f"{session.get('name') or sid}: did not finish the day (the session failed); there is no result."]
        return None
    row = store.get_session_day(sid, day) or {}
    before = store.previous_equity(sid, day)
    start = before["nav_paise"] if before else int((session.get("config") or {}).get("capital_paise", equity["nav_paise"]))
    change = equity["nav_paise"] - start
    name = session.get("name") or sid
    verb = "made" if change > 0 else "lost" if change < 0 else "broke even"
    lines = [f"{name}: {verb} {rupees(abs(change))} today." if change else f"{name}: broke even today.",
             f"  The account is worth {rupees(equity['nav_paise'])}."]
    held = [p for p in end["positions"] if int(p.get("qty") or 0) != 0]
    if held:
        names = ", ".join(f"{_symbol(int(p['instrument_id']))} ({'long' if int(p['qty']) > 0 else 'short'} "
                          f"{abs(int(p['qty']))})" for p in held[:5])
        more = f" and {len(held) - 5} more" if len(held) > 5 else ""
        lines.append(f"  {len(held)} open position{'s' if len(held) != 1 else ''}: {names}{more}.")
    else:
        lines.append("  No open positions.")
    cov = row.get("coverage")
    lines.append(f"  Market data captured: {cov * 100:.1f}% of the session." if cov is not None
                 else "  Market data captured: not measured.")
    parity = row.get("parity")
    lines.append({"pass": "  Replay check: passed.", "fail": "  Replay check failed: the stored results disagree with "
                  "a replay of the journal. The session is paused."}.get(parity, "  Replay check: not run yet."))
    labels = json.loads(row.get("labels_json") or "[]")
    if labels:
        lines.append(f"  Notes: {', '.join(labels)}.")
    return lines


def daily_summary(day: date) -> tuple[str, str] | None:
    """(subject, body) for the sessions that finalised `day`, in plain words, or None when none did. Reads the store
    only."""
    d = day.isoformat()
    blocks = [lines for s in store.list_sessions() if (lines := _session_lines(s, d))]
    if not blocks:
        return None
    subject = f"[STOCKIFY] Paper trading summary for {day:%a %d %b %Y}"
    lead = "Every session failed on this day: there are no results.\n\n" if _every_session_failed(d) else ""
    body = (f"What the paper-trading sessions did on {day:%A %d %B %Y}. No real orders were placed.\n\n" + lead
            + "\n\n".join("\n".join(b) for b in blocks) + "\n\n- STOCKIFY daemon\n")
    return subject, body


def send_daily_summary(day: date, *, sender: Sender | None = None) -> bool:
    """Send `day`'s summary now (the CLI and tests; the daemon uses `post_daily_summary`). True only when it was
    sent or only logged: False when it is not ready or the send failed (it may be tried again)."""
    try:
        if not summary_ready(day):
            return False
        summary = daily_summary(day)
    except Exception as e:  # noqa: BLE001
        log.error("daily summary for %s failed: %s", day, type(e).__name__)
        return False
    if summary is None:
        return False
    return send("daily_summary", *summary, sender=sender, key=day.isoformat()) is None


def post_daily_summary(day: date, on_done: Callable[[str | None], None]) -> bool:
    """The daemon's way: build the summary (store reads) and `post` it; `on_done(error)` hears the outcome on the
    delivery thread. False when it is not ready or could not be queued."""
    try:
        if not summary_ready(day) or (summary := daily_summary(day)) is None:
            return False
    except Exception as e:  # noqa: BLE001
        log.error("daily summary for %s failed: %s", day, type(e).__name__)
        return False
    return post("daily_summary", *summary, key=day.isoformat(), on_done=on_done)


# ── messages for the places that notify ──────────────────────────────────────────────────────────────────────────

def session_event(event: str, title: str, detail: dict, *, key: str | None = None) -> bool:
    """`post` a plain message about one session: `title`, then the detail's keys."""
    lines = [f"{k}: {v}" for k, v in detail.items()]
    return post(event, f"[STOCKIFY] {title}", f"{title}\n\n" + "\n".join(lines) + "\n\n- STOCKIFY daemon\n", key=key)


def recorder_stall(group: str, name: str, since_ns: int) -> bool:
    """`post` that a FULL index went silent during the continuous session (RM 7.1). One message per episode: the key
    holds the index and the time of its last packet, so a stall that goes on is told once. Cheap, never raises."""
    try:
        since = timeutil.ns_to_ist(since_ns)
        subject = f"[STOCKIFY] The recorder stopped hearing {name}"
        body = (f"The recorder has had no market data for {name} ({group} session) since {since:%H:%M:%S} IST on "
                f"{since:%a %d %b %Y}, while the market is open.\n\n"
                "What to check: that the laptop is online and awake, that Kite is up, and the recorder's status on the "
                "Data coverage page. If the connection dropped, the recorder reconnects by itself; the missing time is "
                "recorded as a gap and is not made up.\n\n- STOCKIFY daemon\n")
        return post("recorder_stall", subject, body, key=f"{since.date().isoformat()}:{group}:{name}:{since_ns}")
    except Exception as e:  # noqa: BLE001 — a message must never reach the recorder's health loop
        log.error("stall notification could not be built: %s", type(e).__name__)
        return False


def disk_low(day: date, drive: str, warnings: list[str]) -> bool:
    """`post` that the lab's disk is running low (RM 7.1): one message per drive per day, naming every warning."""
    try:
        lines = "\n".join(f"- {w}" for w in warnings)
        body = (f"The disk that holds the lab data is running low:\n\n{lines}\n\n"
                "Recorded ticks are never deleted by themselves. Free some space (old backups, downloads, other "
                "files) or move the lab folder; the Data coverage page shows what the lab holds."
                "\n\n- STOCKIFY daemon\n")
        return post("disk_low", "[STOCKIFY] The disk is running low", body, key=f"{day.isoformat()}:{drive or 'lab'}")
    except Exception as e:  # noqa: BLE001
        log.error("disk notification could not be built: %s", type(e).__name__)
        return False


__all__ = ["EVENTS", "daily_summary", "disk_low", "fit", "flush", "NotConfigured", "post", "post_daily_summary", "recorder_stall",
           "reset", "rupees", "send", "send_daily_summary", "send_test", "session_event", "settings", "summary_ready"]
