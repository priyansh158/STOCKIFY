"""The chart model of one paper-session day (CH 6.13): the bars the day's journal holds so far, a marker per paper fill
with the strategy's note, and the profit of the trade each closing fill ends.

Reads only: the day's journal and bundle files, and the session's own stored order, fill and charge rows
(`store.session_rows`, `store.session_days`). Nothing is written, nothing is created (a missing folder is "no
journal", and the paths used are the reader's, `paths.journal_path_ro`) and nothing is repaired. A live day's journal
is being appended to, so a torn tail is simply not read (`journal.read_from` reports it and never removes it).

The bars are folded incrementally. A process-level cache keyed by (journal path, instrument) holds the journal's
`ReadState` (byte offset, and the probes that tell a replaced file from a grown one) and the instrument's folded bars
(`chartbars.BarFold`, a few hundred bars, never the records), so a poll reads the new bytes, decodes the new
records once and folds them. At most `CACHE_MAX` entries are held, least recently used out. The lock guards only the
swap of an entry; reading and decoding happen outside it, on a copy of the cached fold, so a slow read never blocks
another session's poll.

Trade profit is FIFO over the whole session's fills up to the day, earlier days first, so a position carried
overnight closes with its real profit. Each day's engine numbers its own orders and fills, so ids are namespaced by
the day (day index << 32) while matching, and returned as the engine's own.

A position still open at the end of the day in a contract that has expired ended by expiry settlement, which a
session's rows do not carry: the chart says so and `pnl_complete` is false.

Units: prices and profit in paise, quantities in units, times in UTC epoch ns, `interval_s` in seconds.
"""
from __future__ import annotations

import json
import struct
import threading
from bisect import bisect_right
from collections import OrderedDict
from datetime import date
from typing import NamedTuple

from stockify import paths, store
from stockify.daemon import journal
from stockify.reporting import chart, chartbars

CACHE_MAX = 8
_SHIFT = 32
_ROWS = 10_000_000                       # session_rows takes a limit; a session's rows are far fewer
_LOCK = threading.Lock()


class _Entry(NamedTuple):
    state: journal.ReadState
    fold: chartbars.BarFold
    basis: str | None


_CACHE: "OrderedDict[tuple[str, int], _Entry]" = OrderedDict()


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def cache_size() -> int:
    with _LOCK:
        return len(_CACHE)


def _journal_bars(path, instrument_id: int, anchors: dict) -> tuple[str, str | None, list, int]:  # noqa: ANN001
    """(data_state, price basis, bars, interval_s) from the journal at `path`: "no_pack" for a missing journal,
    "pack unreadable" for one that cannot be read (never an exception). A journal that cannot be read this moment but
    was read before (the writer is mid-append) answers with what was read before."""
    if not path.is_file():
        return "no_pack", None, [], chartbars.DEFAULT_INTERVAL_S
    key = (str(path), instrument_id)
    with _LOCK:
        ent = _CACHE.get(key)
    if ent is not None and ent.fold.anchors != anchors:
        ent = None
    box = {"fold": ent.fold.copy() if ent else chartbars.BarFold(instrument_id, interval_s=None, anchors=anchors)}

    def restart() -> None:
        box["fold"] = chartbars.BarFold(instrument_id, interval_s=None, anchors=anchors)

    try:
        header, state, _torn, _restarted = journal.read_from(path, ent.state if ent else None,
                                                             lambda rec: box["fold"].feed_one(rec), on_restart=restart)
    except (journal.JournalError, OSError, struct.error):
        if ent is None:
            return "pack unreadable", None, [], chartbars.DEFAULT_INTERVAL_S
        bars, got = ent.fold.result()
        return "ok", ent.basis, bars, got if got is not None else chartbars.DEFAULT_INTERVAL_S
    new = _Entry(state, box["fold"], header["price_basis"])
    with _LOCK:
        cur = _CACHE.get(key)
        if cur is None or cur.state.ident != state.ident or cur.state.offset <= state.offset:
            _CACHE[key] = new
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_MAX:
            _CACHE.popitem(last=False)
    bars, got = new.fold.result()
    return "ok", new.basis, bars, got if got is not None else chartbars.DEFAULT_INTERVAL_S


def _read_bundle(session_id: str, day: date) -> dict | None:
    try:
        doc = json.loads(paths.day_bundle_path_ro(session_id, day).read_text(encoding="utf-8"))
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


def _expired_open(orders: list[dict], fills: list[dict], instrument_id: int, chosen: str, rows_of) -> int:  # noqa: ANN001
    """How many (contract, product) positions of `instrument_id` are still open after the session's fills up to the
    end of `chosen` although the contract has expired by then: they ended by expiry settlement. `rows_of()` gives the
    instrument rows to look the expiry up in (the chosen day's bundle, then earlier days')."""
    product = {o["order_id"]: o.get("product") or "" for o in orders}
    net: dict[str, int] = {}
    for f in fills:
        if f["instrument_id"] != instrument_id:
            continue
        key = product.get(f["order_id"], "")
        net[key] = net.get(key, 0) + (f["qty"] if f["side"] == "BUY" else -f["qty"])
    if not any(net.values()):
        return 0
    row = next((r for rows in rows_of() for r in rows if r.get("id") == instrument_id), None)
    expiry = (row or {}).get("actual_expiry") or (row or {}).get("expiry")
    if not isinstance(expiry, str) or expiry > chosen:
        return 0
    return sum(1 for v in net.values() if v)


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
    try:
        jpath = paths.journal_path_ro(session_id, day)
        bundle = _read_bundle(session_id, day)
    except ValueError:
        raise LookupError("no such session") from None

    orders, fills, charges = _namespaced(session_id, days, idx)
    everything, _skipped, _delivered, _idle = chart.markers_of(orders, fills, charges, [], bundle)
    today = []
    for m in everything:
        if m["fill_id"] is not None and m["fill_id"] >> _SHIFT == idx:
            today.append({**m, "fill_id": m["fill_id"] & (2**_SHIFT - 1), "order_id": m["order_id"] & (2**_SHIFT - 1)})

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

    def instrument_rows():  # the chosen day's bundle first, then each earlier day's, newest first
        yield list(inst.values())
        for earlier in reversed(days[:idx]):
            doc = _read_bundle(session_id, date.fromisoformat(earlier))
            yield [i for i in (doc or {}).get("instruments", []) if isinstance(i, dict)]

    state, basis, bars, interval = "no_pack", None, [], chartbars.DEFAULT_INTERVAL_S
    if instrument_id is not None:
        try:
            anchors = chartbars.anchors_from_bundle(bundle, instrument_id) if bundle else {}
        except (KeyError, StopIteration, TypeError):
            anchors = {}
        state, basis, bars, interval = _journal_bars(jpath, instrument_id, anchors)
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

    ended = _expired_open(orders, fills, instrument_id, chosen, instrument_rows) if instrument_id is not None else 0
    notice = (f"{ended} position{'s' if ended > 1 else ''} in this contract ended by expiry settlement, which this chart "
              "does not show; its profit is incomplete.") if ended else None

    return {"instruments": instruments, "instrument_id": instrument_id, "basis": basis,
            "interval_s": interval, "bars": [{"start_ns": b.start_ns, "open": b.open, "high": b.high, "low": b.low,
                                              "close": b.close, "volume": b.volume} for b in shown],
            "markers": out, "omitted_markers": omitted, "plots": [], "truncated": truncated, "next_from_ns": None,
            "data_state": state, "notice": notice, "pnl_complete": not ended, "skipped_settlements": ended,
            "idle_settlements": 0, "trade_date": chosen, "days": days, "paper": True, "live": live}
