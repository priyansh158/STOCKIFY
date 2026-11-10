"""A paper session's start-of-day state (A1 §6.4, A4 §1.10, D-66).

    text = engine.eod_state()          # after a day's run: canonical JSON
    Engine(day_bundle_with_sod_state)  # the next day: the constructor applies bundle["sod_state"]

Everything one day hands to the next, so that a run split into one-day bundles writes exactly what the whole run
writes. Money in paise, quantities in units, times in UTC ns: integers only; floats appear only inside `strategy`
(whatever the strategy's `save_state()` returns). The bar history is the full history, never trimmed (C-2):
indicators run over every bar. Per-day metrics (rejects, participation, exposure, labels) are not part of it.

The C++ engine writes the same bytes (VOL 5.3), so every field below is described exactly in A1 §6.4.
"""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import date as _date_cls

from stockify import numerics
from stockify.oracle.ledger import Ledger, Position
from stockify.oracle.orders import FINAL, LIVE, PRODUCTS, REASONS, SIDES, TYPES, VALIDITIES, Order

SCHEMA = "stockify.sod_state/1"
IST_OFFSET_S = 19_800
_EPOCH_ORDINAL = 719_163                    # date(1970, 1, 1).toordinal()

# The top-level keys (A1 §6.4 lists them in this order; the JSON is canonical, so they serialise sorted). Checked by
# blockwatch against the A1 table.
# <block name="sod-state-keys" same-as="docs/plan/A1_schemas.md:sod-state-keys" same-as-mode="sequence" same-as-pattern='"(?P<value>[a-z_]+)"'>
KEYS = ("schema", "trade_date", "cash_paise", "ledger", "pending_orders", "final_orders", "dp_done", "timers",
        "built_bars", "counters", "rng", "subscriptions", "strategy_enabled", "pause_reasons", "queued_notifications",
        "fsp_seen", "market", "bar_history", "strategy")
# </block>

# The field lists below are tied by blockwatch to the C++ reader's (engine/src/sod_state.cpp): change both together.
# <block name="sod-state-position-fields" same-as-pattern='"(?P<value>[a-z_]+)"'>
POSITION_FIELDS = ("instrument_id", "product", "style", "q", "d", "c", "realised", "vm", "last_mark", "mark_source")
# </block>
# <block name="sod-state-order-fields" same-as-pattern='"(?P<value>[a-z_]+)"'>
ORDER_FIELDS = ("order_id", "parent_id", "instrument_id", "side", "qty", "product", "type", "price", "trigger",
                "validity", "protection", "tag", "t_submit", "t_activate", "status", "filled_qty", "fill_value",
                "reason", "limit", "from_market", "triggered", "protection_bp", "reserved", "first_eligible_done",
                "trade_date", "charged", "eod_after")
# </block>
# <block name="sod-state-counters" same-as-pattern='"(?P<value>[a-z_]+)"'>
COUNTERS = ("next_order", "next_fill", "next_timer", "charge_row")
# </block>
# <block name="sod-state-market-fields" same-as-pattern='"(?P<value>[a-z_]+)"'>
MARKET_FIELDS = ("ltp", "t", "s0", "s1", "bid", "ask", "book", "band", "oi", "min_interval", "day")
# </block>
# <block name="sod-state-snapshot-fields" same-as-pattern='"(?P<value>[a-z_]+)"'>
SNAPSHOT_FIELDS = ("t", "ltp", "volume", "atp", "high", "low")
# </block>


class StateError(ValueError):
    pass


# ── Capture ──────────────────────────────────────────────────────────────────

def _order(o: Order) -> dict:
    return {f: getattr(o, f) for f in ORDER_FIELDS}


def _pending_internal(engine, kind: str) -> list:  # noqa: ANN001
    """The internal events of `kind` still to come (left in the queue, or passed over after the last DAY_END), in
    the order they would fire: by key (time, rank, creation order)."""
    items = [x for x in list(engine.internal) + list(engine.carried) if x[1] == kind]
    return sorted(items, key=lambda x: x[0])


def _timers(engine) -> list:  # noqa: ANN001
    out, seen = [], set()
    for key, _, tid in _pending_internal(engine, "TIMER"):
        spec = engine.timers.get(tid)
        if spec is None or spec[0] != key[0] or tid in seen:
            continue                                   # cancelled (its queue entry is stale)
        seen.add(tid)
        out.append([tid, spec[0], spec[1]])
    missing = sorted(set(engine.timers) - seen)
    if missing:
        raise StateError(f"timers {missing} have no queued event")
    return out


def _built(engine) -> list:  # noqa: ANN001
    out = []
    for _, _, (iid, isec, start) in _pending_internal(engine, "BAR_BUILT"):
        b = engine.built.get((iid, isec), {}).get(start)
        if b is not None:
            out.append([iid, isec, start, *b])
    return out


def _market(m) -> dict:  # noqa: ANN001
    out = {}
    for f in MARKET_FIELDS:
        v = getattr(m, f)
        if f in ("s0", "s1") and v is not None:
            v = {k: v[k] for k in SNAPSHOT_FIELDS}
        elif f == "book" and v is not None:
            v = [list(level) for level in v]
        elif f == "band" and v is not None:
            v = list(v)
        out[f] = v
    return out


# ── The strategy's own state (A2 §4.1) ───────────────────────────────────────

