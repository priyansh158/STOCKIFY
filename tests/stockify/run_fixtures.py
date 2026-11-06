"""Strategies for the runner tests (test_runs_launch.py): loaded by ref, so they pass the A2 §4.4 source check."""
from __future__ import annotations

from stockify.strategy import Strategy


class FailOnSecondBar(Strategy):
    def on_start(self, ctx):  # noqa: ANN001
        self.iid = ctx.resolve(ctx.params["symbol"])
        self.seen = 0
        ctx.subscribe_bars(self.iid, 86400)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        self.seen += 1
        if self.seen == 2:
            raise ZeroDivisionError("planned failure on the second bar")
