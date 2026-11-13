"""The chart model of one paper-session day (CH 6.13): the bars the day's journal holds so far, a marker per paper fill
with the strategy's note, and the profit of the trade each closing fill ends.

Reads only: the day's journal and bundle files, and the session's own stored order, fill and charge rows
(`store.session_rows`, `store.session_days`). Nothing is written and nothing is repaired. A live day's journal is
being appended to, so a torn tail is simply not read (`journal.read` reports it and never removes it).

The bars are read incrementally: a process-level cache keyed by the journal's path holds the byte offset decoded so
far and the records decoded (only the kinds a bar is built from), so a poll decodes the new whole records and no
others. A journal shorter than the cached offset is a different file: the entry starts again. At most `CACHE_MAX`
journals are held, least recently used out.

Trade profit is FIFO over the whole session's fills up to the day, earlier days first, so a position carried
overnight closes with its real profit. Each day's engine numbers its own orders and fills, so ids are namespaced by
the day (day index << 32) while matching, and returned as the engine's own.

Units: prices and profit in paise, quantities in units, times in UTC epoch ns, `interval_s` in seconds.
"""
from __future__ import annotations

import json
import struct
import threading
from bisect import bisect_right
from collections import OrderedDict
from datetime import date

from stockify import paths, store
from stockify.daemon import journal
from stockify.data import packs
from stockify.reporting import chart, chartbars

CACHE_MAX = 8
_SHIFT = 32
_ROWS = 10_000_000                       # session_rows takes a limit; a session's rows are far fewer
_KEEP = frozenset({"BAR", "TICK_QUOTE", "TICK_FULL", "INDEX", "GAP"})     # the kinds a bar is built from
_LOCK = threading.Lock()
_CACHE: "OrderedDict[str, list]" = OrderedDict()                          # path -> [decoded byte offset, records]


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def cache_size() -> int:
    with _LOCK:
        return len(_CACHE)


def _journal_records(path) -> tuple[str, dict | None, list]:  # noqa: ANN001
    """(data_state, header, records): "no_pack" for a missing journal, "pack unreadable" for one that cannot be
    read (never an exception). A journal that cannot be read this moment but was read before (the writer is mid-
    append) answers with what was read before."""
    if not path.is_file():
        return "no_pack", None, []
    key = str(path)
    try:
        header, body, _torn = journal.read(path)
    except (journal.JournalError, OSError, struct.error):
        with _LOCK:
            ent = _CACHE.get(key)
            return ("ok", None, list(ent[1])) if ent else ("pack unreadable", None, [])
    with _LOCK:
        ent = _CACHE.get(key)
        if ent is None or len(body) < ent[0]:
            ent = _CACHE[key] = [0, []]
        _CACHE.move_to_end(key)
        try:
            off = ent[0]
            while off < len(body):
                rec, off = packs.decode(body, off)
                if rec["kind"] in _KEEP:
                    ent[1].append(rec)
                ent[0] = off
        except (packs.PackError, struct.error, KeyError, IndexError):
            _CACHE.pop(key, None)
            return "pack unreadable", header, []
        while len(_CACHE) > CACHE_MAX:
            _CACHE.popitem(last=False)
        return "ok", header, list(ent[1])