MAX_BYTES = 256 * 1024 * 1024     # the longest state text either runner reads (the engine's SOD_STATE_MAX_BYTES is the
# same number). The largest state the tests write is about 31 KB and a long paper session's is some tens of MB, so
# this is a bound, not a working size. Both runners check it before they parse anything.
MAX_DEPTH = 64                    # levels of lists and objects in a free-form part (strategy, queued_notifications)
TEXT_DEPTH = MAX_DEPTH + 1        # …and in the whole text: the state object itself is one more level
INT_LO, INT_HI = -(2**63), 2**64 - 1   # the integers a free-form part may hold: int64 and uint64, as the C++ reader has
I64_MAX = 2**63 - 1
I32_MAX = 2**31 - 1
U32_MAX = 2**32 - 1
# An id counter or the charge-row count may not pass 2**62. A run adds at most one a nanosecond, so no run reaches it
# (146 years), and a state the runner writes is therefore one its own reader accepts: the writer's next id or row
# stays far inside int64 (the C++ engine adds them in int64 and the CSV writers count past them).
COUNTER_MAX = 2**62


def esc(s: str) -> str:
    """A piece of the file's text, as it goes into an error message: quoted, one printable ASCII byte at a time
    (`'` and `\\` escaped, everything else outside 0x20-0x7e as \\xHH of its UTF-8 bytes), cut at 100 bytes. The C++
    reader writes the same bytes."""
    raw = s.encode("utf-8", "surrogatepass")
    cut = len(raw) > 100
    out = []
    for b in raw[:100]:
        if b == 0x27:
            out.append("\\'")
        elif b == 0x5C:
            out.append("\\\\")
        elif 0x20 <= b <= 0x7E:
            out.append(chr(b))
        else:
            out.append(f"\\x{b:02x}")
    return "'" + "".join(out) + ("..." if cut else "") + "'"


def _walk_free_form(owner: str, root, *, save: bool = False):  # noqa: ANN001, ANN202
    """Visit every value of a free-form part without recursion: (path, value) in document order. Raises StateError
    for a part nested past MAX_DEPTH levels or a key that is not a string (the caller checks the leaves)."""
    stack = [(owner, root, 1)]
    while stack:
        path, v, level = stack.pop()
        if isinstance(v, (list, dict)):
            if level > MAX_DEPTH:
                if save:
                    raise StateError(f"strategy {owner}: save_state() returned a state nested more than {MAX_DEPTH} "
                                     f"levels deep")
                raise StateError(f"sod_state: {owner} is nested more than {MAX_DEPTH} levels deep")
            if isinstance(v, list):
                kids = [(f"{path}[{n}]", x, level + 1) for n, x in enumerate(v)]
            else:
                kids = []
                for k, x in v.items():
                    if not isinstance(k, str):
                        if save:
                            raise StateError(f"strategy {owner}: save_state() returned {path} with key {k!r}; keys "
                                             f"must be strings")
                        raise StateError(f"sod_state: {owner} has a key that is not text")
                    kids.append((f"{path}[{k!r}]", x, level + 1))
            stack.extend(reversed(kids))
        yield path, v


def _check_json(owner: str, root) -> None:  # noqa: ANN001
    """What save_state() returned: JSON-typed, at most MAX_DEPTH levels deep, integers inside 64 bits, finite floats
    that canonical JSON gives back exactly."""
    for path, v in _walk_free_form(owner, root, save=True):
        path = "state" + path[len(owner):]
        if v is None or isinstance(v, (bool, str, list, dict)):
            continue
        if isinstance(v, int):
            if not INT_LO <= v <= INT_HI:
                raise StateError(f"strategy {owner}: save_state() returned {path} = an integer outside 64 bits")
            continue
        if isinstance(v, float):
            if not math.isfinite(v):
                raise StateError(f"strategy {owner}: save_state() returned {path} = {v!r}; NaN and Infinity cannot be "
                                 f"saved")
            if float(numerics.format_float(v)) != v:
                raise StateError(f"strategy {owner}: save_state() returned {path} = {v!r}, which canonical JSON does "
                                 f"not round-trip exactly")
            continue
        raise StateError(f"strategy {owner}: save_state() returned {path} = a {type(v).__name__}, which is not "
                         f"JSON-typed (None, bool, int, finite float, str, list, str-keyed dict; a tuple would come "
                         f"back as a list)")


def strategy_state(strategy) -> dict:  # noqa: ANN001
    """strategy.save_state(), checked: a JSON object of JSON-typed values that canonical JSON gives back exactly."""
    owner = type(strategy).__name__
    try:
        st = strategy.save_state()
    except Exception as e:  # noqa: BLE001 — the strategy's own code raised it
        raise StateError(f"strategy {owner}.save_state() raised {type(e).__name__}: {e}") from e
    if not isinstance(st, dict):
        raise StateError(f"strategy {owner}: save_state() must return a dict, got a {type(st).__name__}")
    _check_json(owner, st)
    return st


