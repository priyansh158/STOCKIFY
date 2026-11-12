"""Notifications and the daily summary (RW 5.1; M5.8).

    notify.send("parity_failure", subject, body, key="s1:2026-11-17")   # -> None, or why it failed; never raises
    notify.post(...)                                                    # the same, from any thread: it only enqueues
    notify.daily_summary(day)                                           # -> (subject, body), or None; store only

`stockify/config/notify.json` names one channel (`null`, `email`, `ntfy`, `telegram`) and a switch per event. With no
channel, or the event switched off, a notification is only logged: the default. Secrets come from the environment
(`STOCKIFY_NTFY_TOPIC`, `STOCKIFY_TELEGRAM_TOKEN`, `STOCKIFY_TELEGRAM_CHAT`; email reuses the geo monitor's SMTP
settings through `geo_monitor.email_notify`) and are never logged: a failure logs the error's class only, because a
transport error often quotes the URL, and the URL holds the topic or the bot token.

A notifier failure never reaches the caller. The engine thread and the recorder's ticker thread call `post`, which
only puts the message on a bounded queue for a daemon thread. `key` makes a message once-only for the process: a
"still open" condition must not tell the owner again (callers pass a key, or do not call at all).
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import sys
import threading
import urllib.request
from datetime import date
from typing import Callable

from stockify import store
from stockify.config import loader

log = logging.getLogger("stockify.daemon.notify")

EVENTS = loader.NOTIFY_EVENTS
QUEUE_MAX = 100
HTTP_TIMEOUT_S = 10
NTFY_SERVER = "https://ntfy.sh"
TELEGRAM_SERVER = "https://api.telegram.org"
Sender = Callable[[str, str, str], None]          # (channel, subject, body); raises on failure

_lock = threading.Lock()
_seen: set[tuple[str, str]] = set()
_queue: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None
_pending = 0
_idle = threading.Condition(_lock)


def settings() -> dict:
    return loader.load_json("notify.json")


# ── delivery ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _blocked() -> bool:
    """The unit tests load `tests.support`; they must never reach a real mail server or the internet."""
    return "tests.support" in sys.modules


def _env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def _http_post(url: str, data: bytes, headers: dict) -> None:
    if _blocked():
        raise RuntimeError("real delivery is blocked under test")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")   # https only: the bases are fixed
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
        resp.read(1024)


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def _send_email(subject: str, body: str) -> None:
    if _blocked():
        raise RuntimeError("real delivery is blocked under test")
    from geo_monitor import email_notify
    if not email_notify.email_configured():
        raise RuntimeError("email is not configured")
    email_notify.send_email(subject, body)


def _default_sender(channel: str, subject: str, body: str) -> None:
    if channel == "email":
        _send_email(subject, body)
    elif channel == "ntfy":
        _http_post(f"{NTFY_SERVER}/{_env('STOCKIFY_NTFY_TOPIC')}", body.encode("utf-8"),
                   {"Title": _ascii(subject), "Content-Type": "text/plain; charset=utf-8"})
    elif channel == "telegram":
        token, chat = _env("STOCKIFY_TELEGRAM_TOKEN"), _env("STOCKIFY_TELEGRAM_CHAT")
        text = json.dumps({"chat_id": chat, "text": f"{subject}\n\n{body}"}).encode("utf-8")
        _http_post(f"{TELEGRAM_SERVER}/bot{token}/sendMessage", text, {"Content-Type": "application/json"})
    else:
        raise RuntimeError(f"unknown channel {channel!r}")


# ── sending ──────────────────────────────────────────────────────────────────────────────────────────────────────

def send(event: str, subject: str, body: str, *, sender: Sender | None = None, key: str | None = None,
         legacy_email: bool = False) -> str | None:
    """Tell the owner. None when it was sent or only logged (no channel, event off, or `key` already sent); else
    why it was not, as the error's class name (never its text: that can hold a secret). Never raises.

    `legacy_email` is for the login reminder, which has always gone by email and must not go quiet because no
    channel is chosen yet: with no channel and the event on, it uses email."""
    try:
        return _send(event, subject, body, sender, key, legacy_email)
    except Exception as e:  # noqa: BLE001 — a notifier failure never blocks the daemon
        log.error("notification %s failed: %s", event, type(e).__name__)
        return type(e).__name__


def _send(event, subject, body, sender, key, legacy_email):  # noqa: ANN001, ANN202
    if event not in EVENTS:
        log.error("unknown notification event %r (subject: %s)", event, subject)
        return "unknown event"
    cfg = settings()
    channel = cfg["channel"]
    if channel is None and legacy_email and cfg["events"][event]:
        channel = "email"
    if channel is None or not cfg["events"][event]:
        log.info("NOTIFY %s (log only): %s", event, subject)
        return None
    if key is not None:
        with _lock:
            if (event, key) in _seen:
                log.info("NOTIFY %s: already sent for %s", event, key)
                return None
    try:
        (sender or _default_sender)(channel, subject, body)
    except Exception as e:  # noqa: BLE001
        log.error("notification %s by %s failed: %s", event, channel, type(e).__name__)
        return type(e).__name__
    if key is not None:
        with _lock:
            _seen.add((event, key))
    log.info("NOTIFY %s sent by %s: %s", event, channel, subject)
    return None


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
            send(item["event"], item["subject"], item["body"], key=item["key"])
        except BaseException:  # noqa: BLE001 — the worker outlives any one message
            log.error("notification worker error")
        finally:
            with _idle:
                _pending -= 1
                _idle.notify_all()


def post(event: str, subject: str, body: str, *, key: str | None = None) -> bool:
    """`send` from any thread (the engine's, the ticker's): it only enqueues; a daemon thread delivers. False when
    the queue is full (the message is dropped and logged). Never raises, never blocks."""
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
        _queue.put({"event": event, "subject": subject, "body": body, "key": key})
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
    with _lock:
        _seen.clear()


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


def _session_lines(session: dict, day: str) -> list[str] | None:
    sid = session["session_id"]
    end = store.session_day_end(sid, day)
    equity = end["equity"]
    if equity is None:
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
    body = (f"What the paper-trading sessions did on {day:%A %d %B %Y}. No real orders were placed.\n\n"
            + "\n\n".join("\n".join(b) for b in blocks) + "\n\n- STOCKIFY daemon\n")
    return subject, body


def send_daily_summary(day: date, *, sender: Sender | None = None) -> bool:
    """Send `day`'s summary once. False when there is nothing to tell yet (a day not finalised)."""
    try:
        summary = daily_summary(day)
    except Exception as e:  # noqa: BLE001
        log.error("daily summary for %s failed: %s", day, type(e).__name__)
        return False
    if summary is None:
        return False
    send("daily_summary", *summary, sender=sender, key=day.isoformat())
    return True


# ── messages for the places that notify ──────────────────────────────────────────────────────────────────────────

def session_event(event: str, title: str, detail: dict, *, key: str | None = None) -> bool:
    """`post` a plain message about one session: `title`, then the detail's keys."""
    lines = [f"{k}: {v}" for k, v in detail.items()]
    return post(event, f"[STOCKIFY] {title}", f"{title}\n\n" + "\n".join(lines) + "\n\n- STOCKIFY daemon\n", key=key)


__all__ = ["EVENTS", "daily_summary", "flush", "post", "reset", "rupees", "send", "send_daily_summary",
           "send_test", "session_event", "settings"]
