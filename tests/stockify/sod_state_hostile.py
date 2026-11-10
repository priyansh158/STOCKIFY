"""Hostile start-of-day states: one table, driven against the oracle and the C++ engine (ST 5.1).

The state file sits on disk between days, so both runners treat it as untrusted input: a state is accepted whole or
refused whole (A1 §6.4, A4 §1.10). Each row here is one change to the good fixture state
(tests/fixtures/sod_state/day0_state.json, written by the oracle for the day before day1_bundle.json) and what must
happen to it: `error` is a piece of the refusal both runners must give, or None when both must accept it and then
write the same bytes. `item` is the number of the rule in the fix brief the row belongs to.

A row's `make` gets a fresh copy of the good state (a dict) and the good text, and returns the dict to load (it is
then written as canonical JSON) or the text itself (for what only a text can hold: duplicate keys, a NUL, depth).
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from stockify import numerics

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sod_state"
GOOD_TEXT = (FIXTURES / "day0_state.json").read_text(encoding="utf-8")
BUNDLE_PATH = FIXTURES / "day1_bundle.json"
MAX_BYTES = 256 * 1024 * 1024          # sod_state.MAX_BYTES and the engine's SOD_STATE_MAX_BYTES (a test ties the three)
INT64_MAX = 2**63 - 1
BOUND = 2**62                          # the largest id counter or charge row a state may carry (sod_state.COUNTER_MAX)
MIDNIGHT = 1768242600000000000         # 2026-01-13 00:00 IST: the first instant after the saved day (2026-01-12)
TIMER = [1, 1768244760000000000, 2220000000000]   # the good state's only timer: id, t_ns, period_ns


@dataclass(frozen=True)
class Row:
    name: str
    item: int                          # the brief's rule number
    make: Callable[[dict, str], object]
    error: str | None                  # a piece of the refusal; None: accepted by both, same bytes written back
    run: bool = False                  # accepted rows: also run the day and compare the end-of-day states
    text_only: bool = False            # cannot travel inside a bundle's sod_state as a dict (a text-level refusal)
    bundle: bool = False               # a text_only row that is also spliced, as text, into a bundle file (the
                                       # constructor path: no dict stands between the file and the reader)


def good() -> dict:
    return json.loads(GOOD_TEXT)


def text_of(state) -> str:  # noqa: ANN001
    return state if isinstance(state, str) else numerics.dumps_canonical(state)


def build(row: Row) -> object:
    return row.make(good(), GOOD_TEXT)


# ── Small mutation helpers ───────────────────────────────────────────────────

def put(path: str, value) -> Callable[[dict, str], dict]:  # noqa: ANN001
    """Set one field: "counters.next_order", "ledger.positions.1.d", "final_orders.5.side"."""
    def make(st: dict, _text: str) -> dict:
        node = st
        keys = path.split(".")
        for k in keys[:-1]:
            node = node[int(k)] if isinstance(node, list) else node[k]
        last = keys[-1]
        if isinstance(node, list):
            node[int(last)] = copy.deepcopy(value)
        else:
            node[last] = copy.deepcopy(value)
        return st
    return make


def drop(path: str) -> Callable[[dict, str], dict]:
    def make(st: dict, _text: str) -> dict:
        node = st
        keys = path.split(".")
        for k in keys[:-1]:
            node = node[int(k)] if isinstance(node, list) else node[k]
        del node[int(keys[-1]) if isinstance(node, list) else keys[-1]]
        return st
    return make


def edit(fn: Callable[[dict], None]) -> Callable[[dict, str], dict]:
    def make(st: dict, _text: str) -> dict:
        fn(st)
        return st
    return make


def replace(old: str, new: str) -> Callable[[dict, str], str]:
    """A change to the text itself (for what a dict cannot hold)."""
    def make(_st: dict, text: str) -> str:
        assert text.count(old) == 1, old
        return text.replace(old, new)
    return make


def order(st: dict, order_id: int) -> dict:
    return next(o for o in st["final_orders"] if o["order_id"] == order_id)


def make_live(st: dict, oid: int = 31, **changes) -> dict:  # noqa: ANN003
    """Move a final order (31: cancelled at the end of its day) to pending_orders as OPEN."""
    o = order(st, oid)
    st["final_orders"].remove(o)
    o.update({"status": "OPEN", "reason": ""})
    o.update(changes)
    st["pending_orders"].append(o)
    return o


def live(**changes) -> Callable[[dict, str], dict]:  # noqa: ANN003
    return edit(lambda st: make_live(st, **changes))


def final(oid: int, **changes) -> Callable[[dict, str], dict]:  # noqa: ANN003
    return edit(lambda st: order(st, oid).update(changes))


def nested(depth: int, inner: str = "1") -> str:
    return "[" * depth + inner + "]" * depth


def _dup_position(st: dict) -> None:
    st["ledger"]["positions"].append(copy.deepcopy(st["ledger"]["positions"][1]))


def _dup_order(st: dict) -> None:
    st["final_orders"].append(copy.deepcopy(st["final_orders"][0]))


def _order_in_both_groups(st: dict) -> None:
    o = copy.deepcopy(order(st, 31))
    o.update({"status": "OPEN", "reason": ""})
    st["pending_orders"].append(o)


def _built_bar(st: dict, **kw) -> None:  # noqa: ANN003
    """One built bar of instrument 2 waiting to be published at 00:01 IST on the next day."""
    row = {"iid": 2, "isec": 60, "start": MIDNIGHT, "values": [100, 110, 90, 105, 10, 0]}
    row.update(kw)
    st["built_bars"].append([row["iid"], row["isec"], row["start"], *row["values"]])


def _carried_work(st: dict) -> None:
    """A built bar, a one-shot timer and a live order, all waiting for the next day."""
    _built_bar(st)
    st["timers"].append([2, MIDNIGHT + 5_000_000_000, 0])
    st["counters"]["next_timer"] = 3
    make_live(st)


def _huge_next_order(st: dict) -> None:
    make_live(st)
    st["counters"]["next_order"] = 2**62


def _history(st: dict, fn: Callable[[list], None]) -> None:
    fn(st["bar_history"]["2:60"])


def _market_without(st: dict, iid: str) -> None:
    del st["market"][iid]


def _keys_first(st: dict) -> dict:
    """The same state with a hostile unknown key (quotes, a newline, an escape sequence, a long tail)."""
    st["x'\n\x1b[31m" + "A" * 300] = 1
    return st


def snapshot(**kw) -> dict:  # noqa: ANN003
    out = {"t": 1, "ltp": 1, "volume": 1, "atp": 1, "high": 1, "low": 1}
    out.update(kw)
    return out


def book(slot: int, value: int) -> list:
    """A ten-level book of (price, qty, orders) with one price (slot 0) or quantity (slot 1) changed."""
    rows = [[1, 1, 1] for _ in range(10)]
    rows[3][slot] = value
    return rows


STRATEGY_OK = {"a": -2**63, "b": 2**64 - 1, "s": "é   \" \\ \x7f", "f": 1.5e300, "n": None, "t": [True, {"k": []}]}

ROWS: tuple[Row, ...] = (
    # ── accepted: both load it, and write the same bytes back ────────────────
    Row("the good state", 1, lambda st, text: text, None),
    Row("the good state, then the day is run", 1, lambda st, text: text, None, run=True),
    Row("strategy state at the 64-bit edges", 9, put("strategy", STRATEGY_OK), None),
    Row("strategy nested exactly 64 levels", 2, replace('"strategy":{}', '"strategy":{"k":' + nested(63) + "}"), None),
    Row("no timers", 3, put("timers", []), None),
    Row("a huge next_order with a live order runs", 4, edit(_huge_next_order), None, run=True),
    Row("a rejected order keeps the text it was placed with", 6,
        final(1, side="HOLD", product="XYZ", type="ICEBERG", validity="GTC", qty=-5, instrument_id=999), None),
    Row("a built bar, a one-shot timer and a live order are carried", 3, edit(_carried_work), None, run=True),
    Row("charge_row at the bound loads", 4, put("counters.charge_row", BOUND), None),
    Row("next_fill at the bound loads", 4, put("counters.next_fill", BOUND), None),
    Row("next_timer at the bound loads", 4, put("counters.next_timer", BOUND), None),
    Row("rng state at 2**64-1", 13, put("rng.slippage", [2**64 - 1, 3]), None),

    # ── 1: all-or-nothing (the last checks of the old loader) ────────────────
    Row("strategy is a list", 1, put("strategy", []), "strategy must be an object"),
    Row("a bad row at the end of bar_history", 1,
        edit(lambda st: st["bar_history"]["7:60"].append([1, 2, 3])), "bar_history row must be a list of 7"),
    Row("state is a list", 1, lambda st, text: "[]", "state must be an object with a 'schema'", text_only=True),
    Row("an older schema", 1, put("schema", "stockify.sod_state/0"),
        "schema must be 'stockify.sod_state/1', got 'stockify.sod_state/0'"),
    Row("schema is a number", 1, put("schema", 1), "schema must be 'stockify.sod_state/1', got a non-string"),
    Row("a key is missing", 1, drop("timers"), "state is missing 'timers'"),
    Row("an unknown key", 1, put("marks", {}), "state has unknown keys ['marks']"),
    Row("an unknown counter", 1, put("counters.event_no", 0), "counters has unknown keys ['event_no']"),
    Row("not JSON", 1, lambda st, text: "{not json", "sod_state: invalid JSON", text_only=True),
    Row("an empty text", 1, lambda st, text: "", "sod_state: invalid JSON", text_only=True),
    Row("text after the state", 1, lambda st, text: text + " {}", "sod_state: invalid JSON", text_only=True),

    # ── 2: nesting depth (the two crash cases, and the limit) ────────────────
    Row("queued_notifications nested 5,000 deep (crash)", 2,
        replace('"queued_notifications":[]', '"queued_notifications":' + nested(5000)), "nested more than",
        text_only=True, bundle=True),
    Row("queued_notifications nested 100,000 deep, as a bundle's state would hold it (crash)", 2,
        replace('"queued_notifications":[]', '"queued_notifications":' + nested(100000)), "nested more than",
        text_only=True, bundle=True),
    Row("strategy nested 20,000 deep (crash)", 2,
        replace('"strategy":{}', '"strategy":{"k":' + nested(20000) + "}"), "nested more than", text_only=True, bundle=True),
    Row("strategy nested 65 levels", 2, replace('"strategy":{}', '"strategy":{"k":' + nested(64) + "}"),
        "nested more than"),
    Row("queued_notifications nested 65 levels", 2,
        replace('"queued_notifications":[]', '"queued_notifications":' + nested(65)), "nested more than"),
    Row("a market entry nested 100,000 deep", 2,
        replace('"strategy_enabled":true', '"strategy_enabled":' + nested(100000)), "nested more than",
        text_only=True, bundle=True),

    # ── 3: timers (the two hang cases first) ─────────────────────────────────
    Row("a timer with a negative period (hang)", 3, put("timers.0.2", -1),
        "timer period_ns must not be negative"),
    Row("a timer period near INT64_MAX (hang)", 3, put("timers.0.2", INT64_MAX - 5),
        "timer 1: t_ns + period_ns does not fit in int64"),
    Row("a timer at time 0", 3, put("timers.0.1", 0), "timer 1 is not after the saved day 2026-01-12"),
    Row("a timer inside the saved day", 3, put("timers.0.1", MIDNIGHT - 1),
        "timer 1 is not after the saved day 2026-01-12"),
    Row("a timer before 1970", 3, put("timers.0.1", -5), "timer 1 is not after the saved day 2026-01-12"),
    Row("two rows for one timer", 3, edit(lambda st: st["timers"].append(list(TIMER))), "timer 1 is listed twice"),
    Row("a timer id at next_timer", 3, put("timers.0.0", 2), "timer 2 is not below counters.next_timer (2)"),
    Row("a timer id far past next_timer", 3, put("timers.0.0", 7), "timer 7 is not below counters.next_timer (2)"),
    Row("timer id 0", 3, put("timers.0.0", 0), "timer id must be at least 1"),
    Row("a timer row of two", 3, put("timers.0", [1, MIDNIGHT + 1]), "timer must be a list of 3"),
    Row("a timer time as text", 3, put("timers.0.1", "soon"), "timer t_ns must be an integer"),
    Row("next_timer 0", 3, put("counters.next_timer", 0), "counters.next_timer must be at least 1"),

    # ── 4: ids and counters ──────────────────────────────────────────────────
    Row("next_order at the last order id", 4, put("counters.next_order", 40),
        "final_orders: order 40 is not below counters.next_order (40)"),
    Row("next_order 1 over forty orders", 4, put("counters.next_order", 1),
        "final_orders: order 1 is not below counters.next_order (1)"),
    Row("next_order 0", 4, put("counters.next_order", 0), "counters.next_order must be at least 1"),
    Row("next_order 2**63", 4, put("counters.next_order", 2**63), "counters.next_order does not fit in int64"),
    Row("next_order one past the bound", 4, put("counters.next_order", BOUND + 1),
        "counters.next_order must be at most 4611686018427387904"),
    Row("next_order at INT64_MAX", 4, put("counters.next_order", INT64_MAX),
        "counters.next_order must be at most 4611686018427387904"),
    Row("next_fill one past the bound", 4, put("counters.next_fill", BOUND + 1),
        "counters.next_fill must be at most 4611686018427387904"),
    Row("next_timer one past the bound", 4, put("counters.next_timer", BOUND + 1),
        "counters.next_timer must be at most 4611686018427387904"),
    Row("charge_row one past the bound", 4, put("counters.charge_row", BOUND + 1),
        "counters.charge_row must be at most 4611686018427387904"),
    Row("charge_row at INT64_MAX", 4, put("counters.charge_row", INT64_MAX),
        "counters.charge_row must be at most 4611686018427387904"),
    Row("next_fill 0", 4, put("counters.next_fill", 0), "counters.next_fill must be at least 1"),
    Row("one order listed twice", 4, edit(_dup_order), "final_orders: order 1 is listed twice"),
    Row("one order in both groups", 4, edit(_order_in_both_groups), "final_orders: order 31 is listed twice"),
    Row("one position listed twice", 4, edit(_dup_position), "ledger position 2 MIS is listed twice"),
    Row("order id 0", 4, final(1, order_id=0), "final_orders.order_id must be at least 1"),
    Row("a key twice at the top", 4, replace('"cash_paise":4974100', '"cash_paise":1,"cash_paise":4974100'),
        "duplicate key 'cash_paise'", text_only=True, bundle=True),
    Row("a key twice in the strategy's state", 4, replace('"strategy":{}', '"strategy":{"a":1,"b":2,"a":3}'),
        "duplicate key 'a'", text_only=True, bundle=True),
    Row("a key twice in counters", 4, replace('"next_timer":2', '"next_timer":2,"next_timer":2'),
        "duplicate key 'next_timer'", text_only=True, bundle=True),

    # ── 5: positions ─────────────────────────────────────────────────────────
    Row("position direction 0", 5, put("ledger.positions.1.d", 0), "ledger position.d must be 1 or -1"),
    Row("position direction 2", 5, put("ledger.positions.1.d", 2), "ledger position.d must be 1 or -1"),
    Row("position direction INT32_MAX with q 2**62", 5,
        edit(lambda st: st["ledger"]["positions"][1].update({"d": 2147483647, "q": 2**62})),
        "ledger position.d must be 1 or -1"),
    Row("a negative position quantity", 5, put("ledger.positions.1.q", -5),
        "ledger position.q must not be negative"),
    Row("a negative position cost", 5, put("ledger.positions.1.c", -1), "ledger position.c must not be negative"),
    Row("a flat position with a cost", 5, put("ledger.positions.0.c", 5),
        "ledger position.c must be 0 when q is 0"),
    Row("a position quantity past int64", 5, put("ledger.positions.1.q", 2**63),
        "ledger position.q does not fit in int64"),
    Row("an unknown position product", 5, put("ledger.positions.1.product", "XYZ"),
        "ledger position: unknown product 'XYZ'"),
    Row("an unknown position style", 5, put("ledger.positions.1.style", "cash"),
        "ledger position: unknown style 'cash'"),

    # ── 6: orders ────────────────────────────────────────────────────────────
    Row("a live order with an unknown side", 6, live(side="SEL"), "pending_orders: unknown side 'SEL'"),
    Row("a live order with an unknown product", 6, live(product="HOLD"), "pending_orders: unknown product 'HOLD'"),
    Row("a live order with an unknown type", 6, live(type="ICEBERG"), "pending_orders: unknown type 'ICEBERG'"),
    Row("a live order with an unknown validity", 6, live(validity="GTC"), "pending_orders: unknown validity 'GTC'"),
    Row("a filled order with an unknown side", 6, final(6, side="SEL"), "final_orders: unknown side 'SEL'"),
    Row("an unknown status", 6, final(6, status="DONE"), "final_orders: unknown status 'DONE'"),
    Row("an unknown reason", 6, final(6, reason="BECAUSE"), "final_orders: unknown reason 'BECAUSE'"),
    Row("a live status under final_orders", 6, final(31, status="OPEN", reason=""),
        "final_orders: order 31 has the live status 'OPEN'"),
    Row("a final status under pending_orders", 6, live(status="REJECTED", reason="R_MARGIN"),
        "pending_orders: order 31 has the final status 'REJECTED'"),
    Row("a live order with a negative quantity", 6, live(qty=-5), "pending_orders.qty must be positive"),
    Row("a live order filled past its quantity", 6, live(filled_qty=9),
        "pending_orders.filled_qty must not be more than qty"),
    Row("a negative filled quantity", 6, final(6, filled_qty=-1), "final_orders.filled_qty must not be negative"),
    Row("a negative reserve", 6, live(reserved=-10**18), "pending_orders.reserved must not be negative"),
    Row("a quantity as text", 6, final(6, qty="225"), "final_orders.qty must be an integer"),
    Row("a quantity as a float", 6, final(6, qty=225.0), "final_orders.qty must be an integer"),
    Row("a flag as a number", 6, final(6, charged=1), "final_orders.charged must be a boolean"),
    Row("a protection that is neither AUTO nor a number", 6, final(6, protection="MANUAL"),
        "final_orders.protection must be an integer"),
    Row("an order date that is no date", 6, final(6, trade_date="2026-13-45"),
        "final_orders.trade_date must be a YYYY-MM-DD date"),
    Row("an order with a field missing", 6, drop("final_orders.5.tag"), "final_orders is missing 'tag'"),

    # ── 8: numbers the two JSON readers treat differently ────────────────────
    Row("1e999 in the strategy's state", 8, replace('"strategy":{}', '"strategy":{"x":1e999}'),
        "a number is out of range", text_only=True, bundle=True),
    Row("-1e999 as the cash", 8, replace('"cash_paise":4974100', '"cash_paise":-1e999'),
        "a number is out of range", text_only=True, bundle=True),
    Row("NaN in the strategy's state", 8, replace('"strategy":{}', '"strategy":{"x":NaN}'),
        "sod_state: invalid JSON", text_only=True),
    Row("Infinity in the strategy's state", 8, replace('"strategy":{}', '"strategy":{"x":Infinity}'),
        "sod_state: invalid JSON", text_only=True),
    Row("a lone surrogate escape", 8, replace('"strategy":{}', '"strategy":{"x":"\\ud800"}'),
        "sod_state: invalid JSON", text_only=True),
    Row("a byte order mark first", 8, lambda st, text: "﻿" + text, "sod_state: invalid JSON", text_only=True),
    Row("a built bar whose end overflows", 8, edit(lambda st: _built_bar(st, start=INT64_MAX - 5)),
        "built bar: start_ns + interval_s does not fit in int64"),
    Row("cash as a float", 8, put("cash_paise", 1.5), "cash_paise must be an integer"),
    Row("cash as a flag", 8, put("cash_paise", True), "cash_paise must be an integer"),
    Row("cash past int64", 8, put("cash_paise", 2**63), "cash_paise does not fit in int64"),
    Row("the ledger is a list", 8, put("ledger", []), "ledger must be an object"),

    # ── 9: integers outside 64 bits ──────────────────────────────────────────
    Row("2**64 in the strategy's state", 9, replace('"strategy":{}', '"strategy":{"n":18446744073709551616}'),
        "an integer outside 64 bits", text_only=True, bundle=True),
    Row("-2**63-1 in the strategy's state", 9, replace('"strategy":{}', '"strategy":{"n":-9223372036854775809}'),
        "an integer outside 64 bits", text_only=True, bundle=True),
    Row("10**30 in the strategy's state", 9, replace('"strategy":{}', '"strategy":{"n":1' + "0" * 30 + "}"),
        "an integer outside 64 bits", text_only=True, bundle=True),
    Row("a 5,000-digit integer", 9, replace('"strategy":{}', '"strategy":{"n":' + "9" * 5000 + "}"),
        "an integer outside 64 bits", text_only=True, bundle=True),
    Row("2**64 as the cash", 9, replace('"cash_paise":4974100', '"cash_paise":18446744073709551616'),
        "an integer outside 64 bits", text_only=True, bundle=True),

    # ── 10: the state's date ─────────────────────────────────────────────────
    Row("no trade_date", 10, put("trade_date", None), "trade_date must be a YYYY-MM-DD date"),
    Row("the bundle's own day", 10, put("trade_date", "2026-01-13"),
        "trade_date 2026-01-13 must be before the bundle's first day 2026-01-13"),
    Row("a day years ahead", 10, put("trade_date", "2030-01-01"),
        "trade_date 2030-01-01 must be before the bundle's first day 2026-01-13"),
    Row("a date that does not exist", 10, put("trade_date", "2026-02-30"), "trade_date must be a YYYY-MM-DD date"),
    Row("a date as a number", 10, put("trade_date", 20260112), "trade_date must be a YYYY-MM-DD date"),
    Row("a date with a tail", 10, put("trade_date", "2026-01-12T00:00"), "trade_date must be a YYYY-MM-DD date"),

    # ── 11: ids and intervals against the bundle ─────────────────────────────
    Row("a position on an instrument the bundle lacks", 11, put("ledger.positions.1.instrument_id", 999),
        "ledger position.instrument_id 999 is not in the bundle"),
    Row("a live order on an instrument the bundle lacks", 11, live(instrument_id=999),
        "pending_orders.instrument_id 999 is not in the bundle"),
    Row("a tick subscription outside the bundle", 11, put("subscriptions.ticks", [1, 999]),
        "subscriptions.ticks 999 is not in the bundle"),
    Row("a bar subscription outside the bundle", 11, put("subscriptions.bars", {"999": [60]}),
        "subscriptions.bars key '999' is not in the bundle"),
    Row("a bar subscription with interval 0", 11, put("subscriptions.bars.1", [0, 60]),
        "subscriptions.bars interval_s must be positive"),
    Row("a bar subscription with a negative interval", 11, put("subscriptions.bars.1", [-60]),
        "subscriptions.bars interval_s must be positive"),
    Row("a bar subscription key with a leading zero", 11, put("subscriptions.bars", {"01": [60]}),
        "subscriptions.bars key '01' is not an instrument id"),
    Row("subscriptions.bars is a list", 11, put("subscriptions.bars", []), "subscriptions.bars must be an object"),
    Row("a market entry outside the bundle", 11, edit(lambda st: st["market"].update({"999": st["market"]["1"]})),
        "market key '999' is not in the bundle"),
    Row("a market key with a leading zero", 11, edit(lambda st: st["market"].update({"07": st["market"].pop("7")})),
        "market key '07' is not an instrument id"),
    Row("a market key that is no number", 11, edit(lambda st: st["market"].update({"x": st["market"]["1"]})),
        "market key 'x' is not an instrument id"),
    Row("a market min_interval of 0", 11, put("market.2.min_interval", 0), "market.min_interval must be positive"),
    Row("bar history outside the bundle", 11, edit(lambda st: st["bar_history"].update({"999:60": []})),
        "bar_history key '999:60' is not in the bundle"),
    Row("bar history with interval 0", 11, edit(lambda st: st["bar_history"].update({"2:0": []})),
        "bar_history key '2:0' is not <instrument_id>:<interval_s>"),
    Row("a bar history key without an interval", 11, edit(lambda st: st["bar_history"].update({"2": []})),
        "bar_history key '2' is not <instrument_id>:<interval_s>"),
    Row("bar history without a market entry", 11, edit(lambda st: _market_without(st, "7")),
        "bar_history key '7:60' has no market entry"),
    Row("a built bar outside the bundle", 11, edit(lambda st: _built_bar(st, iid=999)),
        "built bar instrument_id 999 is not in the bundle"),
    Row("a built bar with interval 0", 11, edit(lambda st: _built_bar(st, isec=0)),
        "built bar interval_s must be positive"),
    Row("a delivery done on an instrument the bundle lacks", 11, put("dp_done", [[999, "2026-01-12"]]),
        "dp_done instrument_id 999 is not in the bundle"),
    Row("a settlement price of an instrument the bundle lacks", 11, put("fsp_seen", [[999, "2026-01-12", 5]]),
        "fsp_seen underlying_id 999 is not in the bundle"),

    # ── 13: the low findings ─────────────────────────────────────────────────
    Row("a negative charge_row", 13, put("counters.charge_row", -5), "counters.charge_row must not be negative"),
    Row("an even slippage increment", 13, put("rng.slippage", [1, 4]), "rng.slippage inc must be odd"),
    Row("an even strategy increment", 13, put("rng.strategy.1", 0), "rng.strategy inc must be odd"),
    Row("a negative rng state", 13, put("rng.slippage.0", -1),
        "rng.slippage state must be a non-negative 64-bit integer"),
    Row("an rng row of three", 13, put("rng.slippage", [1, 3, 5]), "rng.slippage must be a list of 2"),
    Row("a NUL and more after the state", 13, lambda st, text: text + "\x00} this is not json {{{",
        "the text has a NUL character", text_only=True),
    Row("a NUL inside a string", 13, replace('"strategy":{}', '"strategy":{"x":"a\x00b"}'),
        "the text has a NUL character", text_only=True),
    Row("bar history going back in time", 13,
        edit(lambda st: _history(st, lambda rows: rows.__setitem__(1, [rows[0][0] - 60, *rows[1][1:]]))),
        "bar_history 2:60: start_ns must rise from row to row"),
    Row("two bars with one start time", 13,
        edit(lambda st: _history(st, lambda rows: rows.insert(1, list(rows[0])))),
        "bar_history 2:60: start_ns must rise from row to row"),
    Row("a bar with a negative price", 13,
        edit(lambda st: _history(st, lambda rows: rows[3].__setitem__(1, -100))), "bar open must not be negative"),
    Row("a bar with a negative volume", 13,
        edit(lambda st: _history(st, lambda rows: rows[3].__setitem__(5, -1))), "bar volume must not be negative"),
    Row("a built bar with a negative value", 13, edit(lambda st: _built_bar(st, values=[100, 110, -90, 105, 10, 0])),
        "built bar value must not be negative"),
    Row("a built bar published inside the saved day", 13, edit(lambda st: _built_bar(st, start=MIDNIGHT - 120 * 10**9)),
        "built bar 2 60 is not after the saved day 2026-01-12"),
    Row("one built bar listed twice", 13, edit(lambda st: (_built_bar(st), _built_bar(st))),
        "built bar 2 60 1768242600000000000 is listed twice"),
    Row("one settlement price listed twice", 13, put("fsp_seen", [[3, "2026-01-12", 5], [3, "2026-01-12", 6]]),
        "fsp_seen 3 2026-01-12 is listed twice"),
    Row("a hostile unknown key is escaped in the message", 13, lambda st, text: _keys_first(st),
        r"state has unknown keys ['x\'\x0a\x1b[31mAAAA"),
    Row("a hostile status is escaped in the message", 13, final(6, status="A'\nB\\\x7f "),
        r"final_orders: unknown status 'A\'\x0aB\\\x7f\xe2\x80\xa8'"),
    Row("a text past the size limit", 13, lambda st, text: text + " " * (MAX_BYTES + 1 - len(text)),
        "the text is larger than 268435456 bytes", text_only=True),

    # ── negative market values (nothing the engine writes: a negative price crashes a run) ──────────────────
    Row("a negative market ltp", 8, put("market.2.ltp", -1), "market.ltp must not be negative"),
    Row("a negative market bid", 8, put("market.2.bid", -1), "market.bid must not be negative"),
    Row("a negative market ask", 8, put("market.2.ask", -1), "market.ask must not be negative"),
    Row("a negative market oi", 8, put("market.2.oi", -1), "market.oi must not be negative"),
    Row("a negative snapshot ltp", 8, put("market.2.s0", snapshot(ltp=-1)), "market snapshot.ltp must not be negative"),
    Row("a negative snapshot volume", 8, put("market.2.s0", snapshot(volume=-1)),
        "market snapshot.volume must not be negative"),
    Row("a negative snapshot atp", 8, put("market.2.s1", snapshot(atp=-1)), "market snapshot.atp must not be negative"),
    Row("a negative snapshot high", 8, put("market.2.s1", snapshot(high=-1)),
        "market snapshot.high must not be negative"),
    Row("a negative snapshot low", 8, put("market.2.s1", snapshot(low=-1)), "market snapshot.low must not be negative"),
    Row("a snapshot with a negative time is fine", 8, put("market.2.s0", snapshot(t=-1)), None),
    Row("a negative band lower", 8, put("market.2.band", [-1, 5]), "market.band lower must not be negative"),
    Row("a negative band upper", 8, put("market.2.band", [1, -5]), "market.band upper must not be negative"),
    Row("a negative book price", 8, put("market.2.book", book(0, -1)), "market.book price must not be negative"),
    Row("a negative book quantity", 8, put("market.2.book", book(1, -1)), "market.book qty must not be negative"),

    # ── shapes of the remaining parts ────────────────────────────────────────
    Row("strategy_enabled as a number", 6, put("strategy_enabled", 1), "strategy_enabled must be a boolean"),
    Row("a pause reason that is a number", 6, put("pause_reasons", [5]), "pause_reasons must be a string"),
    Row("queued_notifications is an object", 6, put("queued_notifications", {}),
        "queued_notifications must be a list"),
    Row("a delivery row of three", 6, put("dp_done", [[2, "2026-01-12", 1]]), "dp_done row must be a list of 2"),
    Row("a market book of nine levels", 6, put("market.2.book", [[1, 1, 1]] * 9), "market.book must be a list of 10"),
    Row("a market band of three", 6, put("market.2.band", [1, 2, 3]), "market.band must be a list of 2"),
    Row("a market snapshot with an unknown key", 6,
        put("market.2.s0", {"t": 1, "ltp": 1, "volume": 1, "atp": 1, "high": 1, "low": 1, "open": 1}),
        "market snapshot has unknown keys ['open']"),
    Row("a market price as text", 6, put("market.2.ltp", "100155"), "market.ltp must be an integer"),
    Row("a market day that is no date", 6, put("market.2.day", "yesterday"), "market.day must be a YYYY-MM-DD date"),
)

# The rows whose refusal, before ST 5.1, came as a hang or as a dead process (creview H2, H3, H4): the brief wants
# them in the table by name.
HANG_ROWS = ("a timer with a negative period (hang)", "a timer period near INT64_MAX (hang)",
             "a huge next_order with a live order runs")
CRASH_ROWS = ("queued_notifications nested 5,000 deep (crash)", "strategy nested 20,000 deep (crash)",
              "queued_notifications nested 100,000 deep, as a bundle's state would hold it (crash)")