def capture(engine) -> dict:  # noqa: ANN001
    """The state at the end of the engine's last processed day (or, before a run, the state it was loaded with)."""
    led: Ledger = engine.book.ledger
    # Invariants for what the state leaves out (A1 §6.4): DAY_END releases every CNC credit, and an instrument's last
    # price is its market LTP, so both are rebuilt on load rather than carried.
    if led.unavailable:
        raise StateError(f"sod_state: CNC releases left after DAY_END: {sorted(led.unavailable)}")
    ltps = {iid: m.ltp for iid, m in engine.market.items() if m.ltp is not None}
    if engine.underlying_last != ltps:
        raise StateError("sod_state: last prices differ from the market LTPs")
    orders = [engine.book.orders[k] for k in sorted(engine.book.orders)]
    bar_history = {}
    for iid in sorted(engine.market):
        for isec in sorted(engine.market[iid].bars):
            bar_history[f"{iid}:{isec}"] = [list(r) for r in engine.market[iid].bars[isec]]
    return {
        "schema": SCHEMA,
        "trade_date": engine.state_date,
        "cash_paise": led.cash,
        "ledger": {"capital_paise": led.capital, "charges_total_paise": led.charges_total,
                   "reserved_paise": led.reserved,
                   "positions": [{f: getattr(p, f) for f in POSITION_FIELDS} for p in led.positions.values()]},
        "pending_orders": [_order(o) for o in orders if o.status not in FINAL],
        "final_orders": [_order(o) for o in orders if o.status in FINAL],
        "dp_done": sorted([iid, d] for iid, d in engine.book.dp_done),
        "timers": _timers(engine),
        "built_bars": _built(engine),
        "counters": {"next_order": engine.next_order, "next_fill": engine.book.next_fill,
                     "next_timer": engine.next_timer,
                     "charge_row": engine.charge_row_base + len(engine.book.charges)},
        "rng": {"slippage": [engine.book.rng.state, engine.book.rng.inc],
                "strategy": [engine.strategy_rng.state, engine.strategy_rng.inc]},
        "subscriptions": {"ticks": sorted(engine.sub_ticks),
                          "bars": {str(iid): sorted(v) for iid, v in sorted(engine.sub_bars.items())}},
        "strategy_enabled": engine.strategy_enabled,
        "pause_reasons": sorted(engine.pause_reasons),
        "queued_notifications": list(engine.queued_notifications),
        "fsp_seen": sorted([uid, d, px] for (uid, d), px in engine.fsp_seen.items()),
        "market": {str(iid): _market(engine.market[iid]) for iid in sorted(engine.market)},
        "bar_history": bar_history,
        "strategy": (engine.day_end_strategy_state if engine.day_end_strategy_state is not None
                     else strategy_state(engine.strategy)),
    }


# ── Restore ──────────────────────────────────────────────────────────────────
#
# The state file is untrusted input. `restore` checks all of it first, in the order below (the C++ reader checks in
# the same order, so both refuse a state with the same words), and only then changes the engine: a refused load
# leaves the engine exactly as it was.

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


def _bad(where: str, what: str) -> StateError:
    return StateError(f"sod_state: {where} {what}")


def _need(d: dict, keys, where: str) -> None:  # noqa: ANN001
    """Exactly these keys: a missing one, or one the schema does not have, is refused."""
    if not isinstance(d, dict):
        raise StateError(f"sod_state: {where} must be an object")
    for k in keys:
        if k not in d:
            raise StateError(f"sod_state: {where} is missing {k!r}")
    extra = sorted(set(d) - set(keys))
    if extra:
        raise StateError(f"sod_state: {where} has unknown keys [" + ", ".join(esc(k) for k in extra) + "]")


def _int(v, where: str) -> int:  # noqa: ANN001
    if type(v) is not int:
        raise _bad(where, "must be an integer")
    if not -(2**63) <= v <= I64_MAX:
        raise _bad(where, "does not fit in int64")
    return v


def _nonneg(v, where: str) -> int:  # noqa: ANN001
    v = _int(v, where)
    if v < 0:
        raise _bad(where, "must not be negative")
    return v


def _pos(v, where: str) -> int:  # noqa: ANN001
    v = _int(v, where)
    if v <= 0:
        raise _bad(where, "must be positive")
    return v


def _atleast1(v, where: str) -> int:  # noqa: ANN001
    v = _int(v, where)
    if v < 1:
        raise _bad(where, "must be at least 1")
    return v


def _counter(v, where: str, lo: int) -> int:  # noqa: ANN001
    v = _int(v, where)
    if v < lo:
        raise _bad(where, "must be at least 1" if lo == 1 else "must not be negative")
    if v > COUNTER_MAX:
        raise _bad(where, f"must be at most {COUNTER_MAX}")
    return v


def _i32(v, where: str) -> int:  # noqa: ANN001
    v = _int(v, where)
    if not -(2**31) <= v <= I32_MAX:
        raise _bad(where, "does not fit in int32")
    return v


def _iid(v, where: str) -> int:  # noqa: ANN001
    v = _int(v, where)
    if not 0 <= v <= U32_MAX:
        raise _bad(where, "must be between 0 and 4294967295")
    return v


def _str(v, where: str) -> str:  # noqa: ANN001
    if type(v) is not str:
        raise _bad(where, "must be a string")
    return v


def _bool(v, where: str) -> bool:  # noqa: ANN001
    if type(v) is not bool:
        raise _bad(where, "must be a boolean")
    return v


