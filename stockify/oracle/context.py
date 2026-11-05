"""The oracle's `Context` (A2 §4.2): what a strategy can see and do."""
from __future__ import annotations

from datetime import date

from stockify import numerics, strategy as S, timeutil


class _Rng:
    def __init__(self, gen):  # noqa: ANN001
        self._g = gen

    def random(self) -> float:
        return self._g.random()

    def below(self, n: int) -> int:
        return self._g.below(n)


class Context:
    def __init__(self, engine):  # noqa: ANN001
        self._e = engine
        self.params = dict(engine.bundle["strategy"].get("params", {}))
        self._rng = _Rng(engine.strategy_rng)

    # time and phases
    def now(self) -> int:
        return self._e.now

    def today(self) -> date:
        return timeutil.ns_to_ist(self._e.now).date()

    def phase(self, iid: int) -> str:
        e = self._e
        return e.phase_at(e.group_of(iid), e.now)

    # data
    def subscribe_ticks(self, iid: int) -> None:
        self._e.sub_ticks.add(iid)

    def subscribe_bars(self, iid: int, interval_s: int) -> None:
        self._e.sub_bars.setdefault(iid, set()).add(int(interval_s))

    def last_tick(self, iid: int) -> S.Tick | None:
        m = self._e.market.get(iid)
        if m is None or m.ltp is None:
            return None
        s1 = m.s1 or {}
        return S.Tick(iid, m.t, m.ltp, s1.get("volume", 0), s1.get("atp", 0), m.bid, m.ask, self.stale(iid))

    def depth(self, iid: int) -> list | None:
        m = self._e.market.get(iid)
        return list(m.book) if m and m.book else None

    def stale(self, iid: int) -> bool:
        m = self._e.market.get(iid)
        if m is None or m.t is None:
            return True
        return self._e.now - m.t > self._e.bundle["pricing"]["stale_after_ms"] * 1_000_000

    def bars(self, iid: int, interval_s: int, n: int) -> list[S.Bar]:
        m = self._e.market.get(iid)
        rows = (m.bars.get(int(interval_s), []) if m else [])[-n:]
        return [S.Bar(iid, interval_s, r[0], r[0] + interval_s * 1_000_000_000, r[1], r[2], r[3], r[4], r[5], r[6] or 0)
                for r in rows]

    def _closes(self, iid: int, interval_s: int) -> list[int]:
        m = self._e.market.get(iid)
        return [r[4] for r in (m.bars.get(int(interval_s), []) if m else [])]

    def sma(self, iid: int, interval_s: int, n: int) -> float:
        return S.sma(self._closes(iid, interval_s), n)

    def ema(self, iid: int, interval_s: int, n: int) -> float:
        return S.ema(self._closes(iid, interval_s), n)

    def rsi(self, iid: int, interval_s: int, n: int) -> float:
        return S.rsi(self._closes(iid, interval_s), n)

    def atr(self, iid: int, interval_s: int, n: int) -> float:
        m = self._e.market.get(iid)
        rows = m.bars.get(int(interval_s), []) if m else []
        return S.atr([(r[2], r[3], r[4]) for r in rows], n)

    def session_vwap(self, iid: int) -> float:
        """Bars only for now: Σ ((H + L + C) // 3 · V) / Σ V since today's CONTINUOUS start (A2 §4.3)."""
        e = self._e
        m = e.market.get(iid)
        if m is None or not m.bars:
            return S.NAN
        d = e._date(e.now)
        cs = e.continuous_start(e.group_of(iid, d), d)
        isec = min(m.bars)
        pv = v = 0
        for r in m.bars[isec]:
            if cs is not None and r[0] >= cs and e._date(r[0]) == d:
                pv += (r[2] + r[3] + r[4]) // 3 * r[5]
                v += r[5]
        return float(pv) / float(v) if v else S.NAN

    # orders
    def place(self, instrument: int, side: str, qty: int, product: str, type: str, price: int = 0, trigger: int = 0,  # noqa: A002
              validity: str = "DAY", protection="AUTO", tag: str = "") -> int:  # noqa: ANN001
        o = self._e.place(int(instrument), side, int(qty), product, type, int(price), int(trigger), validity,
                          protection, tag)
        return o.order_id

    def modify(self, order_id: int, **changes) -> bool:  # noqa: ANN003
        return self._e.modify(order_id, changes)

    def cancel(self, order_id: int) -> bool:
        return self._e.cancel(order_id)

    def order(self, order_id: int):  # noqa: ANN201
        from stockify.oracle.orders import OrderView
        o = self._e.book.orders.get(order_id)
        return OrderView.of(o) if o else None

    def position(self, iid: int, product: str) -> S.PositionView | None:
        p = self._e.book.ledger.position(iid, product)
        return S.PositionView(p.instrument_id, p.product, p.qty, p.avg_price, p.realised) if p else None

    def positions(self) -> list[S.PositionView]:
        out = []
        for p in sorted(self._e.book.ledger.positions.values(), key=lambda p: (p.instrument_id, p.product)):
            if p.q:
                out.append(S.PositionView(p.instrument_id, p.product, p.qty, p.avg_price, p.realised))
        return out

    def funds(self) -> S.Funds:
        return self._e.funds()

    # timers
    def schedule_at(self, t_ns: int) -> int:
        e = self._e
        if t_ns <= e.now:
            raise ValueError("schedule_at needs a time after now()")
        tid = e.next_timer
        e.next_timer += 1
        e.timers[tid] = (t_ns, 0)
        e.push(t_ns, 8, "TIMER", tid)
        return tid

    def schedule_every(self, period_ns: int, first_ns: int) -> int:
        e = self._e
        if first_ns <= e.now or period_ns <= 0:
            raise ValueError("schedule_every needs first_ns after now() and a positive period")
        tid = e.next_timer
        e.next_timer += 1
        e.timers[tid] = (first_ns, period_ns)
        e.push(first_ns, 8, "TIMER", tid)
        return tid

    def cancel_timer(self, timer_id: int) -> None:
        self._e.timers.pop(timer_id, None)

    # misc
    def log(self, level: str, msg: str) -> None:
        self._e.log(level.upper(), str(msg))

    def rng(self) -> _Rng:
        return self._rng

    def _rows(self) -> list[dict]:
        return self._e.bundle["instruments"]

    def resolve(self, symbol: str) -> int:
        exchange, _, name = symbol.rpartition(":")
        for r in self._rows():
            if r["tradingsymbol"] == name and (not exchange or r["exchange"] == exchange):
                return r["id"]
        raise KeyError(f"{symbol} is not in this run's bundle")

    def spec(self, iid: int) -> dict | None:
        return self._e.spec(iid, self._e._date(self._e.now))

    def expiries(self, underlying: str, kind: str) -> list[str]:
        d = self._e._date(self._e.now)
        kinds = ("CE", "PE") if kind.upper() == "OPT" else (kind.upper(),)
        return sorted({r["expiry"] for r in self._rows() if r["underlying"] == underlying and r["kind"] in kinds
                       and (r.get("actual_expiry") or r["expiry"]) >= d})

    def futures_chain(self, underlying: str) -> list[int]:
        rows = [r for r in self._rows() if r["underlying"] == underlying and r["kind"] == "FUT"]
        return [r["id"] for r in sorted(rows, key=lambda r: r["expiry"])]

    def chain(self, underlying: str, expiry: str) -> list[dict]:
        rows = [r for r in self._rows() if r["underlying"] == underlying and r["expiry"] == expiry
                and r["kind"] in ("CE", "PE")]
        return [{"instrument_id": r["id"], "kind": r["kind"], "strike_paise": r["strike_paise"]}
                for r in sorted(rows, key=lambda r: (r["strike_paise"], r["kind"]))]

    rupees = staticmethod(S.rupees)
    paise = staticmethod(numerics.to_paise)
