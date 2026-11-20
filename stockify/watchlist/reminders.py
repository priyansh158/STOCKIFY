"""CW 3: results coming up for the watched stocks: one digest email an evening (the daemon's `watch_results` job).

The meetings come from the fundamentals results calendar (`stockify.fundamentals.calendar`, NSE's board-meeting
calendar), which the `fundamentals` job refreshes first for the universe and the watched stocks. A run for IST trade
date `day` covers meetings after `day` up to `results_lead_trading_days` trading days ahead, every calendar date in
between included: on a Friday with a lead of 2 that is Saturday to Tuesday, so a weekend meeting is told on the
Friday. A meeting already past when the run happens (a catch-up the next morning) is skipped.

Each meeting is told once: `store.record_alert(kind="results", ref=<meeting date>)` is the once-only record, so a moved
meeting (a new date) is told again and a restart tells nothing twice. A reminder whose email failed is tried again on the
job's next attempt.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Callable

from stockify import timeutil

log = logging.getLogger("stockify.watchlist.reminders")

GROUP = "CM"


def horizon(day: date, lead: int) -> date:
    """The `lead`-th trading day after `day`."""
    from stockify.data import calendar
    d = day
    for _ in range(lead):
        d = calendar.next_trading_day(GROUP, d)
    return d


def _send(event: str, subject: str, body: str, *, key: str) -> str | None:
    from stockify.daemon import notify
    return notify.send(event, subject, body, key=key)


def run(day: date, *, now_ns: int | None = None, send: Callable | None = None, cfg: dict | None = None) -> dict:
    """Tell the owner about the watched stocks' coming results meetings. Returns {meetings, told, error}: `told` the
    meetings in the email (none sent when there are none), `error` None when it was sent or nothing was due, else why
    the email failed (the rows stay `failed` and the next run tries them again)."""
    from stockify.config import loader
    from stockify.fundamentals import calendar as fcal
    from stockify.watchlist import store
    cfg = cfg or loader.load_json("watchlist.json")
    now_ns = timeutil.now_ns() if now_ns is None else now_ns
    send = send or _send
    today = max(day + timedelta(days=1), timeutil.ns_to_ist(now_ns).date())
    end = horizon(day, int(cfg["results_lead_trading_days"]))
    out = {"meetings": 0, "told": [], "error": None}
    watched = {r["symbol"]: int(r["instrument_id"]) for r in store.rows()}
    if not watched or end < today:
        return out
    meetings = [m for m in fcal.upcoming(today, (end - today).days) if m["symbol"] in watched]
    out["meetings"] = len(meetings)
    due: list[dict] = []
    for m in meetings:
        iid = watched[m["symbol"]]
        new = store.record_alert(iid, m["symbol"], "results", m["meeting_date"], at_ns=now_ns)
        if new or _waiting(store, iid, m["meeting_date"]):
            due.append({**m, "instrument_id": iid})
    if not due:
        return out
    subject, body = digest(due)
    error = send("watch_results", subject, body, key=f"results:{day.isoformat()}")
    for m in due:
        store.set_delivery(m["instrument_id"], "results", m["meeting_date"], "sent" if error is None else "failed")
    out["told"] = [(m["symbol"], m["meeting_date"]) for m in due]
    out["error"] = error
    if error is not None:
        log.warning("watchlist: the results reminder for %s was not sent (%s); tried again on the next attempt",
                    day, error)
    return out


def _waiting(store, iid: int, ref: str) -> bool:  # noqa: ANN001
    """True for a reminder recorded earlier whose email has not gone yet (pending, or failed last time)."""
    return any(a["instrument_id"] == iid and a["kind"] == "results" and a["ref"] == ref
               and a["delivery"] in ("pending", "failed") for a in store.alerts())


def digest(due: list[dict]) -> tuple[str, str]:
    names = ", ".join(dict.fromkeys(m["symbol"] for m in due))
    lines = []
    for m in due:
        d = date.fromisoformat(m["meeting_date"])
        lines.append(f"{m['symbol']}: board meeting on {d:%a %d %b %Y} for results ({m['period_hint']}); "
                     f"NSE lists it as \"{m['purpose']}\"")
    body = "\n".join(lines) + "\n\nFrom NSE's board-meeting calendar. A date can move; a moved date is told again."
    return f"[STOCKIFY] Results coming up: {names}", body