def _date(v, where: str) -> str:  # noqa: ANN001
    """A real YYYY-MM-DD date (nothing else: not 20260112, not a time after it)."""
    if type(v) is not str or not _DATE.fullmatch(v):
        raise _bad(where, "must be a YYYY-MM-DD date")
    try:
        _date_cls.fromisoformat(v)
    except ValueError:
        raise _bad(where, "must be a YYYY-MM-DD date") from None
    return v


def _list(v, where: str, size: int | None = None) -> list:  # noqa: ANN001
    if type(v) is not list or (size is not None and len(v) != size):
        raise _bad(where, f"must be a list of {size}" if size is not None else "must be a list")
    return v


def _opt(check, v, where: str):  # noqa: ANN001, ANN202
    return None if v is None else check(v, where)


def _known(v: str, allowed, group: str, what: str) -> str:  # noqa: ANN001
    if v not in allowed:
        raise StateError(f"sod_state: {group}: unknown {what} {esc(v)}")
    return v


def _is_decimal(s: str) -> bool:
    """ASCII digits, no leading zero (so a key is the one spelling of its number)."""
    return bool(s) and len(s) <= 10 and s.isascii() and s.isdigit() and (s[0] != "0" or s == "0")


def _iid_key(key: str, where: str) -> int:
    """int(key) for an object keyed by an instrument id in decimal."""
    if not _is_decimal(key) or int(key) > U32_MAX:
        raise _bad(where, f"key {esc(key)} is not an instrument id")
    return int(key)


def _day_after(day: str) -> int:
    """The first instant after an IST calendar day: its end, as UTC ns."""
    return ((_date_cls.fromisoformat(day).toordinal() - _EPOCH_ORDINAL + 1) * 86400 - IST_OFFSET_S) * 10**9


def _snapshot(v):  # noqa: ANN001, ANN202
    if v is None:
        return None
    _need(v, SNAPSHOT_FIELDS, "market snapshot")
    return {k: (_int if k == "t" else _nonneg)(v[k], f"market snapshot.{k}") for k in SNAPSHOT_FIELDS}


def _read_order(o, group: str, bundle_ids, next_order: int, seen: set) -> Order:  # noqa: ANN001
    _need(o, ORDER_FIELDS, group)
    oid = _atleast1(o["order_id"], f"{group}.order_id")
    if oid in seen:
        raise StateError(f"sod_state: {group}: order {oid} is listed twice")
    seen.add(oid)
    if oid >= next_order:
        raise StateError(f"sod_state: {group}: order {oid} is not below counters.next_order ({next_order})")
    status = _known(_str(o["status"], f"{group}.status"), LIVE + FINAL, group, "status")
    _known(_str(o["reason"], f"{group}.reason"), REASONS, group, "reason")
    if group == "pending_orders" and status in FINAL:
        raise StateError(f"sod_state: {group}: order {oid} has the final status {esc(status)}")
    if group == "final_orders" and status not in FINAL:
        raise StateError(f"sod_state: {group}: order {oid} has the live status {esc(status)}")
    # A rejected order keeps what the strategy placed it with, valid or not (orders.csv writes it as given); every
    # other order was accepted, so its words and numbers are the engine's own.
    placed = status != "REJECTED"
    w = group + "."
    _nonneg(o["parent_id"], w + "parent_id")
    iid = _iid(o["instrument_id"], w + "instrument_id")
    if group == "pending_orders" and iid not in bundle_ids:
        raise _bad(w + "instrument_id", f"{iid} is not in the bundle")
    side = _str(o["side"], w + "side")
    if placed:
        _known(side, SIDES, group, "side")
    qty = _pos(o["qty"], w + "qty") if placed else _int(o["qty"], w + "qty")
    product = _str(o["product"], w + "product")
    type_ = _str(o["type"], w + "type")
    validity = _str(o["validity"], w + "validity")
    if placed:
        _known(product, PRODUCTS, group, "product")
        _known(type_, TYPES, group, "type")
        _known(validity, VALIDITIES, group, "validity")
    _int(o["price"], w + "price")
    _int(o["trigger"], w + "trigger")
    prot = o["protection"]
    if not (type(prot) is str and prot == "AUTO"):
        _i32(prot, w + "protection")
    _str(o["tag"], w + "tag")
    _int(o["t_submit"], w + "t_submit")
    _int(o["t_activate"], w + "t_activate")
    filled = _nonneg(o["filled_qty"], w + "filled_qty")
    if placed and filled > qty:
        raise _bad(w + "filled_qty", "must not be more than qty")
    _int(o["fill_value"], w + "fill_value")
    _opt(_int, o["limit"], w + "limit")
    _bool(o["from_market"], w + "from_market")
    _bool(o["triggered"], w + "triggered")
    _opt(_i32, o["protection_bp"], w + "protection_bp")
    _nonneg(o["reserved"], w + "reserved")
    _bool(o["first_eligible_done"], w + "first_eligible_done")
    _opt(_date, o["trade_date"], w + "trade_date")
    _bool(o["charged"], w + "charged")
    _opt(_date, o["eod_after"], w + "eod_after")
    return Order(**{f: o[f] for f in ORDER_FIELDS})


def _stream(v, name: str) -> list:  # noqa: ANN001
    pair = _list(v, f"rng.{name}", 2)
    for what, x in zip(("state", "inc"), pair):
        if type(x) is not int or not 0 <= x < 2**64:
            raise _bad(f"rng.{name}", f"{what} must be a non-negative 64-bit integer")
    if pair[1] % 2 == 0:
        raise _bad(f"rng.{name}", "inc must be odd")
    return list(pair)


