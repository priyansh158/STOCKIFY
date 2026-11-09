"""A wrapper that runs another strategy unchanged and logs the margin it ties up (M4.13 comparison report).

equity.csv records margin only at each day's close, and an intraday option strategy is flat by then; the
comparison needs the intraday peak. This wrapper forwards every hook to the strategy named in its params
(`inner_ref`, "module:Class"; the other params pass through untouched) and after each fill and at each session end
logs `margin_watch blocked=<paise>` (ctx.funds().blocked). Leading underscore: not a catalogue strategy.
"""
from __future__ import annotations

from stockify import strategy as S

LOG_PREFIX = "margin_watch blocked="


class _InnerParams:
    """ctx with `params` minus inner_ref, so the wrapped strategy sees exactly its own params."""

    def __init__(self, ctx, params: dict):  # noqa: ANN001
        self._ctx, self.params = ctx, params

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._ctx, name)


class MarginWatch(S.Strategy):
    def on_start(self, ctx):  # noqa: ANN001
        params = dict(ctx.params)
        self.inner_ref = params.pop("inner_ref")
        self.inner = S.load(self.inner_ref)
        self._params = params
        self.inner.on_start(self._wrap(ctx))

    # A paper session's day-to-day state (A2 §4.1): the wrapped strategy is re-loaded by its ref and given its own
    # saved state; the rest is the default.
    def save_state(self) -> dict:
        return {"inner": self.inner.save_state(),
                "own": S.encode_state(self, {k: v for k, v in vars(self).items() if k != "inner"})}

    def load_state(self, state: dict) -> None:
        for name, value in S.decode_state(state["own"]).items():
            setattr(self, name, value)
        self.inner = S.load(self.inner_ref)
        self.inner.load_state(state["inner"])

    def _wrap(self, ctx):  # noqa: ANN001, ANN202
        return _InnerParams(ctx, self._params)

    def _log_margin(self, ctx) -> None:  # noqa: ANN001
        ctx.log("info", f"{LOG_PREFIX}{ctx.funds().blocked}")

    def on_bar(self, ctx, bar):  # noqa: ANN001
        self.inner.on_bar(self._wrap(ctx), bar)

    def on_tick(self, ctx, tick):  # noqa: ANN001
        self.inner.on_tick(self._wrap(ctx), tick)

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        self.inner.on_timer(self._wrap(ctx), timer_id)

    def on_session_phase(self, ctx, change):  # noqa: ANN001
        self.inner.on_session_phase(self._wrap(ctx), change)

    def on_order_update(self, ctx, order):  # noqa: ANN001
        self.inner.on_order_update(self._wrap(ctx), order)

    def on_fill(self, ctx, fill):  # noqa: ANN001
        self.inner.on_fill(self._wrap(ctx), fill)
        self._log_margin(ctx)

    def on_session_end(self, ctx):  # noqa: ANN001
        self.inner.on_session_end(self._wrap(ctx))
        self._log_margin(ctx)
