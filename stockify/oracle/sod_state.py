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

import json

from stockify import numerics
from stockify.oracle.ledger import Ledger, Position
from stockify.oracle.orders import FINAL, Order

SCHEMA = "stockify.sod_state/1"

# The top-level keys (A1 §6.4 lists them in this order; the JSON is canonical, so they serialise sorted).
KEYS = ("schema", "trade_date", "cash_paise", "ledger", "cnc_releases", "pending_orders", "final_orders", "dp_done",
        "timers", "built_bars", "counters", "rng", "subscriptions", "strategy_enabled", "pause_reasons",
        "queued_notifications", "fsp_seen", "marks", "market", "bar_history", "strategy")

POSITION_FIELDS = ("instrument_id", "product", "style", "q", "d", "c", "realised", "vm", "last_mark", "mark_source")
ORDER_FIELDS = ("order_id", "parent_id", "instrument_id", "side", "qty", "product", "type", "price", "trigger",
                "validity", "protection", "tag", "t_submit", "t_activate", "status", "filled_qty", "fill_value",
                "reason", "limit", "from_market", "triggered", "protection_bp", "reserved", "first_eligible_done",
                "trade_date", "charged", "eod_after")
COUNTERS = ("next_order", "next_fill", "next_timer", "charge_row", "event_no")
MARKET_FIELDS = ("ltp", "t", "s0", "s1", "bid", "ask", "book", "band", "oi", "min_interval", "day")
SNAPSHOT_FIELDS = ("t", "ltp", "volume", "atp", "high", "low")


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


def capture(engine) -> dict:  # noqa: ANN001
    """The state at the end of the engine's last processed day (or, before a run, the state it was loaded with)."""
    led: Ledger = engine.book.ledger
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
        "cnc_releases": [[d, led.unavailable[d]] for d in sorted(led.unavailable)],
        "pending_orders": [_order(o) for o in orders if o.status not in FINAL],
        "final_orders": [_order(o) for o in orders if o.status in FINAL],
        "dp_done": sorted([iid, d] for iid, d in engine.book.dp_done),
        "timers": _timers(engine),
        "built_bars": _built(engine),
        "counters": {"next_order": engine.next_order, "next_fill": engine.book.next_fill,
                     "next_timer": engine.next_timer, "charge_row": engine.charge_row_base + len(engine.book.charges),
                     "event_no": engine.event_no},
        "rng": {"slippage": [engine.book.rng.state, engine.book.rng.inc],
                "strategy": [engine.strategy_rng.state, engine.strategy_rng.inc]},
        "subscriptions": {"ticks": sorted(engine.sub_ticks),
                          "bars": {str(iid): sorted(v) for iid, v in sorted(engine.sub_bars.items())}},
        "strategy_enabled": engine.strategy_enabled,
        "pause_reasons": sorted(engine.pause_reasons),
        "queued_notifications": list(engine.queued_notifications),
        "fsp_seen": sorted([uid, d, px] for (uid, d), px in engine.fsp_seen.items()),
        "marks": {str(iid): px for iid, px in sorted(engine.underlying_last.items())},
        "market": {str(iid): _market(engine.market[iid]) for iid in sorted(engine.market)},
        "bar_history": bar_history,
        "strategy": engine.strategy.save_state(),
    }


# ── Restore ──────────────────────────────────────────────────────────────────

def _need(d: dict, keys, where: str) -> None:  # noqa: ANN001
    if not isinstance(d, dict):
        raise StateError(f"sod_state: {where} must be an object")
    for k in keys:
        if k not in d:
            raise StateError(f"sod_state: {where} is missing {k!r}")


def _snapshot(v):  # noqa: ANN001, ANN202
    if v is None:
        return None
    _need(v, SNAPSHOT_FIELDS, "market snapshot")
    return {k: v[k] for k in SNAPSHOT_FIELDS}