def _free_form(owner: str, v):  # noqa: ANN001, ANN202
    """A part the strategy or the notifier owns: any JSON, but bounded (depth, integers inside 64 bits, finite)."""
    for _, x in _walk_free_form(owner, v):
        if isinstance(x, bool) or x is None or isinstance(x, str):
            continue
        if isinstance(x, int):
            if not INT_LO <= x <= INT_HI:
                raise _bad(owner, "holds an integer outside 64 bits")
        elif isinstance(x, float):
            if not math.isfinite(x):
                raise _bad(owner, "holds a number that is out of range")
        elif not isinstance(x, (list, dict)):
            raise _bad(owner, "holds a value that is not JSON-typed")
    return v


def parse(engine, state) -> dict:  # noqa: ANN001
    """Check a start-of-day state against the engine's bundle and return what restore applies. The engine is not
    touched. Raises StateError."""
    from stockify.oracle.runner import Market
    if not isinstance(state, dict) or "schema" not in state:
        raise StateError("sod_state: state must be an object with a 'schema'")
    if state["schema"] != SCHEMA:
        got = esc(state["schema"]) if type(state["schema"]) is str else "a non-string"
        raise StateError(f"sod_state: schema must be {SCHEMA!r}, got {got}")
    _need(state, KEYS, "state")
    ids = engine.inst
    first_day = engine.days[0]["date"]
    day = _date(state["trade_date"], "trade_date")
    if day >= first_day:
        raise StateError(f"sod_state: trade_date {day} must be before the bundle's first day {first_day}")
    after = _day_after(day)

    c = state["counters"]
    _need(c, COUNTERS, "counters")
    next_order = _counter(c["next_order"], "counters.next_order", 1)
    next_fill = _counter(c["next_fill"], "counters.next_fill", 1)
    next_timer = _counter(c["next_timer"], "counters.next_timer", 1)
    charge_row = _counter(c["charge_row"], "counters.charge_row", 0)

    cash = _int(state["cash_paise"], "cash_paise")
    led_doc = state["ledger"]
    _need(led_doc, ("capital_paise", "charges_total_paise", "reserved_paise", "positions"), "ledger")
    capital = _int(led_doc["capital_paise"], "ledger.capital_paise")
    charges_total = _int(led_doc["charges_total_paise"], "ledger.charges_total_paise")
    reserved = _int(led_doc["reserved_paise"], "ledger.reserved_paise")
    positions: dict = {}
    for p in _list(led_doc["positions"], "ledger.positions"):
        _need(p, POSITION_FIELDS, "ledger position")
        iid = _iid(p["instrument_id"], "ledger position.instrument_id")
        if iid not in ids:
            raise _bad("ledger position.instrument_id", f"{iid} is not in the bundle")
        product = _known(_str(p["product"], "ledger position.product"), PRODUCTS, "ledger position", "product")
        style = _known(_str(p["style"], "ledger position.style"), ("premium", "variation"), "ledger position",
                       "style")
        q = _nonneg(p["q"], "ledger position.q")
        d = _int(p["d"], "ledger position.d")
        if d not in (1, -1):
            raise _bad("ledger position.d", "must be 1 or -1")
        cost = _nonneg(p["c"], "ledger position.c")
        if q == 0 and cost != 0:
            raise _bad("ledger position.c", "must be 0 when q is 0")
        realised = _int(p["realised"], "ledger position.realised")
        vm = _int(p["vm"], "ledger position.vm")
        last_mark = _opt(_int, p["last_mark"], "ledger position.last_mark")
        mark_source = _str(p["mark_source"], "ledger position.mark_source")
        if (iid, product) in positions:
            raise StateError(f"sod_state: ledger position {iid} {product} is listed twice")
        positions[(iid, product)] = Position(iid, product, style, q, d, cost, realised, vm, last_mark, mark_source)

    orders, seen = [], set()
    for group in ("pending_orders", "final_orders"):
        for o in _list(state[group], group):
            orders.append(_read_order(o, group, ids, next_order, seen))

    dp_done = set()
    for row in _list(state["dp_done"], "dp_done"):
        pair = _list(row, "dp_done row", 2)
        iid = _iid(pair[0], "dp_done instrument_id")
        if iid not in ids:
            raise _bad("dp_done instrument_id", f"{iid} is not in the bundle")
        dp_done.add((iid, _date(pair[1], "dp_done trade_date")))

    timers, timer_ids = [], set()
    for row in _list(state["timers"], "timers"):
        t = _list(row, "timer", 3)
        tid = _atleast1(t[0], "timer id")
        at = _int(t[1], "timer t_ns")
        period = _nonneg(t[2], "timer period_ns")
        if at + period > I64_MAX:
            raise StateError(f"sod_state: timer {tid}: t_ns + period_ns does not fit in int64")
        if at < after:
            raise StateError(f"sod_state: timer {tid} is not after the saved day {day}")
        if tid in timer_ids:
            raise StateError(f"sod_state: timer {tid} is listed twice")
        timer_ids.add(tid)
        if tid >= next_timer:
            raise StateError(f"sod_state: timer {tid} is not below counters.next_timer ({next_timer})")
        timers.append((tid, at, period))

    built, built_keys = [], set()
    for row in _list(state["built_bars"], "built_bars"):
        b = _list(row, "built bar", 9)
        iid = _iid(b[0], "built bar instrument_id")
        if iid not in ids:
            raise _bad("built bar instrument_id", f"{iid} is not in the bundle")
        isec = _i32(b[1], "built bar interval_s")
        if isec <= 0:
            raise _bad("built bar interval_s", "must be positive")
        start = _int(b[2], "built bar start_ns")
        end = start + isec * 1_000_000_000
        if end > I64_MAX:
            raise StateError("sod_state: built bar: start_ns + interval_s does not fit in int64")
        if end < after:
            raise StateError(f"sod_state: built bar {iid} {isec} is not after the saved day {day}")
        values = [_nonneg(x, "built bar value") for x in b[3:]]
        if (iid, isec, start) in built_keys:
            raise StateError(f"sod_state: built bar {iid} {isec} {start} is listed twice")
        built_keys.add((iid, isec, start))
        built.append((iid, isec, start, values))

    rng = state["rng"]
    _need(rng, ("slippage", "strategy"), "rng")
    slippage = _stream(rng["slippage"], "slippage")
    strategy_rng = _stream(rng["strategy"], "strategy")

    subs = state["subscriptions"]
    _need(subs, ("ticks", "bars"), "subscriptions")
    sub_ticks = set()
    for iid in _list(subs["ticks"], "subscriptions.ticks"):
        iid = _iid(iid, "subscriptions.ticks")
        if iid not in ids:
            raise _bad("subscriptions.ticks", f"{iid} is not in the bundle")
        sub_ticks.add(iid)
    if type(subs["bars"]) is not dict:
        raise StateError("sod_state: subscriptions.bars must be an object")
    sub_bars: dict = {}
    for key, intervals in subs["bars"].items():
        iid = _iid_key(key, "subscriptions.bars")
        if iid not in ids:
            raise _bad("subscriptions.bars", f"key {esc(key)} is not in the bundle")
        mine = sub_bars.setdefault(iid, set())
        for isec in _list(intervals, "subscriptions.bars"):
            isec = _i32(isec, "subscriptions.bars interval_s")
            if isec <= 0:
                raise _bad("subscriptions.bars interval_s", "must be positive")
            mine.add(isec)

    strategy_enabled = _bool(state["strategy_enabled"], "strategy_enabled")
    pause_reasons = {_str(r, "pause_reasons") for r in _list(state["pause_reasons"], "pause_reasons")}
    queued = _free_form("queued_notifications", _list(state["queued_notifications"], "queued_notifications"))

    fsp_seen: dict = {}
    for row in _list(state["fsp_seen"], "fsp_seen"):
        f = _list(row, "fsp_seen row", 3)
        uid = _iid(f[0], "fsp_seen underlying_id")
        if uid not in ids:
            raise _bad("fsp_seen underlying_id", f"{uid} is not in the bundle")
        d = _date(f[1], "fsp_seen date")
        px = _int(f[2], "fsp_seen paise")
        if (uid, d) in fsp_seen:
            raise StateError(f"sod_state: fsp_seen {uid} {d} is listed twice")
        fsp_seen[(uid, d)] = px

    if type(state["market"]) is not dict:
        raise StateError("sod_state: market must be an object")
    market: dict = {}
    for key, m in state["market"].items():
        iid = _iid_key(key, "market")
        if iid not in ids:
            raise _bad("market", f"key {esc(key)} is not in the bundle")
        _need(m, MARKET_FIELDS, "market")
        book = band = min_interval = None
        ltp = _opt(_nonneg, m["ltp"], "market.ltp")
        t = _opt(_int, m["t"], "market.t")
        s0, s1 = _snapshot(m["s0"]), _snapshot(m["s1"])
        bid = _opt(_nonneg, m["bid"], "market.bid")
        ask = _opt(_nonneg, m["ask"], "market.ask")
        if m["book"] is not None:
            book = []
            for lv in _list(m["book"], "market.book", 10):
                lv = _list(lv, "market.book level", 3)
                book.append((_nonneg(lv[0], "market.book price"), _nonneg(lv[1], "market.book qty"),
                             _i32(lv[2], "market.book orders")))
        if m["band"] is not None:
            pair = _list(m["band"], "market.band", 2)
            band = (_nonneg(pair[0], "market.band lower"), _nonneg(pair[1], "market.band upper"))
        oi = _opt(_nonneg, m["oi"], "market.oi")
        if m["min_interval"] is not None:
            min_interval = _i32(m["min_interval"], "market.min_interval")
            if min_interval <= 0:
                raise _bad("market.min_interval", "must be positive")
        mday = _opt(_date, m["day"], "market.day")
        market[iid] = Market(ltp=ltp, t=t, s0=s0, s1=s1, bid=bid, ask=ask, book=book, band=band, oi=oi,
                             min_interval=min_interval, day=mday)

    if type(state["bar_history"]) is not dict:
        raise StateError("sod_state: bar_history must be an object")
    for key, rows in state["bar_history"].items():
        iid_text, colon, isec_text = key.partition(":")
        if (not colon or not _is_decimal(iid_text) or not _is_decimal(isec_text) or int(iid_text) > U32_MAX
                or not 0 < int(isec_text) <= I32_MAX):
            raise _bad("bar_history", f"key {esc(key)} is not <instrument_id>:<interval_s>")
        iid, isec = int(iid_text), int(isec_text)
        if iid not in ids:
            raise _bad("bar_history", f"key {esc(key)} is not in the bundle")
        if iid not in market:
            raise _bad("bar_history", f"key {esc(key)} has no market entry")
        out, last = [], None
        for row in _list(rows, "bar_history"):
            r = _list(row, "bar_history row", 7)
            start = _int(r[0], "bar start_ns")
            vals = [_nonneg(x, f"bar {name}")
                    for x, name in zip(r[1:], ("open", "high", "low", "close", "volume", "oi"))]
            if last is not None and start <= last:
                raise StateError(f"sod_state: bar_history {key}: start_ns must rise from row to row")
            last = start
            out.append((start, *vals))
        market[iid].bars[isec] = out

    if not isinstance(state["strategy"], dict):
        raise StateError("sod_state: strategy must be an object")
    strategy = _free_form("strategy", state["strategy"])
    return {
        "day": day, "cash": cash, "capital": capital, "charges_total": charges_total, "reserved": reserved,
        "positions": positions, "orders": orders, "dp_done": dp_done, "next_order": next_order,
        "next_fill": next_fill, "next_timer": next_timer, "charge_row": charge_row, "slippage": slippage,
        "strategy_rng": strategy_rng, "timers": timers, "built": built, "sub_ticks": sub_ticks,
        "sub_bars": sub_bars, "strategy_enabled": strategy_enabled, "pause_reasons": pause_reasons,
        "queued": queued, "fsp_seen": fsp_seen, "market": market, "strategy": strategy,
    }


