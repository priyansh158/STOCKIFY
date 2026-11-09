"""Replay of a recorded strategy, for goldens saved by the fuzz shrinker (M3.12, `stockify.tools.fuzz`).

A recording is the list of context calls a strategy made, each tagged with the number of the callback it was
made in (0 = on_start, then 1, 2, … for every hook the engine delivered, in order). Replaying the same calls at
the same callback numbers reproduces the run exactly: the engine's behaviour depends only on the calls, never on
how the strategy chose them. The recording lives in the bundle's params:

    params: { calls: [ [k, "place", [1, "BUY", 10, "CNC", "LIMIT"], {"price": 10000}], … ] }

Methods replayed: subscribe_ticks, subscribe_bars, schedule_at, schedule_every, cancel_timer, place, modify,
cancel. Kept free of anything the strategy load check refuses (A2 §4.4), so a golden can name it by ref.
"""
from __future__ import annotations

from stockify.strategy import Strategy

# Each replayed call, by name: an explicit table, since the load check refuses getattr (LCB 5.4).
_CALLS = {
    "subscribe_ticks": lambda ctx: ctx.subscribe_ticks,
    "subscribe_bars": lambda ctx: ctx.subscribe_bars,
    "schedule_at": lambda ctx: ctx.schedule_at,
    "schedule_every": lambda ctx: ctx.schedule_every,
    "cancel_timer": lambda ctx: ctx.cancel_timer,
    "place": lambda ctx: ctx.place,
    "modify": lambda ctx: ctx.modify,
    "cancel": lambda ctx: ctx.cancel,
}
REPLAYED = tuple(_CALLS)


class Replay(Strategy):
    def on_start(self, ctx):  # noqa: ANN001
        self.k = 0
        self.by_k: dict[int, list] = {}
        for k, method, args, kwargs in ctx.params.get("calls", []):
            self.by_k.setdefault(int(k), []).append((method, list(args), dict(kwargs)))
        self._run(ctx)

    def _run(self, ctx) -> None:  # noqa: ANN001
        for method, args, kwargs in self.by_k.get(self.k, []):
            if method not in REPLAYED:
                raise ValueError(f"replay: {method} is not a replayed context call")
            _CALLS[method](ctx)(*args, **kwargs)

    def _next(self, ctx) -> None:  # noqa: ANN001
        self.k += 1
        self._run(ctx)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        self._next(ctx)

    def on_tick(self, ctx, tick):  # noqa: ANN001
        self._next(ctx)

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        self._next(ctx)

    def on_fill(self, ctx, fill):  # noqa: ANN001
        self._next(ctx)

    def on_order_update(self, ctx, order):  # noqa: ANN001
        self._next(ctx)

    def on_session_phase(self, ctx, change):  # noqa: ANN001
        self._next(ctx)

    def on_session_end(self, ctx):  # noqa: ANN001
        self._next(ctx)