def restore(engine, state: dict) -> None:  # noqa: ANN001
    """Apply a start-of-day state to an engine that has not processed any event (A4 §1.10)."""
    from stockify.oracle.runner import BAR_BUILT, TIMER, Market
    _need(state, ("schema",), "state")
    if state["schema"] != SCHEMA:
        raise StateError(f"sod_state: schema must be {SCHEMA!r}, got {state['schema']!r}")
    _need(state, KEYS, "state")
    led_doc = state["ledger"]
    _need(led_doc, ("capital_paise", "charges_total_paise", "reserved_paise", "positions"), "ledger")
    led = Ledger(state["cash_paise"], led_doc["capital_paise"], {}, led_doc["charges_total_paise"],
                 {d: v for d, v in state["cnc_releases"]}, led_doc["reserved_paise"])
    for p in led_doc["positions"]:
        _need(p, POSITION_FIELDS, "ledger position")
        led.positions[(p["instrument_id"], p["product"])] = Position(**{f: p[f] for f in POSITION_FIELDS})
    book = engine.book
    book.ledger = led
    orders = []
    for group in ("pending_orders", "final_orders"):
        for o in state[group]:
            _need(o, ORDER_FIELDS, group)
            orders.append(Order(**{f: o[f] for f in ORDER_FIELDS}))
    book.orders = {o.order_id: o for o in sorted(orders, key=lambda o: o.order_id)}
    engine.prior_final = {o.order_id for o in orders if o.status in FINAL}
    book.dp_done = {(iid, d) for iid, d in state["dp_done"]}
    c = state["counters"]
    _need(c, COUNTERS, "counters")
    engine.next_order, book.next_fill, engine.next_timer = c["next_order"], c["next_fill"], c["next_timer"]
    engine.charge_row_base, engine.event_no = c["charge_row"], c["event_no"]
    live = [o.order_id for o in orders if o.status not in FINAL]
    engine.min_live = min(live) if live else engine.next_order
    rng = state["rng"]
    _need(rng, ("slippage", "strategy"), "rng")
    book.rng.state, book.rng.inc = rng["slippage"]
    engine.strategy_rng.state, engine.strategy_rng.inc = rng["strategy"]
    for tid, t, period in state["timers"]:
        engine.timers[tid] = (t, period)
        engine.push(t, TIMER, "TIMER", tid)
    for iid, isec, start, *bar in state["built_bars"]:
        engine.built.setdefault((iid, isec), {})[start] = list(bar)
        engine.push(start + isec * 1_000_000_000, BAR_BUILT, "BAR_BUILT", (iid, isec, start))
    subs = state["subscriptions"]
    _need(subs, ("ticks", "bars"), "subscriptions")
    engine.sub_ticks = set(subs["ticks"])
    engine.sub_bars = {int(iid): set(v) for iid, v in subs["bars"].items()}
    engine.strategy_enabled = state["strategy_enabled"]
    engine.pause_reasons = set(state["pause_reasons"])
    engine.queued_notifications = list(state["queued_notifications"])
    engine.fsp_seen = {(uid, d): px for uid, d, px in state["fsp_seen"]}
    engine.underlying_last = {int(iid): px for iid, px in state["marks"].items()}
    engine.market = {}
    for iid in sorted(state["market"], key=int):
        m = state["market"][iid]
        _need(m, MARKET_FIELDS, "market")
        engine.market[int(iid)] = Market(
            ltp=m["ltp"], t=m["t"], s0=_snapshot(m["s0"]), s1=_snapshot(m["s1"]), bid=m["bid"], ask=m["ask"],
            book=[tuple(level) for level in m["book"]] if m["book"] is not None else None,
            band=tuple(m["band"]) if m["band"] is not None else None, oi=m["oi"], min_interval=m["min_interval"],
            day=m["day"])
    for key in sorted(state["bar_history"]):
        iid, _, isec = key.partition(":")
        m = engine.market.setdefault(int(iid), Market())
        m.bars[int(isec)] = [tuple(r) for r in state["bar_history"][key]]
    engine.state_date = state["trade_date"]
    engine.strategy.load_state(state["strategy"])


def loads(text: str) -> dict:
    try:
        state = json.loads(text)
    except json.JSONDecodeError as e:
        raise StateError(f"sod_state: invalid JSON: {e}") from e
    return state


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
