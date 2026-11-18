"""FS 7: the upcoming results calendar: board meetings in the next weeks that consider financial results, for the
stocks in the current universe, from NSE's event calendar (`/api/event-calendar`).

The answer is a JSON list of {symbol, company, purpose, bm_desc, date "19-Nov-2026"} (fixture
tests/fixtures/fundamentals/event_calendar_2026-11-17.json). Without a range NSE lists only the next few days, so
refresh asks for `from_date` / `to_date` (DD-MM-YYYY, IST days). A meeting NSE no longer lists (moved, withdrawn)
is dropped from the table; meetings already past are kept as read.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from stockify import dbconn
from stockify.data import nse_files
from stockify.fundamentals import nse_api, store, universe

log = logging.getLogger("stockify.fundamentals.calendar")

PATH = "/api/event-calendar"
WARMUP = "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar"
WINDOW_DAYS = 28

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

# Results reported in a month: the quarter that ended before it (Indian financial year, April to March).
_PERIOD_BY_MONTH = {**dict.fromkeys((10, 11, 12), "Jul–Sep quarter"),
                    **dict.fromkeys((1, 2, 3), "Oct–Dec quarter"),
                    **dict.fromkeys((4, 5, 6), "Jan–Mar quarter (and the year)"),
                    **dict.fromkeys((7, 8, 9), "Apr–Jun quarter")}


def period_hint(meeting: date) -> str:
    return _PERIOD_BY_MONTH[meeting.month]


def _parse_date(text) -> date | None:  # noqa: ANN001
    """'19-Nov-2026' → date; None for anything else."""
    try:
        d, m, y = str(text).strip().split("-")
        return date(int(y), _MONTHS[m.lower()], int(d))
    except (ValueError, KeyError):
        return None


def parse(doc, symbols: set[str]) -> list[dict]:  # noqa: ANN001
    """The results meetings of `symbols` in NSE's answer: {symbol, meeting_date, purpose, period_hint}, one per
    (symbol, date), by date then symbol. Items of another purpose, other symbols or without a readable date drop out."""
    if not isinstance(doc, list):
        raise nse_files.NseError("the event calendar answer is not a list")
    out: dict[tuple[str, str], dict] = {}
    for item in doc:
        if not isinstance(item, dict):
            continue
        symbol, purpose = item.get("symbol"), item.get("purpose")
        if symbol not in symbols or not isinstance(purpose, str) or "result" not in purpose.lower():
            continue
        meeting = _parse_date(item.get("date"))
        if meeting is None:
            continue
        out[(symbol, meeting.isoformat())] = {"symbol": symbol, "meeting_date": meeting.isoformat(),
                                              "purpose": purpose.strip(), "period_hint": period_hint(meeting)}
    return [out[k] for k in sorted(out, key=lambda k: (k[1], k[0]))]


def refresh(day: date, *, client: nse_api.Client) -> int:
    """Read NSE's calendar for `day` .. `day` + 28 days and make the table's upcoming rows match it; returns how
    many results meetings of universe stocks are listed."""
    symbols = {u["symbol"] for u in universe.current()}
    params = {"index": "equities", "from_date": f"{day:%d-%m-%Y}",
              "to_date": f"{day + timedelta(days=WINDOW_DAYS):%d-%m-%Y}"}
    doc = client.json(PATH, params, warmup=WARMUP, name=f"{day:%Y%m%d}_equities_{params['from_date']}_{params['to_date']}.json")
    rows = parse(doc, symbols)
    start = day.isoformat()
    conn = store._connect()
    with dbconn.locked_write(store._lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM results_calendar WHERE meeting_date >= ?", (start,))
        conn.executemany(
            "INSERT OR REPLACE INTO results_calendar (symbol, meeting_date, purpose, period_hint, read_on) "
            "VALUES (?, ?, ?, ?, ?)",
            [(r["symbol"], r["meeting_date"], r["purpose"], r["period_hint"], start) for r in rows])
        conn.execute("COMMIT")
    log.info("fundamentals: results calendar %s: %d meetings of universe stocks", day, len(rows))
    return len(rows)


def upcoming(day: date, days: int = WINDOW_DAYS) -> list[dict]:
    """Meetings from `day` through `day` + `days`, by date then symbol."""
    rows = store._connect().execute(
        "SELECT symbol, meeting_date, purpose, period_hint, read_on FROM results_calendar "
        "WHERE meeting_date >= ? AND meeting_date <= ? ORDER BY meeting_date, symbol",
        (day.isoformat(), (day + timedelta(days=days)).isoformat())).fetchall()
    return [dict(r) for r in rows]
