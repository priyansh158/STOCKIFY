"""HD 3.1: "what does it say now". A strategy's own `on_bar`, replayed over the bars through a light context.

The desk asks every tested strategy the same question the backtest answers bar by bar: would it be holding the
stock now, and at what price would it get out? Rather than a second copy of each rule, `now()` loads the strategy
exactly as a backtest does (`stockify.strategy.load`, source guard included) and feeds it the bars through
`ReplayContext`, which offers only what the desk's strategies use. Orders fill at once at the bar's close. The
oracle fills them with the next bar instead, and a daily bar reaches the strategy at its end, so the replay over
bars[:k] (last bar: day k - 1, counting from 0) matches the oracle's end-of-day position on day k + 1.

The live recompute (`now_tail`) replays only the last `window(ref, params)` bars: each voter decides from a fixed
look-back (the swing rules' LOOKBACK, the golden cross's slow average), so a replay that starts that many bars back,
plus a few for the bar before the last, says the same as one over the whole history.

Bars are the bar cache's adjusted basis (`stockify.data.history`), so a split or bonus never looks like a crash.
Money is paise, time UTC epoch ns.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass

from stockify import strategy as S
from stockify import timeutil
from stockify.oracle.context import _note, _plot_args, _protection, _whole

DAY_S = 86_400
DAY_NS = DAY_S * timeutil.NS_PER_S
# What the replay starts with. Only the sign of the position matters to the desk; the sum is large enough that
# the strategies' own sizing buys at least one share of any NSE stock.
CAPITAL_PAISE = 10_000_000_000                       # ₹10 crore
PKG = "stockify.strategies."

# The strategies whose "now" votes on a holding, with their params beyond the stock, and the baseline (never voted).
VOTERS: tuple[tuple[str, dict], ...] = (
    (PKG + "rsi_pullback:RsiPullback", {}),
    (PKG + "macd_cross:MacdCross", {}),
    (PKG + "bollinger_reversion:BollingerReversion", {}),
    (PKG + "donchian_breakout:DonchianBreakout", {}),
    (PKG + "supertrend:Supertrend", {}),
    (PKG + "ema_trend:EmaTrend", {}),
    (PKG + "chandelier_exit:ChandelierExit", {}),
    (PKG + "sma_cross:SmaCross", {"fast": 50, "slow": 200}),
)
BASELINE: tuple[str, dict] = (PKG + "buy_and_hold:BuyAndHold", {})

NOT_ON_DESK = "not available on the desk"
TAIL_SPARE = 5                 # bars a tail replay keeps beyond a rule's look-back


@dataclass(frozen=True)
class Signal:
    ref: str
    display_name: str
    in_position: bool                 # holding at the last bar's close
    buying_today: bool                # the last bar placed a BUY
    exit_level_paise: int | None      # the `exit_level` plotted on the last bar, if any
    note: str                         # the note of the last order placed in the replay ("" if none)


def display_name(ref: str, cls: type, params: dict) -> str:
    """The plain name: the class's DISPLAY_NAME, except SMA Crossover at 50/200, shown as the golden cross."""
    if ref == PKG + "sma_cross:SmaCross":
        fast, slow = params.get("fast"), params.get("slow")
        if str(fast) == "50" and str(slow) == "200":
            return "Golden Cross (50/200)"
        return f"SMA Crossover ({fast}/{slow})"
    return str(getattr(cls, "DISPLAY_NAME", ref.rpartition(":")[2]))


class _Position:
    __slots__ = ("qty", "avg", "realised")

    def __init__(self) -> None:
        self.qty, self.avg, self.realised = 0, 0, 0


