"""The `script` strategy for golden scenarios (A3 §2).

It subscribes to every instrument in the bundle and performs listed actions
when the Nth market-data event (1-based, in engine order) is delivered, or at
a given engine time. Goldens therefore test the engine, not strategy logic.

    actions:
      - { on_event: 1, place: { instrument: 1, side: BUY, qty: 10, product: CNC, type: MARKET } }
      - { at_ns: 1772424390000000000, cancel: 1 }
      - { on_event: 3, modify: { order_id: 2, price: 10100 } }
"""
from __future__ import annotations

from stockify.strategy import Strategy


class Script(Strategy):
    def __init__(self, actions: list[dict]):
        self.actions = actions
        self.n = 0
        self.timers: dict[int, list[dict]] = {}

    def on_start(self, ctx):  # noqa: ANN001
        for row in ctx._rows():
            ctx.subscribe_ticks(row["id"])
            for isec in (60, 180, 300, 600, 900, 1800, 3600, 86400):
                ctx.subscribe_bars(row["id"], isec)
        by_time: dict[int, list[dict]] = {}
        for a in self.actions:
            if "at_ns" in a:
                by_time.setdefault(int(a["at_ns"]), []).append(a)
        for t, acts in sorted(by_time.items()):
            self.timers[ctx.schedule_at(t)] = acts

    def _do(self, ctx, a: dict) -> None:  # noqa: ANN001
        if "place" in a:
            p = dict(a["place"])
            ctx.place(p.pop("instrument"), p.pop("side"), p.pop("qty"), p.pop("product"), p.pop("type"), **p)
        if "cancel" in a:
            ctx.cancel(int(a["cancel"]))
        if "modify" in a:
            m = dict(a["modify"])
            ctx.modify(int(m.pop("order_id")), **m)

    def _market(self, ctx) -> None:  # noqa: ANN001
        self.n += 1
        for a in self.actions:
            if a.get("on_event") == self.n:
                self._do(ctx, a)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        self._market(ctx)

    def on_tick(self, ctx, tick):  # noqa: ANN001
        self._market(ctx)

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        for a in self.timers.pop(timer_id, []):
            self._do(ctx, a)