def restore(engine, state: dict) -> None:  # noqa: ANN001
    """Apply a start-of-day state to an engine that has not processed any event (A4 §1.10): all of it, or (a
    StateError) none of it."""
    from stockify.oracle.runner import BAR_BUILT, TIMER
    s = parse(engine, state)
    # The strategy's part goes first among the changes: it is the one step that runs code we did not write, so its
    # failure must leave the engine untouched. Everything below cannot fail.
    try:
        engine.strategy.load_state(s["strategy"])
    except Exception as e:  # noqa: BLE001 — the strategy's own code raised it
        raise StateError(f"strategy {type(engine.strategy).__name__}.load_state() raised {type(e).__name__}: "
                         f"{e}") from e
    book = engine.book
    book.ledger = Ledger(s["cash"], s["capital"], s["positions"], s["charges_total"], {}, s["reserved"])
    book.orders = {o.order_id: o for o in sorted(s["orders"], key=lambda o: o.order_id)}
    engine.prior_final = {o.order_id for o in s["orders"] if o.status in FINAL}
    engine.live_ids = sorted(o.order_id for o in s["orders"] if o.status not in FINAL)
    book.dp_done = s["dp_done"]
    engine.next_order, book.next_fill, engine.next_timer = s["next_order"], s["next_fill"], s["next_timer"]
    engine.charge_row_base = s["charge_row"]
    book.rng.state, book.rng.inc = s["slippage"]
    engine.strategy_rng.state, engine.strategy_rng.inc = s["strategy_rng"]
    for tid, t, period in s["timers"]:
        engine.timers[tid] = (t, period)
        engine.push(t, TIMER, "TIMER", tid)
    for iid, isec, start, bar in s["built"]:
        engine.built.setdefault((iid, isec), {})[start] = list(bar)
        engine.push(start + isec * 1_000_000_000, BAR_BUILT, "BAR_BUILT", (iid, isec, start))
    engine.sub_ticks = s["sub_ticks"]
    engine.sub_bars = s["sub_bars"]
    engine.strategy_enabled = s["strategy_enabled"]
    engine.pause_reasons = s["pause_reasons"]
    engine.queued_notifications = list(s["queued"])
    engine.fsp_seen = s["fsp_seen"]
    engine.market = s["market"]
    engine.underlying_last = {iid: m.ltp for iid, m in engine.market.items() if m.ltp is not None}
    engine.state_date = s["day"]
    # Until the engine runs, the state it would write for the strategy is the one it was handed (run() clears this).
    engine.day_end_strategy_state = copy.deepcopy(s["strategy"])