def _unsupported(name: str):  # noqa: ANN202
    def call(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202, ARG001
        raise NotImplementedError(f"ctx.{name} is {NOT_ON_DESK}")
    call.__name__ = name
    return call


class ReplayContext:
    """The strategy context of a desk replay: one instrument, its bars so far, instant fills at the close.

    Mirrors the signatures of the oracle's Context (stockify/oracle/context.py) for the calls the swing and bar
    strategies make; the rest raise NotImplementedError."""

    def __init__(self, params: dict, instrument_id: int, *, capital_paise: int = CAPITAL_PAISE,
                 spec: dict | None = None) -> None:
        self.params = dict(params)
        self._iid = int(instrument_id)
        self._spec = dict(spec) if spec is not None else {"lot": 1}
        self._bars: list[S.Bar] = []
        self._by_key: dict[tuple[int, int], list[S.Bar]] = {}    # (instrument, interval_s) -> its bars, in order
        self._now = 0
        self._cash = int(capital_paise)
        self._positions: dict[tuple[int, str], _Position] = {}
        self._subs: set[tuple[int, int]] = set()
        self._next_order = 1
        self.plots: dict[str, tuple[int, str, int, int]] = {}    # name -> (value, kind, instrument, t_ns)
        self.orders: list[dict] = []                              # every order placed, in order
        self.logs: list[tuple[str, str]] = []

    # driving the replay
    def advance(self, bar: S.Bar) -> None:
        """The next bar has closed: it joins the history and the clock moves to its end."""
        self._bars.append(bar)
        self._by_key.setdefault((bar.instrument, bar.interval_s), []).append(bar)
        self._now = bar.t_ns

    def subscribed(self, bar: S.Bar) -> bool:
        return (bar.instrument, bar.interval_s) in self._subs

    # time
    def now(self) -> int:
        return self._now

    def today(self):  # noqa: ANN201
        return timeutil.ns_to_ist(self._now).date()

    # instruments
    def resolve(self, symbol: str) -> int:
        if symbol == self.params.get("symbol"):
            return self._iid
        raise KeyError(f"{symbol} is not the stock this replay is for")

    def spec(self, iid: int) -> dict | None:
        return dict(self._spec) if iid == self._iid else None

    # data
    def subscribe_bars(self, iid: int, interval_s: int) -> None:
        self._subs.add((int(iid), int(interval_s)))

    def _series(self, iid: int, interval_s: int) -> list[S.Bar]:
        """The bars of one series so far (kept as they arrive: no walk over the whole history per call). Callers
        must not change the list."""
        return self._by_key.get((int(iid), int(interval_s)), [])

    def bars(self, iid: int, interval_s: int, n: int) -> list[S.Bar]:
        return self._series(iid, interval_s)[-n:] if n > 0 else []

    def _closes(self, iid: int, interval_s: int) -> list[int]:
        return [b.close for b in self._series(iid, interval_s)]

    def sma(self, iid: int, interval_s: int, n: int) -> float:
        return S.sma(self._closes(iid, interval_s), n)

    def ema(self, iid: int, interval_s: int, n: int) -> float:
        return S.ema(self._closes(iid, interval_s), n)

    def rsi(self, iid: int, interval_s: int, n: int) -> float:
        return S.rsi(self._closes(iid, interval_s), n)

    def atr(self, iid: int, interval_s: int, n: int) -> float:
        return S.atr([(b.high, b.low, b.close) for b in self._series(iid, interval_s)], n)

    # orders and the book
    def place(self, instrument: int, side: str, qty: int, product: str, type: str, price: int = 0, trigger: int = 0,  # noqa: A002
              validity: str = "DAY", protection="AUTO", *, tag: str = "", note: str = "") -> int:  # noqa: ANN001
        qty, price, trigger = _whole("qty", qty), _whole("price", price), _whole("trigger", trigger)
        _protection(protection)
        note = _note(note)
        if type != "MARKET":
            raise NotImplementedError(f"ctx.place with a {type} order is {NOT_ON_DESK} (market orders only)")
        if side not in ("BUY", "SELL"):
            raise ValueError(f'ctx.place: side must be "BUY" or "SELL", not {side!r}')
        if qty <= 0:
            raise ValueError(f"ctx.place: qty must be positive, not {qty}")
        if int(instrument) != self._iid or not self._bars:
            raise ValueError(f"ctx.place: no price for instrument {instrument} in this replay")
        fill = self._bars[-1].close
        p = self._positions.setdefault((self._iid, product), _Position())
        signed = qty if side == "BUY" else -qty
        if p.qty == 0 or (p.qty > 0) == (signed > 0):                 # opening or adding
            p.avg = (p.avg * abs(p.qty) + fill * qty) // (abs(p.qty) + qty)
        else:                                                         # reducing (or flipping)
            closed = min(abs(p.qty), qty)
            p.realised += (fill - p.avg) * closed * (1 if p.qty > 0 else -1)
            if qty > closed:
                p.avg = fill
        p.qty += signed
        if p.qty == 0:
            p.avg = 0
        self._cash -= signed * fill
        oid = self._next_order
        self._next_order += 1
        self.orders.append({"order_id": oid, "t_ns": self._now, "side": side, "qty": qty, "product": product,
                            "price": fill, "tag": tag, "note": note})
        return oid

    def position(self, iid: int, product: str) -> S.PositionView | None:
        p = self._positions.get((int(iid), product))
        return S.PositionView(int(iid), product, p.qty, p.avg, p.realised) if p else None

    def funds(self) -> S.Funds:
        return S.Funds(available=self._cash, cash=self._cash, blocked=0, reserved=0)

    # outputs
    def plot(self, name: str, value: int, *, kind: str = "price", instrument: int = 0) -> None:
        name, value, kind, instrument = _plot_args(name, value, kind, instrument)
        self.plots[name] = (value, kind, instrument, self._now)

    def log(self, level: str, msg: str) -> None:
        self.logs.append((level.upper(), str(msg)))

    # everything else the oracle's context offers is not part of a desk replay
    phase = _unsupported("phase")
    subscribe_ticks = _unsupported("subscribe_ticks")
    last_tick = _unsupported("last_tick")
    depth = _unsupported("depth")
    stale = _unsupported("stale")
    session_vwap = _unsupported("session_vwap")
    modify = _unsupported("modify")
    cancel = _unsupported("cancel")
    order = _unsupported("order")
    positions = _unsupported("positions")
    schedule_at = _unsupported("schedule_at")
    schedule_every = _unsupported("schedule_every")
    cancel_timer = _unsupported("cancel_timer")
    rng = _unsupported("rng")
    expiries = _unsupported("expiries")
    futures_chain = _unsupported("futures_chain")
    chain = _unsupported("chain")
    _rows = _unsupported("_rows")

    rupees = staticmethod(S.rupees)
    paise = staticmethod(S.paise)


def _signal(ref: str, name: str, ctx: ReplayContext, bar: S.Bar, first_today: int) -> Signal:
    """Where the strategy stands right after `bar`; `first_today` is the index of the first order `bar` placed."""
    today = ctx.orders[first_today:]
    held = sum(p.qty for (iid, _), p in ctx._positions.items() if iid == bar.instrument)
    level = ctx.plots.get("exit_level")
    return Signal(
        ref=ref, display_name=name, in_position=held > 0,
        buying_today=any(o["side"] == "BUY" for o in today),
        exit_level_paise=level[0] if level is not None and level[3] == bar.t_ns else None,
        note=ctx.orders[-1]["note"] if ctx.orders else "")


def walk(ref: str, params: dict, bars: list[S.Bar], *, warm: int = 0) -> list[Signal]:
    """Replay `ref` (a "package.module:Class" strategy) with `params` over `bars`, oldest first: the Signal after
    each bar, so walk(...)[k - 1] == now(..., bars[:k]). One pass, for a day-by-day view of a rule. The first
    `warm` bars only join the history (the strategy sees them through ctx.bars, not through on_bar), and get no
    Signal."""
    st = S.load(ref)
    name = display_name(ref, type(st), params)
    if not bars:
        return []
    ctx = ReplayContext(params, bars[0].instrument)
    st.on_start(ctx)
    out = []
    for i, bar in enumerate(bars):
        ctx.advance(bar)
        if i < warm:
            continue
        first_today = len(ctx.orders)
        if ctx.subscribed(bar):
            st.on_bar(ctx, bar)
        out.append(_signal(ref, name, ctx, bar, first_today))
    return out


def now(ref: str, params: dict, bars: list[S.Bar]) -> Signal:
    """Replay `ref` with `params` over `bars`, oldest first, and say where it stands after the last one: holding
    or not, whether the last bar bought, the exit level it plotted on the last bar, and its last order's note."""
    if not bars:
        st = S.load(ref)
        return Signal(ref, display_name(ref, type(st), params), False, False, None,
                      "No daily bars for this stock yet.")
    return walk(ref, params, bars)[-1]


def window(ref: str, params: dict) -> int:
    """How many of the latest bars a tail replay of `ref` needs to say what a replay over all of them says: the
    rule's look-back (`LOOKBACK`, or the golden cross's slow average) plus TAIL_SPARE."""
    return _window(ref, *(None if params.get(k) is None else int(params[k]) for k in ("fast", "slow")))


@functools.lru_cache(maxsize=64)
def _window(ref: str, fast: int | None, slow: int | None) -> int:
    params = {k: v for k, v in (("fast", fast), ("slow", slow)) if v is not None}
    cls = type(S.load(ref))
    need = int(getattr(cls, "LOOKBACK", 0) or 0)
    for key in ("fast", "slow"):
        if key in params:
            need = max(need, int(params[key]))
    if ref == PKG + "sma_cross:SmaCross" and "slow" not in params:
        need = max(need, 200)
    return need + TAIL_SPARE


def now_tail(ref: str, params: dict, bars: list[S.Bar]) -> Signal:
    """`now` from only the last `window(ref, params)` bars: the live recompute's fast path. Those bars are the
    history; on_bar runs on the last TAIL_SPARE of them only, each with a full look-back behind it. The position,
    the buying-today flag and the exit level match the full replay; the note is the tail's own last order's."""
    if not bars:
        return now(ref, params, bars)
    tail = bars[-window(ref, params):]
    return walk(ref, params, tail, warm=max(0, len(tail) - TAIL_SPARE))[-1]


_QTY_LEAD = re.compile(r"^(?:Bought|Sold short|Sold|Flipped to (?:long|short)) [0-9,]+ \S+: ")


def plain_note(note: str) -> str:
    """A replay order's note without its lead ("Bought 1,39,391 NSE:X: "): the share count comes from the
    replay's notional book, not the owner's, so the desk never shows it. The reason starts with a capital."""
    m = _QTY_LEAD.match(note or "")
    if not m:
        return note or ""
    rest = note[m.end():]
    return rest[:1].upper() + rest[1:]


# ── Bars ─────────────────────────────────────────────────────────────────────

def daily_bars(instrument_id: int, *, until_ns: int | None = None) -> list[S.Bar]:
    """The bar cache's daily candles (adjusted basis) as strategy Bars, oldest first; `until_ns` bounds the START."""
    from stockify.data import history
    out = []
    for r in history.bars(instrument_id, "day", to_ns=until_ns):
        start = int(r["ts_ns"])
        out.append(S.Bar(instrument=int(instrument_id), interval_s=DAY_S, start_ns=start, t_ns=start + DAY_NS,
                         open=int(r["open_paise"]), high=int(r["high_paise"]), low=int(r["low_paise"]),
                         close=int(r["close_paise"]), volume=int(r["volume"] or 0), oi=int(r["oi"] or 0)))
    return out


def _day_start_ns(t_ns: int) -> int:
    """The start of the IST trading day holding t_ns (00:00 IST, as Kite stamps day candles), UTC epoch ns."""
    return timeutil.ist_ns(timeutil.ns_to_ist(t_ns).date(), "00:00")


def with_live_candle(bars: list[S.Bar], ltp_paise: int, t_ns: int, day_open_paise: int, day_high_paise: int,
                     day_low_paise: int) -> list[S.Bar]:
    """`bars` with today's partial day bar (built=True) from the live price: replacing a bar for today if the
    history already has one, else appended. The high and low take in the open and the live price. A copy; `bars`
    is not changed. No bars: no stock to build on, so [] back."""
    if not bars:
        return []
    start = _day_start_ns(t_ns)
    last = bars[-1]
    if start < last.start_ns:
        raise ValueError("the live price is older than the last daily bar")
    keep = bars[:-1] if start == last.start_ns else list(bars)
    volume, oi = (last.volume, last.oi) if start == last.start_ns else (0, 0)
    live = S.Bar(instrument=last.instrument, interval_s=DAY_S, start_ns=start, t_ns=start + DAY_NS,
                 open=int(day_open_paise), high=max(int(day_high_paise), int(ltp_paise), int(day_open_paise)),
                 low=min(int(day_low_paise), int(ltp_paise), int(day_open_paise)), close=int(ltp_paise),
                 volume=volume, oi=oi, built=True)
    return [*keep, live]
