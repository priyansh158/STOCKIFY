"""Daily Kite login reminder (D-32, K-7).

The Kite token dies at 06:00 IST and the only login is a browser flow, so on
each trading day the daemon emails the owner at the times in
`schedule.json` → `login_reminders_ist` (06:05 and 08:00 IST; 16:35 and 18:30
in Seattle) while the token is still stale. Each reminder is sent at most once
per trade date (scheduler state). Email uses the geo monitor's SMTP settings;
without them the reminder is only logged.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Callable

from stockify import timeutil
from stockify.data import calendar

log = logging.getLogger("stockify.daemon.reminder")
LOGIN_URL = "http://127.0.0.1:3000/portfolio"


def reminder_times(day: date, cfg: dict) -> list[tuple[str, int]]:
    return sorted((hhmm, timeutil.ist_ns(day, hhmm)) for hhmm in cfg.get("login_reminders_ist", []))


def message(day: date, cfg: dict) -> tuple[str, str]:
    local = calendar.owner_tz()
    pre_open = timeutil.ist_ns(day, cfg["kite_login_window_ist"]["until"])
    deadline = timeutil.ns_to_ist(pre_open).astimezone(local)
    subject = f"[STOCKIFY] Kite login needed for the {day:%a %d %b} session"
    body = (f"The STOCKIFY recorder has no fresh Kite login for the NSE session of {day:%A %d %B %Y} (IST).\n\n"
            f"Log in before {deadline:%H:%M %Z} ({cfg['kite_login_window_ist']['until']} IST): open {LOGIN_URL} "
            "and click 'Login to Kite'.\n\nWithout a login nothing is recorded today.\n\n— STOCKIFY daemon\n")
    return subject, body


def send_default(subject: str, body: str) -> str | None:
    from geo_monitor import email_notify
    if not email_notify.email_configured():
        log.warning("login reminder (email not configured): %s", subject)
        return "email not configured"
    try:
        email_notify.send_email(subject, body)
        return None
    except Exception as e:
        log.error("login reminder email failed: %s", e)
        return str(e)


def wait_with_reminders(clock, stop, until_ns: int, day: date, state, cfg: dict, *,  # noqa: ANN001
                        token_fresh: Callable[[], bool], send: Callable[[str, str], str | None] = send_default) -> bool:
    """Sleep until `until_ns`, sending each due reminder while the token is stale.

    Returns False if `stop` was set while waiting.
    """
    for hhmm, at_ns in reminder_times(day, cfg):
        if at_ns >= until_ns or state.done(f"login_reminder_{hhmm}", day):
            continue
        if not clock.sleep_until(at_ns, stop):
            return False
        if token_fresh():
            log.info("Kite login is fresh; no reminder needed")
            break
        error = send(*message(day, cfg))
        state.mark(f"login_reminder_{hhmm}", day)
        if error is None:
            log.info("login reminder sent (%s IST)", hhmm)
    return clock.sleep_until(until_ns, stop)