def loads(text: str) -> dict:
    """The text of a state file, parsed: bounded (size, depth, integers inside 64 bits, finite numbers), no NUL,
    no key twice in one object. Raises StateError."""
    if not isinstance(text, str):
        raise StateError("sod_state: the text must be a string")
    if len(text) > MAX_BYTES:                         # a character is at least one byte
        raise StateError(f"sod_state: the text is larger than {MAX_BYTES} bytes")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise StateError("sod_state: the text is not valid Unicode") from None
    if size > MAX_BYTES:
        raise StateError(f"sod_state: the text is larger than {MAX_BYTES} bytes")
    if "\x00" in text:
        raise StateError("sod_state: the text has a NUL character")
    if text.startswith("\ufeff"):
        raise StateError("sod_state: invalid JSON: a byte order mark comes first")
    _scan(text)
    try:
        return json.loads(text, object_pairs_hook=_object, parse_int=_parse_int, parse_float=_parse_float,
                          parse_constant=_parse_constant)
    except json.JSONDecodeError as e:
        raise StateError(f"sod_state: invalid JSON: {e}") from e


_TOKEN = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|[\[\]{}]')


def _scan(text: str) -> None:
    """Depth and unpaired surrogates, before json.loads recurses on what it is given."""
    depth = 0
    for m in _TOKEN.finditer(text):
        tok = m.group()
        if tok in "[{":
            depth += 1
            if depth > TEXT_DEPTH:
                raise StateError(f"sod_state: the text is nested more than {TEXT_DEPTH} levels deep")
        elif tok in "]}":
            depth -= 1
        elif "\\u" in tok and _lone_surrogate(tok):
            raise StateError("sod_state: invalid JSON: a string holds an unpaired surrogate")


