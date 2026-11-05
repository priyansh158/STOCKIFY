"""Which instruments the recorder subscribes to, and in which mode (M1.5; D-08).

Tiers (from `universe.json`, A1 §7.1):

* FULL (5-level depth, exchange timestamps): the indices, near/next index
  futures, option strikes within ±`n_full` of ATM, the equities and stock
  futures the owner intends to trade, and anything pinned (M5 pins open
  paper positions).
* QUOTE: chain strikes from `n_full + 1` out to `n_quote`, plus context
  equities. LTP mode is never used.

Option chains are centred on an ATM reference: the same-expiry future's price
when one exists, otherwise the index. A chain re-centres when ATM moves by at
least `recenter_threshold` strikes; new strikes are added at once, old ones
are removed only beyond `n_quote + hysteresis`. Pinned instruments are never
removed. The total stays within the per-connection cap (K-1): the farthest
QUOTE strikes go first. Every change is logged, because a missing packet only
means something if the instrument was known to be subscribed.

Expiry selectors: `weekly:k` is the k-th option expiry on or after the day;
`monthly:k` the k-th futures expiry (futures expire monthly) that also lists
options.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable

from stockify.daemon.clock import Clock
from stockify.data import instruments

FULL, QUOTE = "full", "quote"
log = logging.getLogger("stockify.daemon.subscriptions")


@dataclass(frozen=True)
class Sub:
    instrument_id: int
    token: int
    mode: str              # full | quote
    reason: str            # e.g. index, future:near, chain:NIFTY:weekly:0, equity, pin:<owner>


@dataclass
class Diff:
    subscribe: list[Sub] = field(default_factory=list)
    unsubscribe: list[Sub] = field(default_factory=list)
    mode_change: list[Sub] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.subscribe or self.unsubscribe or self.mode_change)


@dataclass
class Chain:
    key: str                      # NIFTY:weekly:0
    underlying: str
    expiry: str
    n_full: int
    n_quote: int
    recenter_threshold: int
    hysteresis: int
    ref_id: int | None            # instrument whose price centres the chain
    strikes: list[int]            # sorted strike_paise
    options: dict[int, list[int]]  # strike -> [CE id, PE id]
    center: int | None = None     # index into strikes
    kept: set[int] = field(default_factory=set)   # strike indices currently subscribed


class SubscriptionManager:
    def __init__(self, day: date, universe: dict, clock: Clock, *,
                 log_rows: Callable[[list[tuple]], None] | None = None):
        self.day = day
        self.universe = universe
        self.clock = clock
        self.log_rows = log_rows or (lambda rows: None)
        limits = universe.get("limits", {})
        self.cap = int(limits.get("max_per_connection", 3000))
        self.warn_at = int(self.cap * Decimal(limits.get("warn_fraction", "0.8")))
        self.static: dict[int, Sub] = {}
        self.chains: list[Chain] = []
        self.pins: dict[str, set[int]] = {}
        self.current: dict[int, Sub] = {}
        self.warnings: list[str] = []
        self._build_static()
        self._build_chains()

    # ── construction ─────────────────────────────────────────────────────────
    def _token(self, iid: int) -> int | None:
        return instruments.token_at(iid, self.day)

    def _add_static(self, iid: int | None, mode: str, reason: str) -> None:
        if iid is None:
            self.warnings.append(f"not found: {reason}")
            return
        token = self._token(iid)
        if token is None:
            self.warnings.append(f"no token on {self.day}: {reason}")
            return
        old = self.static.get(iid)
        if old is None or (old.mode == QUOTE and mode == FULL):
            self.static[iid] = Sub(iid, token, mode, reason)

    def _symbol(self, qualified: str) -> int | None:
        exchange, symbol = qualified.split(":", 1)
        return instruments.find(exchange, symbol)

    def _futures(self, underlying: str, months: list[str], mode: str) -> None:
        expiries = instruments.expiries(underlying, "FUT", self.day)
        for month in months:
            k = {"near": 0, "next": 1, "far": 2}[month]
            if k < len(expiries):
                iid = instruments.derivative(underlying, "FUT", expiries[k])
                self._add_static(iid, mode, f"future:{underlying}:{month}")
            else:
                self.warnings.append(f"no {month} future for {underlying} on {self.day}")

    def _build_static(self) -> None:
        u = self.universe
        for s in u.get("indices_full", []):
            self._add_static(self._symbol(s), FULL, f"index:{s}")
        for f in u.get("futures_full", []):
            self._futures(f["underlying"], f["months"], FULL)
        for s in u.get("equities_full", []):
            self._add_static(self._symbol(s), FULL, f"equity:{s}")
        for f in u.get("stock_futures_full", []):
            self._futures(f["underlying"], f["months"], FULL)
        for s in u.get("equities_quote", []):
            self._add_static(self._symbol(s), QUOTE, f"equity:{s}")

    def _resolve_expiry(self, underlying: str, selector: str) -> str | None:
        kind, _, k = selector.partition(":")
        k = int(k or 0)
        opt = instruments.expiries(underlying, "OPT", self.day)
        if kind == "weekly":
            pool = opt
        elif kind == "monthly":
            pool = [e for e in instruments.expiries(underlying, "FUT", self.day) if e in set(opt)]
        else:
            raise ValueError(f"unknown expiry selector {selector!r}")
        return pool[k] if k < len(pool) else None

    def _build_chains(self) -> None:
        for spec in self.universe.get("chains", []):
            und, sel = spec["underlying"].upper(), spec["expiry"]
            key = f"{und}:{sel}"
            expiry = self._resolve_expiry(und, sel)
            if expiry is None:
                self.warnings.append(f"no expiry for chain {key} on {self.day}")
                continue
            options: dict[int, list[int]] = {}
            for row in instruments.option_chain(und, expiry, self.day):
                options.setdefault(row["strike_paise"], []).append(row["instrument_id"])
            if not options:
                self.warnings.append(f"empty chain {key} ({expiry})")
                continue
            ref = instruments.derivative(und, "FUT", expiry)
            if ref is None:
                first = next(iter(options.values()))[0]
                ref = instruments.underlying_of(first)
            self.chains.append(Chain(key, und, expiry, int(spec["n_full"]), int(spec["n_quote"]),
                                     int(spec.get("recenter_threshold", 2)), int(spec.get("hysteresis", 2)),
                                     ref, sorted(options), options))

    def reference_ids(self) -> set[int]:
        """Instruments whose price the caller must feed to `update_prices`."""
        return {c.ref_id for c in self.chains if c.ref_id is not None}

    # ── desired state ────────────────────────────────────────────────────────
    @staticmethod
    def _nearest(strikes: list[int], price: int) -> int:
        return min(range(len(strikes)), key=lambda i: (abs(strikes[i] - price), strikes[i]))

    def _recentre(self, chain: Chain, price: int) -> bool:
        target = self._nearest(chain.strikes, price)
        if chain.center is not None and abs(target - chain.center) < chain.recenter_threshold:
            return False
        chain.center = target
        reach = chain.n_quote + chain.hysteresis
        keep = {i for i in chain.kept if abs(i - target) <= reach}
        window = {i for i in range(max(0, target - chain.n_quote),
                                   min(len(chain.strikes), target + chain.n_quote + 1))}
        chain.kept = keep | window
        return True

    def _desired(self) -> dict[int, Sub]:
        out: dict[int, Sub] = dict(self.static)
        quote_rank: list[tuple[int, int]] = []          # (distance, iid) for cap trimming
        for c in self.chains:
            if c.center is None:
                continue
            for i in sorted(c.kept):
                distance = abs(i - c.center)
                mode = FULL if c.n_full > 0 and distance <= c.n_full else QUOTE   # n_full 0 = quote-only chain
                for iid in c.options[c.strikes[i]]:
                    token = self._token(iid)
                    if token is None:
                        continue
                    old = out.get(iid)
                    if old is None or (old.mode == QUOTE and mode == FULL):
                        out[iid] = Sub(iid, token, mode, f"chain:{c.key}")
                    if mode == QUOTE:
                        quote_rank.append((distance, iid))
        for owner, ids in sorted(self.pins.items()):
            for iid in sorted(ids):
                token = self._token(iid)
                if token is not None:
                    out[iid] = Sub(iid, token, FULL, f"pin:{owner}")
        if len(out) > self.cap:
            pinned = {i for ids in self.pins.values() for i in ids}
            for _, iid in sorted(quote_rank, reverse=True):
                if len(out) <= self.cap:
                    break
                if iid not in pinned and out.get(iid) and out[iid].mode == QUOTE:
                    del out[iid]
            self.warnings.append(f"subscription cap {self.cap} reached: trimmed farthest QUOTE strikes")
        if len(out) >= self.warn_at:
            self.warnings.append(f"{len(out)} subscriptions, at or above {self.warn_at} warning level")
        return out

    def _apply(self, reason: str) -> Diff:
        want = self._desired()
        diff = Diff()
        for iid, sub in want.items():
            have = self.current.get(iid)
            if have is None:
                diff.subscribe.append(sub)
            elif have.mode != sub.mode:
                diff.mode_change.append(sub)
        for iid, sub in self.current.items():
            if iid not in want:
                diff.unsubscribe.append(sub)
        self.current = want
        now = self.clock.now_ns()
        rows = ([(now, "subscribe", s.instrument_id, s.token, s.mode, f"{reason}:{s.reason}") for s in diff.subscribe]
                + [(now, "mode", s.instrument_id, s.token, s.mode, f"{reason}:{s.reason}") for s in diff.mode_change]
                + [(now, "unsubscribe", s.instrument_id, s.token, s.mode, f"{reason}:{s.reason}") for s in diff.unsubscribe])
        if rows:
            self.log_rows(rows)
        return diff

    # ── public operations ────────────────────────────────────────────────────
    def build(self, prices: dict[int, int]) -> Diff:
        """First subscription set of the day. `prices`: instrument_id -> paise."""
        for c in self.chains:
            if c.ref_id in prices:
                self._recentre(c, prices[c.ref_id])
            else:
                self.warnings.append(f"chain {c.key}: no reference price yet")
        return self._apply("start")

    def update_prices(self, prices: dict[int, int]) -> Diff:
        changed = False
        for c in self.chains:
            if c.ref_id in prices:
                changed |= self._recentre(c, prices[c.ref_id])
        return self._apply("recenter") if changed else Diff()

    def pin(self, ids, owner: str) -> Diff:
        self.pins.setdefault(owner, set()).update(int(i) for i in ids)
        return self._apply("pin")

    def unpin(self, owner: str) -> Diff:
        self.pins.pop(owner, None)
        return self._apply("unpin")

    def unsubscribe_all(self) -> Diff:
        diff = Diff(unsubscribe=list(self.current.values()))
        now = self.clock.now_ns()
        if diff.unsubscribe:
            self.log_rows([(now, "unsubscribe", s.instrument_id, s.token, s.mode, "session_end")
                           for s in diff.unsubscribe])
        self.current = {}
        return diff

    def counts(self) -> dict[str, int]:
        return {FULL: sum(1 for s in self.current.values() if s.mode == FULL),
                QUOTE: sum(1 for s in self.current.values() if s.mode == QUOTE)}