def _read_bundle(session_id: str, day: date) -> dict | None:
    try:
        doc = json.loads(paths.day_bundle_file(session_id, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _namespaced(session_id: str, days: list[str], upto: int) -> tuple[list[dict], list[dict], list[dict]]:
    """The orders, fills and charges of days[0..upto] with ids made unique across days. A fill's order is the one of
    its own day, else the newest earlier day that has that id (an order carried over)."""
    orders: list[dict] = []
    fills: list[dict] = []
    charges: list[dict] = []
    have: dict[int, int] = {}                        # order id -> the newest day index that holds it
    for n in range(upto + 1):
        day = days[n]
        o_rows = store.session_rows(session_id, "orders", trade_date=day, limit=_ROWS)
        f_rows = store.session_rows(session_id, "fills", trade_date=day, limit=_ROWS)
        c_rows = store.session_rows(session_id, "charges", trade_date=day, limit=_ROWS)
        mine = {int(o["order_id"]) for o in o_rows}
        for oid in mine:
            have[oid] = n

        def oday(oid, n=n, mine=mine) -> int:  # noqa: ANN001
            return n if oid in mine or oid not in have else have[oid]

        for o in o_rows:
            parent = o.get("parent_id")
            orders.append({**o, "order_id": (n << _SHIFT) | int(o["order_id"]),
                           "parent_id": None if parent in (None, "") else (oday(int(parent)) << _SHIFT) | int(parent)})
        for f in f_rows:
            fills.append({**f, "fill_id": (n << _SHIFT) | int(f["fill_id"]),
                          "order_id": (oday(int(f["order_id"])) << _SHIFT) | int(f["order_id"])})
        for c in c_rows:
            oid = c.get("order_id")
            charges.append({**c, "order_id": None if oid in (None, "") else (oday(int(oid)) << _SHIFT) | int(oid)})
    return orders, fills, charges


def session_chart(session_id: str, trade_date: str | None, instrument_id: int | None) -> dict:
    """The chart model of one instrument of one day of a paper session (the run chart's model plus `trade_date`,
    `days`, `paper` and `live`). A day with no journal is `data_state` "no_pack". An unknown session, day or
    instrument raises LookupError; a malformed date or id raises ValueError."""
    if instrument_id is not None:
        chart._int_arg("instrument_id", instrument_id)
    if trade_date is not None:
        try:
            if date.fromisoformat(trade_date).isoformat() != trade_date:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("trade_date must look like 2026-11-17") from None
    if not isinstance(session_id, str) or store.get_session(session_id) is None:
        raise LookupError("no such session")
    day_rows = store.session_days(session_id)
    days = [d["trade_date"] for d in day_rows]
    chosen = trade_date if trade_date is not None else (days[-1] if days else None)
    if chosen is None or chosen not in days:
        raise LookupError("no such day")
    idx = days.index(chosen)
    live = day_rows[idx].get("status") == "open"
    day = date.fromisoformat(chosen)

    orders, fills, charges = _namespaced(session_id, days, idx)
    everything, _skipped, _delivered, _idle = chart.markers_of(orders, fills, charges, [], _read_bundle(session_id, day))
    today = []
    for m in everything:
        if m["fill_id"] is not None and m["fill_id"] >> _SHIFT == idx:
            today.append({**m, "fill_id": m["fill_id"] & (2**_SHIFT - 1), "order_id": m["order_id"] & (2**_SHIFT - 1)})

    bundle = _read_bundle(session_id, day)
    inst = {i["id"]: i for i in (bundle or {}).get("instruments", []) if isinstance(i, dict) and "id" in i}
    traded = sorted({m["instrument_id"] for m in today})
    listed = sorted(inst) or traded
    instruments = [{"id": i, "symbol": (inst.get(i) or {}).get("tradingsymbol") or str(i),
                    "kind": chart._KINDS.get((inst.get(i) or {}).get("kind"))} for i in listed]
    if instrument_id is None:
        instrument_id = traded[0] if traded and traded[0] in listed else (listed[0] if listed else None)
    elif instrument_id not in listed:
        raise LookupError("no such instrument")
    mine = [m for m in today if m["instrument_id"] == instrument_id]

    state, header, records = _journal_records(paths.journal_file(session_id, day))
    bars, interval = [], chartbars.DEFAULT_INTERVAL_S
    if state == "ok" and instrument_id is not None:
        try:
            anchors = chartbars.anchors_from_bundle(bundle, instrument_id) if bundle else {}
        except (KeyError, StopIteration, TypeError):
            anchors = {}
        bars, _got = chartbars.bars_from_records(records, instrument_id, interval_s=None, anchors=anchors)
        interval = _got if _got is not None else chartbars.DEFAULT_INTERVAL_S
    truncated = len(bars) > chart.MAX_BARS
    shown = bars[-chart.MAX_BARS:] if truncated else bars

    omitted = 0
    if shown:
        starts = [b.start_ns for b in shown]
        out = []
        for m in mine:
            if m["t_ns"] < starts[0]:
                omitted += 1
                continue
            out.append({**m, "bar_start_ns": starts[bisect_right(starts, m["t_ns"]) - 1]})
    else:
        out = [{**m, "bar_start_ns": m["t_ns"] - m["t_ns"] % chart._MINUTE_NS} for m in mine]
    for m in out:
        del m["instrument_id"]

    return {"instruments": instruments, "instrument_id": instrument_id, "basis": (header or {}).get("price_basis"),
            "interval_s": interval, "bars": [{"start_ns": b.start_ns, "open": b.open, "high": b.high, "low": b.low,
                                              "close": b.close, "volume": b.volume} for b in shown],
            "markers": out, "omitted_markers": omitted, "plots": [], "truncated": truncated, "next_from_ns": None,
            "data_state": state, "notice": None, "pnl_complete": True, "skipped_settlements": 0,
            "idle_settlements": 0, "trade_date": chosen, "days": days, "paper": True, "live": live}