def _lone_surrogate(tok: str) -> bool:
    """A \\uD800-\\uDFFF escape that is not half of a pair (an escaped backslash before the u is not an escape)."""
    i = 0
    while True:
        i = tok.find("\\", i)
        if i < 0:
            return False
        if tok[i + 1] != "u":
            i += 2
            continue
        code = int(tok[i + 2:i + 6], 16)
        if 0xD800 <= code < 0xDC00:
            nxt = tok[i + 6:i + 12]
            if not (nxt.startswith("\\u") and 0xDC00 <= int(nxt[2:6], 16) < 0xE000):
                return True
            i += 12
            continue
        if 0xDC00 <= code < 0xE000:
            return True
        i += 6


BUNDLE_DEPTH = 100                # levels anywhere in a bundle file outside its sod_state (a guard for json.loads)
_COLON = re.compile(r"\s*:")


def _is_state_key(tok: str) -> bool:
    """Is this string token the key `sod_state`, however it is spelled (a JSON escape decodes to the same key)?"""
    if tok == '"sod_state"':
        return True
    if "\\" not in tok:
        return False
    try:
        return json.loads(tok) == "sod_state"
    except ValueError:
        return False


def _scan_bundle(text: str) -> None:
    """The bundle's depth guard: the `sod_state` member under the same limit as a state text (counted from the state
    object itself), the rest of the file under BUNDLE_DEPTH; unpaired surrogates anywhere."""
    depth = 0
    state_depth = None                # the depth of the `sod_state` value's own container, while inside it
    pending = False                   # the next token is the value of the top-level "sod_state" key
    for m in _TOKEN.finditer(text):
        tok = m.group()
        if tok in "[{":
            depth += 1
            if pending:
                state_depth = depth
            pending = False
            if state_depth is not None:
                if depth - state_depth + 1 > TEXT_DEPTH:
                    raise StateError(f"sod_state: the text is nested more than {TEXT_DEPTH} levels deep")
            elif depth > BUNDLE_DEPTH:
                raise StateError(f"sod_state: the bundle is nested more than {BUNDLE_DEPTH} levels deep")
        elif tok in "]}":
            depth -= 1
            if state_depth is not None and depth < state_depth:
                state_depth = None
        else:
            pending = False
            if "\\u" in tok and _lone_surrogate(tok):
                raise StateError("sod_state: invalid JSON: a string holds an unpaired surrogate")
            if depth == 1 and state_depth is None and _is_state_key(tok) and _COLON.match(text, m.end()):
                pending = True


def loads_bundle(text: str) -> dict:
    """A bundle file's text, parsed as the oracle's file readers read it: its `sod_state` is held to the rules of a
    state text (depth, integers inside 64 bits, no key twice); json.loads alone would let a deep one crash with
    RecursionError and a repeated key win silently. Raises StateError. (The C++ reader checks the `sod_state` member
    alone; a repeated key elsewhere in a bundle is refused here too, which no bundle the builder writes has.)"""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8")
    _scan_bundle(text)
    try:
        return json.loads(text, object_pairs_hook=_object, parse_int=_parse_int, parse_float=_parse_float,
                          parse_constant=_parse_constant)
    except json.JSONDecodeError as e:
        raise StateError(f"sod_state: invalid JSON: {e}") from e


def _object(pairs: list) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise StateError(f"sod_state: duplicate key {esc(k)}")
        out[k] = v
    return out


def _parse_int(s: str) -> int:
    if len(s) > 21:
        raise StateError("sod_state: an integer outside 64 bits")
    v = int(s)
    if not INT_LO <= v <= INT_HI:
        raise StateError("sod_state: an integer outside 64 bits")
    return v


def _parse_float(s: str) -> float:
    v = float(s)
    if not math.isfinite(v):
        raise StateError("sod_state: a number is out of range")
    return v


def _parse_constant(name: str) -> float:
    raise StateError(f"sod_state: invalid JSON: {name} is not a JSON value")


def dumps(state: dict) -> str:
    return numerics.dumps_canonical(state)


# ── Size guard (C-2: nothing is trimmed) ─────────────────────────────────────

def size_warning(text: str, warn_bytes: int) -> str | None:
    """A warning when the state's UTF-8 size is past `warn_bytes` (paper.json sod_state_warn_mb), else None."""
    n = len(text.encode("utf-8"))
    if n <= warn_bytes:
        return None
    return (f"sod_state is {n} bytes, past the {warn_bytes}-byte warning (paper.json sod_state_warn_mb); "
            f"the bar history is never trimmed (C-2)")


def warn_bytes() -> int:
    from stockify.config import loader
    return loader.load_json("paper.json")["sod_state_warn_mb"] * 1024 * 1024
