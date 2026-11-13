"""The oracle: a single-threaded, readable implementation of A4 (M2.2).

    result = run_bundle(bundle, pack_paths, out_dir)

Reading guide, in A4 order:

* §1 events — `Engine.run`: one key `(t, rank, src, seq)` for pack records and
  internal events (PHASE 0, GAP 1, REFDATA 2, SETTLE 3, market data 4, CLOCK 5,
  BAR_BUILT 6, MIS_SQOFF 7, TIMER 8, SEG_CLOSE 9, DAY_END 10); orders activate
  just before the first event at or after their `t_activate`.
* §2 market state — `Market`.
* §3 orders — `place` / `_validate` / `_activate` / `modify` / `cancel`.
* §4 fills — `_match_bar`, `_match_snapshot`, `_match_eod`.
* §5 costs — `_complete` and `stockify.oracle.costs`.
* §6 ledger — `stockify.oracle.ledger`, `_funds`, `_seg_close`, `_mis_sqoff`.
* §7 settlement — `_settle_final`, `_day_end`.

State that a bar's two simulated paths may change lives in `Book`, which is
copied for each path; the path with the lower NAV is adopted (§4.1).
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import date as _date

from stockify import numerics, timeutil
from stockify.oracle import costs as costs_mod
from stockify.oracle import notes
from stockify.oracle.bars import bucket_start, volume_delta
from stockify.oracle.ledger import Ledger
from stockify.oracle.margin import DERIVATIVES, FlatMargin
from stockify.oracle.matcher import (OPEN_POS, bar_opportunity, extra_ticks_of, participation_cap, path_points, protection_limit,
                                     slipped)
from stockify.oracle.orders import ACTIVE, FINAL, SYSTEM_TAGS, Fill, FillView, Order, OrderView
from stockify.oracle.rng import SLIPPAGE_STREAM, STRATEGY_STREAM, Pcg32
from stockify.strategy import Bar, Funds, PhaseChange, PositionView, Tick

# Event ranks, in A4 §1.3 order (checked by blockwatch).
# <block name="event-ranks" same-as="docs/plan/A4_engine_semantics.md:event-ranks" same-as-mode="sequence" same-as-pattern='\b(?P<value>[A-Z][A-Z_]+)\b'>
PHASE, GAP, REFDATA, SETTLE, MARKET, CLOCK, BAR_BUILT, MIS_SQOFF, TIMER, SEG_CLOSE, DAY_END = range(11)
# </block>
RANK = {"GAP": GAP, "REFDATA": REFDATA, "SETTLE": SETTLE, "TICK_QUOTE": MARKET, "TICK_FULL": MARKET,
        "INDEX": MARKET, "BAR": MARKET, "CLOCK": CLOCK}
PPB = 1_000_000_000


class RunError(RuntimeError):
    pass


def check_strategy_control(payload) -> tuple[bool, list[str]]:  # noqa: ANN001
    """A strategy_control record's payload (A1 §5, A4 §3.11), or a RunError the engine words the same: exactly
    {"enabled": bool, "reasons": [str, …]}, the reasons non-empty strings in strictly ascending order, none when
    enabled and at least one when disabled."""
    if not isinstance(payload, dict):
        raise RunError("strategy_control: payload must be an object")
    if set(payload) != {"enabled", "reasons"}:
        raise RunError("strategy_control: payload must have exactly the keys 'enabled' and 'reasons'")
    enabled, reasons = payload["enabled"], payload["reasons"]
    if type(enabled) is not bool:
        raise RunError("strategy_control: enabled must be a boolean")
    if type(reasons) is not list or any(type(r) is not str for r in reasons):
        raise RunError("strategy_control: reasons must be a list of strings")
    if any(r == "" for r in reasons):
        raise RunError("strategy_control: a reason must not be empty")
    if any(a >= b for a, b in zip(reasons, reasons[1:])):
        raise RunError("strategy_control: reasons must be sorted and unique")
    if enabled and reasons:
        raise RunError("strategy_control: reasons must be empty when enabled")
    if not enabled and not reasons:
        raise RunError("strategy_control: reasons must not be empty when disabled")
    return enabled, reasons


# ── State ────────────────────────────────────────────────────────────────────

@dataclass
class Book:
    """Everything a fill can change; copied for each simulated bar path."""
    ledger: Ledger
    orders: dict                     # order_id -> Order (all orders)
    rng: Pcg32
    fills: list = field(default_factory=list)
    charges: list = field(default_factory=list)
    order_events: list = field(default_factory=list)
    callbacks: list = field(default_factory=list)
    dp_done: set = field(default_factory=set)
    next_fill: int = 1

    def fork(self, iid: int, live: list) -> "Book":
        """A copy for one bar path: the ledger, this instrument's live orders (`live`: every order not final) and the
        RNG. The dict is copied whole (same order) and only those orders are replaced, so a long paper session's many
        final orders cost a C-level copy, not a Python loop."""
        orders = dict(self.orders)
        for o in live:
            if o.instrument_id == iid and o.status in ACTIVE:
                orders[o.order_id] = o.copy()
        return Book(self.ledger.copy(), orders, self.rng.copy(), [], [], [], [], set(self.dp_done), self.next_fill)


@dataclass
class Market:
    ltp: int | None = None
    t: int | None = None
    s0: dict | None = None
    s1: dict | None = None
    bid: int | None = None
    ask: int | None = None
    book: list | None = None         # [(price, qty, orders)] bids 1-5 then asks 1-5
    band: tuple | None = None        # (lower, upper)
    oi: int | None = None            # open interest: the last TICK_FULL or bar that carried it (M4.4)
    min_interval: int | None = None
    bars: dict = field(default_factory=dict)       # interval_s -> list of (start, o, h, l, c, v)
    day: str | None = None


# ── The engine ───────────────────────────────────────────────────────────────


def _cutoff_key(group: str) -> str:
    """The MIS cut-off an instrument follows (A4 §6.3): FO for derivatives, CM_CAS for a CAS stock on its CAS day
    (Z-5), CM otherwise."""
    if group.startswith("FO"):
        return "FO"
    return "CM_CAS" if group == "CM_CAS" else "CM"

PLOT_MAX_SERIES, PLOT_MAX_POINTS = 8, 200_000      # CH 6.4: per run (D-94)


class Engine:
    def __init__(self, bundle: dict, records: list[tuple], strategy, *, log_lines: list | None = None):  # noqa: ANN001
        self.bundle = bundle
        self.run_cfg = bundle["run"]
        self.fill_cfg = self.run_cfg["fill_model"]
        extra_ticks_of(self.fill_cfg.get("slippage", {}))               # FW 6.2: a hand-edited bundle fails here
        self.policies = self.run_cfg["policies"]
        self.latency_ns = self.run_cfg["latency_ms"] * 1_000_000
        self.inst = {i["id"]: i for i in bundle["instruments"]}
        self._check_instruments(bundle["instruments"])
        self.days = bundle["days"]
        self.day_by_date = {d["date"]: d for d in self.days}
        # prev_days / next_days (A1 §6.2, VOL 5.2): trading days before and after the bundle's own, never processed;
        # consulted only for a day's CONTINUOUS start when a bar dated that day arrives later (a daily bar published at
        # the next midnight), where an order queued after today's session activates (A4 §3.10), and for trading-day
        # counts (D-60)
        self.day_bundle = "next_days" in bundle or "prev_days" in bundle   # a paper session's day bundle (A1 §6.2)
        prev, nxt = bundle.get("prev_days"), bundle.get("next_days")
        self._check_cal_days("prev_days", prev, None, self.days[0]["date"])
        self._check_cal_days("next_days", nxt, self.days[-1]["date"], None)
        self.cal_days = list(prev or []) + self.days + list(nxt or [])
        self.cal_day_ends = [d["day_end_ns"] for d in self.cal_days]
        self.cal_by_date = {d["date"]: d for d in self.cal_days}
        self.system_notes = bundle.get("system_notes") or {}   # CH 6.2: {tag: text}; absent in an older bundle
        self.records = records                       # [(key, record)] sorted
        self.strategy = strategy
        self.costs = costs_mod.Costs(bundle["charges"], self.run_cfg["costs"])
        self._check_mis_rows(bundle["broker_policy"].get("mis_margin_pct"))
        model = self.run_cfg["margin"]["model"]
        self.live_answers = None                      # the journaled margin answers (live_with_fallback only)
        if model == "spanlite":
            from stockify.fno.spanlite import SpanLiteMargin
            self.margin = SpanLiteMargin(bundle, self)
        elif model == "flat":
            self.margin = FlatMargin(bundle)
        elif model == "staircase":                     # MEZ 4.9: B1..B5 per underlying per check, steps recorded
            from stockify.fno.staircase import StaircaseMargin
            self.margin = StaircaseMargin(bundle, self)
        elif model == "live_with_fallback":            # MEZ 5.1: the staircase, a fresh journaled Kite answer prices the book
            from stockify.fno import live_margin
            self.live_answers = live_margin.LiveAnswers()
            self.margin = live_margin.LiveWithFallbackMargin(bundle, self, self.live_answers)
        elif model == "span_exact":                    # exact SPAN only: raises where it cannot price
            from stockify.fno.staircase import SpanExactMargin
            self.margin = SpanExactMargin(bundle, self)
        else:                                          # never a silent fallback to flat (CAM 4.3)
            raise RunError(f"run.margin: margin model {model!r} is not supported yet")
        self.margin_model = model
        # (date, underlying_id, model, step, scan, somc, elm, addons, total): M4.8, step MEZ 4.9
        self.margin_daily: list[tuple] = []
        self.market: dict[int, Market] = {}
        capital = self.run_cfg["capital_paise"]
        self.book = Book(Ledger(capital, capital), {}, Pcg32(bundle["seed"], SLIPPAGE_STREAM))
        self.strategy_rng = Pcg32(bundle["seed"], STRATEGY_STREAM)
        self.day_seeds = self._day_seeds(self.days)     # [(date, seed)] for seed_mode per_day (VOL 5.1)
        self.next_day_seed = 0
        self.internal: list = []
        self.internal_seq = 0
        self.now = 0
        self.key = (0, -1, 0, 0)
        self.next_order = 1
        self.live_ids: list[int] = []                   # ids of the orders that are not final, ascending
        self.day_ends = [d["day_end_ns"] for d in self.days]
        self.sub_ticks: set[int] = set()
        self.sub_bars: dict[int, set[int]] = {}
        self.built: dict = {}                          # (iid, interval) -> {start: [o,h,l,c,v,oi]}
        self.timers: dict[int, tuple] = {}
        self.next_timer = 1
        self.positions_eod: list = []
        self.equity: list = []
        self.log_lines = log_lines if log_lines is not None else []
        self.labels: set[str] = set(bundle.get("labels", []))
        self.plot_kinds: dict[tuple, str] = {}         # CH 6.4: (instrument, name) -> kind, fixed by the first call
        self.plot_points: dict[tuple, int] = {}        # (t_ns, instrument, name) -> value; a repeat replaces
        self.plot_capped: set[str] = set()             # the caps already logged ("series", "points")
        self.settle_closes: list = []                  # (t, iid, product, side, units, price): expiry closes (D-45)
        self.fsp_seen: dict = {}                       # (underlying_id, date) -> fsp
        self.underlying_last: dict[int, int] = {}
        self.participation_used = 0                    # ppm of a bar's volume, max over the run
        self.rejects: dict[str, int] = {}
        self.event_no = 0
        self.delivering = False
        self.open_changes: list[tuple[int, bool]] = []   # (t, any position open) transitions, for exposure
        # Day-to-day state (VOL 5.2, A4 §1.10): see stockify.oracle.sod_state
        self.carried: list = []                        # internal events passed over after the last DAY_END
        self.prior_final: set[int] = set()             # final orders loaded from sod_state (not this day's output)
        self.charge_row_base = 0                       # charges.csv rows written on earlier days
        self.state_date: str | None = None             # the last trading day whose DAY_END ran
        self.day_end_strategy_state: dict | None = None  # save_state() at DAY_END, checked (day bundles only)
        self.strategy_enabled = True                   # D-70, A4 §3.11: a journaled switch, carried day to day
        self.pause_reasons: set[str] = set()
        self.queued_notifications: list = []           # on_fill / on_order_update held while disabled, in order
        self.started = False
        self.resumed = False
        self.busy: str | None = None                   # the call in progress (run / load_sod_state / eod_state)
        self.settled = False                           # a day has ended and nothing has been processed since
        from stockify.oracle.context import Context
        self.ctx = Context(self)
        if bundle.get("sod_state") is not None:        # applied here, so no caller can forget it
            if not isinstance(bundle["sod_state"], dict):
                raise RunError("bundle: sod_state must be an object or null")
            self._load_state(bundle["sod_state"])

    # ── Day-to-day state (A1 §6.4) ────────────────────────────────────────────

    def _enter(self, what: str) -> None:
        """One call at a time: the engine refuses a call made from inside another one (a strategy callback)."""
        if self.busy is not None:
            raise RunError(f"{what}: the engine is busy ({self.busy} is in progress)")
        self.busy = what

    def _load_state(self, state, text: str | None = None) -> None:  # noqa: ANN001
        """Apply a state (a parsed object, or the text of one), all or nothing."""
        from stockify.oracle import sod_state
        self._enter("load_sod_state")
        try:
            if self.started:
                raise RunError("sod_state must be loaded before the first event")
            if self.resumed:
                raise RunError("sod_state is already loaded")
            try:
                sod_state.restore(self, sod_state.loads(text) if text is not None else state)
            except (sod_state.StateError, KeyError, TypeError, ValueError, ArithmeticError, RecursionError) as e:
                raise RunError(str(e)) from e
            self.resumed = True
            self.settled = True
        finally:
            self.busy = None

    def load_sod_state(self, text: str) -> None:
        """A start-of-day state (eod_state() of the previous day); before the first event only."""
        self._load_state(None, text)

    def eod_state(self) -> str:
        """The state for the next day's sod_state, as canonical JSON (A1 §6.4)."""
        from stockify.oracle import sod_state
        self._enter("eod_state")
        try:
            if self.state_date is None or not self.settled:
                raise RunError("sod_state: no trading day has ended yet" if self.state_date is None else
                               "sod_state: events were processed after the last day ended")
            try:
                return sod_state.dumps(sod_state.capture(self))
            except sod_state.StateError as e:
                raise RunError(str(e)) from e
        finally:
            self.busy = None

    @staticmethod
    def _check_cal_days(key: str, rows, after: str | None, before: str | None) -> None:  # noqa: ANN001
        """prev_days / next_days: day objects in the days[] shape without seed, dates rising, all after `after` (the
        last day) or all before `before` (the first day)."""
        if rows is None:
            return
        if not isinstance(rows, list):
            raise RunError(f"bundle: {key} must be a list")
        last = after
        for n, d in enumerate(rows):
            where = f"bundle: {key}[{n}]"
            if not isinstance(d, dict) or not {"date", "groups", "mis_cutoff_ns", "day_end_ns"} <= set(d):
                raise RunError(f"{where} must be a day object (date, groups, mis_cutoff_ns, day_end_ns)")
            if "seed" in d:
                raise RunError(f"{where} has a seed: these days are never processed")
            if last is not None and d["date"] <= last:
                raise RunError(f"{where}: {d['date']} is not after {last}")
            last = d["date"]
        if before is not None and rows and last >= before:
            raise RunError(f"bundle: {key}: {last} is not before {before}")

    def day_orders(self) -> dict:
        """This day's orders: all but the final ones loaded from sod_state (orders.csv, metrics)."""
        return {k: o for k, o in self.book.orders.items() if k not in self.prior_final}

    # ── Time and phases ──────────────────────────────────────────────────────

    def _date(self, t: int) -> str:
        return timeutil.ns_to_ist(t).date().isoformat()

    def group_of(self, iid: int, d: str | None = None) -> str:
        row = self.inst[iid]
        g = row["session_group"]
        if g == "CM_CAS":
            return "CM_CAS" if (d or self._date(self.now)) in row["cas_dates"] else "CM"
        return g

    def phase_at(self, group: str, t: int) -> str:
        day = self.day_by_date.get(self._date(t))
        if day is None:
            return "CLOSED"
        for p in day["groups"][group]:
            if p["start_ns"] <= t < p["end_ns"]:
                return p["phase"]
        return "CLOSED"

    def continuous_start(self, group: str, d: str) -> int | None:
        day = self.cal_by_date.get(d)
        if day is None:
            return None
        starts = [p["start_ns"] for p in day["groups"][group] if p["phase"] == "CONTINUOUS"]
        return min(starts) if starts else None

    def next_continuous_start(self, iid: int, t: int) -> int | None:
        import bisect
        for day in self.cal_days[bisect.bisect_left(self.cal_day_ends, t):]:
            g = self.group_of(iid, day["date"])
            for p in day["groups"][g]:
                if p["phase"] == "CONTINUOUS" and p["start_ns"] >= t:
                    return p["start_ns"]
        return None

    @staticmethod
    def _check_mis_rows(rows) -> None:  # noqa: ANN001
        """broker_policy.mis_margin_pct: an FO row is a share of the NRML margin, and only the whole of it is modelled
        (A4 §6.5, D-80): any other value is refused, never scaled silently."""
        for r in rows or []:
            if (r.get("applies") or {}).get("segment") != "FO":
                continue
            v = r.get("pct_ppb")
            if type(v) is not int or v != PPB:
                raise RunError(f"bundle: broker_policy.mis_margin_pct: the FO row from {r['effective_from']} has pct_ppb "
                               f"{v if type(v) is int else 'missing'}; only {PPB} (the full NRML margin) is "
                               "accepted: intraday F&O leverage is not modelled (D-80)")

    @staticmethod
    def _check_instruments(rows: list[dict]) -> None:
        """Ids within uint32 and positive lots and ticks, checked once (as the engine's InstrumentTable does), so
        no later division meets a zero."""
        for n, r in enumerate(rows):
            where = f"instruments[{n}]"
            for field in ("id", "underlying_id"):
                v = r.get(field)
                if v is not None and not 0 <= v <= 4294967295:
                    raise RunError(f"bundle: {where}: {field} must be between 0 and 4294967295, got {v}")
            if r.get("kind") in ("FUT", "CE", "PE") and r.get("last_trading_ns") is None:
                raise RunError(f"bundle: {where}: a {r['kind']} needs last_trading_ns (D-61)")
            for s in r.get("specs") or []:
                at = f"bundle: {where}.specs (from {s['from']}): "
                if s["lot"] <= 0:
                    raise RunError(at + f"lot must be positive, got {s['lot']}")
                if s["tick_paise"] <= 0:
                    raise RunError(at + f"tick_paise must be positive, got {s['tick_paise']}")

    @staticmethod
    def _day_seeds(days: list[dict]) -> list[tuple[str, int]]:
        """days[].seed, the only source of a day's seed (A4 §4.5): present when the run's seed_mode is per_day."""
        out = []
        for n, d in enumerate(days):
            if "seed" not in d:
                continue
            v = d["seed"]
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v < 2 ** 64:
                raise RunError(f"bundle: days[{n}].seed must be a non-negative 64-bit integer")
            out.append((d["date"], v))
        return out

    def _reseed_for(self, t: int) -> None:
        """Before the first event whose IST date is a seeded day: both streams re-created from that day's seed
        (stream 1 slippage, stream 2 ctx.rng()), in place, so every holder of them sees the new sequence."""
        if self.next_day_seed >= len(self.day_seeds):
            return
        d = self._date(t)
        seed = None
        while self.next_day_seed < len(self.day_seeds) and self.day_seeds[self.next_day_seed][0] <= d:
            seed = self.day_seeds[self.next_day_seed][1]
            self.next_day_seed += 1
        if seed is not None:
            self.book.rng.reseed(seed, SLIPPAGE_STREAM)
            self.strategy_rng.reseed(seed, STRATEGY_STREAM)

    def spec(self, iid: int, d: str) -> dict | None:
        best = None
        for s in self.inst[iid]["specs"]:
            if s["from"] <= d:
                best = s
        return best

    # ── Internal events ──────────────────────────────────────────────────────

    def push(self, t: int, rank: int, kind: str, payload=None) -> None:  # noqa: ANN001
        self.internal_seq += 1
        heapq.heappush(self.internal, ((t, rank, 0, self.internal_seq), kind, payload))

    def _schedule_days(self) -> None:
        for day in self.days:
            groups = day["groups"]
            for g, phases in groups.items():
                for p in phases:
                    self.push(p["start_ns"], PHASE, "PHASE", (g, p["phase"]))
                    self.push(p["end_ns"], PHASE, "PHASE", (g, "CLOSED"))
                close = [p["end_ns"] for p in phases if p["phase"] in ("CONTINUOUS", "CAS")]
                if close:
                    self.push(max(close), SEG_CLOSE, "SEG_CLOSE", (g, day["date"]))
            for seg, t in day["mis_cutoff_ns"].items():
                if t is not None and seg in ("CM", "CM_CAS", "FO"):
                    self.push(t, MIS_SQOFF, "MIS_SQOFF", (seg, day["date"]))
            self.push(day["day_end_ns"], DAY_END, "DAY_END", day["date"])

    # ── Logging and callbacks ────────────────────────────────────────────────

    def log(self, level: str, msg: str) -> None:
        self.log_lines.append(f"{self.now} {level} {msg}")

    def plot(self, name: str, value: int, kind: str, instrument: int) -> None:
        """CH 6.4 (Context.plot, args already checked): an unknown instrument or a changed kind is a ValueError; past
        PLOT_MAX_SERIES series or PLOT_MAX_POINTS points the call is dropped and one WARN line says so per cap."""
        if instrument != 0 and instrument not in self.inst:
            raise ValueError(f"ctx.plot: instrument {instrument} is not in this run's bundle")
        series = (instrument, name)
        have = self.plot_kinds.get(series)
        if have is not None and have != kind:
            raise ValueError(f'ctx.plot: series "{name}" of instrument {instrument} is kind "{have}", not "{kind}"')
        key = (self.now, instrument, name)
        if have is None and len(self.plot_kinds) >= PLOT_MAX_SERIES:
            self._plot_cap("series", PLOT_MAX_SERIES)
        elif key not in self.plot_points and len(self.plot_points) >= PLOT_MAX_POINTS:
            self._plot_cap("points", PLOT_MAX_POINTS)
        else:
            self.plot_kinds[series] = kind
            self.plot_points[key] = value

    def _plot_cap(self, which: str, limit: int) -> None:
        if which not in self.plot_capped:
            self.plot_capped.add(which)
            self.log("WARN", f"plot limit reached: more than {limit} {which}")

    def plot_rows(self) -> list[tuple]:
        """plots.csv's rows, sorted by (t_ns, instrument_id, name)."""
        return [(t, i, n, self.plot_kinds[(i, n)], v) for (t, i, n), v in sorted(self.plot_points.items())]

    def _event(self, book: Book, o: Order, event: str, detail: str = "") -> None:
        book.order_events.append((self.now, o.order_id, event, detail))
        if event in ("SUBMIT", "ACTIVATE", "TRIGGER", "FILL", "COMPLETE", "CANCEL", "REJECT", "MODIFY", "SLICE"):
            book.callbacks.append(("order", o.order_id))

    def _deliver(self) -> None:
        """Queued on_order_update / on_fill callbacks, in the order generated (A4 §3.9). While the strategy is
        disabled they are not delivered: each is kept, as the strategy would have seen it, for the enable (A4 §3.11)."""
        if self.delivering:
            return
        self.delivering = True
        try:
            while self.book.callbacks:
                kind, x = self.book.callbacks.pop(0)
                if kind == "order":
                    view = OrderView.of(self.book.orders[x])
                    if self.strategy_enabled:
                        self.strategy.on_order_update(self.ctx, view)
                    else:
                        self.queued_notifications.append({"kind": "order", "order": dict(vars(view))})
                else:
                    f = x
                    view = FillView(f.order_id, f.instrument_id, f.side, f.qty, f.price, f.t_ns, f.evidence)
                    if self.strategy_enabled:
                        self.strategy.on_fill(self.ctx, view)
                    else:
                        self.queued_notifications.append({"kind": "fill", "fill": dict(vars(view))})
        finally:
            self.delivering = False

    def _hook_on(self, hook: str) -> bool:
        """True when `hook` may run; while the strategy is disabled it is dropped, with a log line (A4 §3.11)."""
        if self.strategy_enabled:
            return True
        self.log("INFO", f"{hook} skipped: strategy disabled ({', '.join(sorted(self.pause_reasons))})")
        return False

    def _on_strategy_control(self, payload) -> None:  # noqa: ANN001
        """A journaled pause or resume (D-70). Enabling delivers what was held, in order, before any other hook."""
        enabled, reasons = check_strategy_control(payload)
        was = self.strategy_enabled
        self.strategy_enabled, self.pause_reasons = enabled, set(reasons)
        if not enabled:
            if was:
                self.log("INFO", f"strategy disabled ({', '.join(reasons)})")
            return
        if was:
            return
        self.log("INFO", "strategy enabled")
        held, self.queued_notifications = self.queued_notifications, []
        self.delivering = True
        try:
            for n in held:
                if n["kind"] == "order":
                    self.strategy.on_order_update(self.ctx, OrderView(**n["order"]))
                else:
                    self.strategy.on_fill(self.ctx, FillView(**n["fill"]))
        finally:
            self.delivering = False

    # ── Funds (A4 §6.5) ──────────────────────────────────────────────────────

    def mark_price(self, iid: int) -> int | None:
        m = self.market.get(iid)
        if m is None:
            return None
        if self.run_cfg["mark"] == "mid" and m.bid and m.ask:
            return (m.bid + m.ask) // 2
        return m.ltp

    def _fo_book(self, ledger: Ledger) -> list[tuple[int, str, int]]:
        """The margin model's portfolio (A4 §6.5, D-80): every NRML position and every MIS future or option, one net
        leg per instrument in the ledger's order, each handed over as NRML. An MIS future or option is margined
        exactly as the same NRML position, in one book with the NRML ones, so a hedge across the two products
        counts; legs that net to nothing are left out."""
        net: dict[int, int] = {}
        for p in ledger.positions.values():
            if p.q and (p.product == "NRML" or
                        (p.product == "MIS" and self.inst[p.instrument_id]["kind"] in DERIVATIVES)):
                net[p.instrument_id] = net.get(p.instrument_id, 0) + p.qty
        return [(iid, "NRML", units) for iid, units in net.items() if units]

    def blocked(self, ledger: Ledger, d: str) -> int:
        mis = 0                                         # MIS shares: a share of their value (A4 §6.5)
        for p in ledger.positions.values():
            kind = self.inst[p.instrument_id]["kind"]
            if p.q and p.product == "MIS" and kind not in DERIVATIVES:
                mis += self.margin.mis(kind, p.q, self.mark_price(p.instrument_id) or p.avg_price, d)
        return self.margin.requirement(self._fo_book(ledger), self.inst.__getitem__, self._ref_price, d) + mis

    def _ref_price(self, iid: int) -> int | None:
        return self.mark_price(iid) or self.underlying_last.get(iid)

    def funds(self, ledger: Ledger | None = None) -> Funds:
        ledger = ledger or self.book.ledger
        d = self._date(self.now)
        blocked = self.blocked(ledger, d)
        held = 0
        for v in ledger.unavailable.values():
            held += v
        available = ledger.cash - blocked - ledger.reserved - held
        return Funds(available, ledger.cash, blocked, ledger.reserved)

    def _reduces(self, iid: int, product: str, side: str, qty: int) -> bool:
        p = self.book.ledger.position(iid, product)
        return p is not None and p.d != (1 if side == "BUY" else -1) and qty <= p.q

    def _required(self, o: Order, px: int) -> int:
        row = self.inst[o.instrument_id]
        kind = row["kind"]
        d = self._date(self.now)
        if self._reduces(o.instrument_id, o.product, o.side, o.qty):
            return 0
        if o.product == "CNC":
            return o.qty * px if o.side == "BUY" else 0
        if o.product == "MIS" and kind not in DERIVATIVES:
            return self.margin.mis(kind, o.qty, px, d)
        # NRML, and MIS futures and options, which need what the same NRML order needs (D-80)
        if kind in ("CE", "PE") and o.side == "BUY":
            return o.qty * px
        legs = self._fo_book(self.book.ledger)
        combined = {iid: units for iid, _, units in legs}
        combined[o.instrument_id] = combined.get(o.instrument_id, 0) + o.qty * o.sign
        merged = [(iid, "NRML", units) for iid, units in combined.items() if units]
        price_after = lambda i: px if i == o.instrument_id else self._ref_price(i)  # noqa: E731
        if hasattr(self.margin, "pair"):            # the staircase: before and after at one step (MEZ 4.9)
            before, after = self.margin.pair(legs, merged, self.inst.__getitem__, self._ref_price, price_after, d)
        else:
            before = self.margin.requirement(legs, self.inst.__getitem__, self._ref_price, d)
            after = self.margin.requirement(merged, self.inst.__getitem__, price_after, d)
        return max(0, after - before)

    # ── Orders (A4 §3) ───────────────────────────────────────────────────────

    def _bp(self, o: Order) -> int:
        if isinstance(o.protection, int):
            return o.protection
        rows = self.bundle["broker_policy"].get("market_protection_auto_bp", [])
        d = self._date(self.now)
        best = None
        for r in rows:
            if r["effective_from"] <= d and (best is None or r["effective_from"] > best["effective_from"]):
                best = r
        if best is None:
            raise RunError(f"no market_protection_auto_bp row on {d}")
        return best["bp"]

    def _reject(self, o: Order, code: str) -> Order:
        o.status, o.reason = "REJECTED", code
        self.book.ledger.reserved -= o.reserved
        o.reserved = 0
        self.rejects[code] = self.rejects.get(code, 0) + 1
        self._event(self.book, o, "REJECT", code)
        self.log("INFO", f"order {o.order_id} rejected {code}")
        return o

    def place(self, iid: int, side: str, qty: int, product: str, type_: str, price: int = 0, trigger: int = 0,
              validity: str = "DAY", protection="AUTO", tag: str = "", parent: int = 0, system: bool = False,
              note: str = "") -> Order:  # noqa: ANN001
        note = notes.clean_note(note)                      # CH 6.2: cleaned once; a slice's children get the clean text
        o = Order(self.next_order, parent, iid, side, qty, product, type_, price, trigger, validity, protection, tag,
                  note, self.now, 0)
        self.next_order += 1
        self.book.orders[o.order_id] = o
        self.live_ids.append(o.order_id)
        self._event(self.book, o, "SUBMIT", f"{side} {qty} {product} {type_}")
        code = self._validate(o, system, upto="R_FREEZE")
        if code:
            return self._reject(o, code)
        spec = self.spec(iid, self._date(self.now))
        freeze = spec.get("freeze_qty")
        if freeze and qty > freeze:
            if self.policies["freeze"] == "reject" and not system:
                return self._reject(o, "R_FREEZE")
            child = numerics.floor_to(freeze, spec["lot"])
            n = -(-qty // child)
            if child <= 0 or n > self.policies["max_slices"]:
                return self._reject(o, "R_FREEZE")
            o.status = "SLICED"
            self._event(self.book, o, "SLICE", f"{n} children")
            left = qty
            while left:
                q = min(child, left)
                left -= q
                self.place(iid, side, q, product, type_, price, trigger, validity, protection, tag, parent=o.order_id,
                           system=system, note=note)
            return o
        code = self._validate(o, system, start="R_HOLDINGS")
        if code:
            return self._reject(o, code)
        self._accept(o)
        return o

    def _validate(self, o: Order, system: bool, start: str = "R_INSTRUMENT", upto: str = "R_MARGIN") -> str | None:
        # The A4 §3.2 check order (checked by blockwatch).
        # <block name="reject-codes" same-as="docs/plan/A4_engine_semantics.md:reject-codes" same-as-mode="sequence" same-as-pattern='"(?P<value>R_[A-Z_]+)"'>
        order = ["R_INSTRUMENT", "R_PRODUCT", "R_SESSION", "R_CAS", "R_QTY_LOT", "R_TICK", "R_BAND", "R_FREEZE",
                 "R_HOLDINGS", "R_NO_REFERENCE", "R_MARGIN"]
        # </block>
        checks = order[order.index(start): order.index(upto) + 1]
        row = self.inst.get(o.instrument_id)
        d = self._date(self.now)
        for code in checks:
            if code == "R_INSTRUMENT":
                if (row is None or row["kind"] in ("INDEX", "FUT_CONT")
                        or (row["last_trading_ns"] is not None and self.now >= row["last_trading_ns"])
                        or o.side not in ("BUY", "SELL") or o.type not in ("MARKET", "LIMIT", "SL", "SL-M")
                        or o.validity not in ("DAY", "IOC")):
                    return code
            elif code == "R_PRODUCT":
                deriv = row["kind"] in ("FUT", "CE", "PE")
                if o.product not in ("CNC", "MIS", "NRML") or (deriv and o.product == "CNC") or \
                        (not deriv and o.product == "NRML"):
                    return code
            elif code == "R_SESSION" and not system:
                g = self.group_of(o.instrument_id, d)
                cutoff = self.day_by_date[d]["mis_cutoff_ns"].get(_cutoff_key(g)) if d in self.day_by_date else None
                if o.product == "MIS" and cutoff is not None and self.now >= cutoff:
                    return code
                if self.policies["preopen"] == "reject" and self.phase_at(g, self.now) != "CONTINUOUS":
                    return code
            elif code == "R_CAS" and not system:
                g = self.group_of(o.instrument_id, d)
                if g == "CM_CAS" and self.phase_at(g, self.now) == "CAS" and self.policies["cas"] == "none":
                    return code
            elif code == "R_QTY_LOT":
                spec = self.spec(o.instrument_id, d)
                if spec is None:
                    return code
                lot = spec["lot"]
                if o.qty <= 0:
                    return code
                if o.qty % lot:
                    reduce_ok = self._reduces(o.instrument_id, o.product, o.side, o.qty) and any(
                        o.qty % s["lot"] == 0 for s in self.inst[o.instrument_id]["specs"] if s["from"] <= d)
                    if not reduce_ok:
                        return code
            elif code == "R_TICK":
                tick = self.spec(o.instrument_id, d)["tick_paise"]
                if o.type in ("LIMIT", "SL") and (o.price <= 0 or o.price % tick):
                    return code
                if o.type in ("SL", "SL-M") and (o.trigger <= 0 or o.trigger % tick):
                    return code
            elif code == "R_BAND":
                band = self.market.get(o.instrument_id, Market()).band
                if band and o.type in ("LIMIT", "SL") and not (band[0] <= o.price <= band[1]):
                    return code
            elif code == "R_FREEZE":
                continue                                # handled by the caller (slice / reject)
            elif code == "R_HOLDINGS":
                if o.product == "CNC" and o.side == "SELL":
                    p = self.book.ledger.position(o.instrument_id, "CNC")
                    held = p.qty if p and p.d > 0 else 0
                    open_sells = 0
                    for x in self.book.orders.values():
                        if (x.order_id != o.order_id and x.instrument_id == o.instrument_id and x.product == "CNC"
                                and x.side == "SELL" and x.status in ("PENDING", "OPEN", "TRIGGER_PENDING")):
                            open_sells += x.remaining
                    if o.qty + open_sells > held:
                        return code
            elif code == "R_NO_REFERENCE":
                if o.type == "MARKET" and self.mark_price(o.instrument_id) is None:     # SL-M uses its trigger
                    return code
            elif code == "R_MARGIN" and not system:
                px = self._order_px(o)
                need = self._required(o, px)
                # A4 §6.5: only risk-increasing orders are refused, so a shortfall can be traded out of
                if need > self.funds().available and not self._reduces(o.instrument_id, o.product, o.side, o.qty):
                    return code
                o.reserved = need
                self.book.ledger.reserved += need
        return None

    def _order_px(self, o: Order) -> int:
        tick = self.spec(o.instrument_id, self._date(self.now))["tick_paise"]
        if o.type in ("LIMIT", "SL"):
            return o.price
        ref = o.trigger if o.type == "SL-M" else self.mark_price(o.instrument_id)
        return protection_limit(o.side, ref, self._bp(o), tick)

    def _accept(self, o: Order) -> None:
        g = self.group_of(o.instrument_id)
        o.t_activate = o.t_submit + self.latency_ns
        if self.phase_at(g, o.t_activate) not in ("CONTINUOUS",):
            nxt = self.next_continuous_start(o.instrument_id, o.t_submit)
            if nxt is None:
                self._reject(o, "R_SESSION")
                return
            o.t_activate = max(nxt + self.latency_ns, o.t_activate)
        o.status = "PENDING"
        o.trade_date = self._date(o.t_activate)

    def live_orders(self) -> list[Order]:
        """Orders that are not final, in id order (finished ones are skipped once)."""
        orders = self.book.orders
        out = [o for o in map(orders.get, self.live_ids) if o is not None and o.status not in FINAL]
        if len(out) != len(self.live_ids):              # some finished since the last call: drop them (once)
            self.live_ids = [o.order_id for o in out]
        return out

    def _activate_due(self, t: int) -> None:
        due = sorted((o for o in self.live_orders() if o.status == "PENDING" and o.t_activate <= t),
                     key=lambda o: (o.t_activate, o.order_id))
        for o in due:
            self._activate(o)

    def _activate(self, o: Order) -> None:
        tick = self.spec(o.instrument_id, self._date(self.now))["tick_paise"]
        if o.type == "MARKET":
            ref = self.mark_price(o.instrument_id)
            if ref is None:
                self._reject(o, "R_NO_REFERENCE")
                return
            o.protection_bp = self._bp(o)
            o.limit = protection_limit(o.side, ref, o.protection_bp, tick)
            o.from_market = True
            o.status = "OPEN"
        elif o.type == "LIMIT":
            o.limit, o.status = o.price, "OPEN"
        else:
            if o.type == "SL-M":
                o.protection_bp = self._bp(o)
            o.status = "TRIGGER_PENDING"
        self._event(self.book, o, "ACTIVATE", f"limit {o.limit}" if o.limit is not None else "")

    def modify(self, order_id: int, changes: dict) -> bool:
        o = self.book.orders.get(order_id)
        if o is None or o.status not in ("PENDING", "OPEN", "TRIGGER_PENDING"):
            return False
        if "qty" in changes and changes["qty"] < o.filled_qty:
            self.log("INFO", f"modify {order_id} refused: qty below filled")
            return False
        new_type = changes.get("type", o.type)
        allowed = {("LIMIT", "MARKET"), ("SL", "LIMIT"), ("SL", "MARKET"), ("SL-M", "LIMIT"), ("SL-M", "MARKET")}
        if new_type != o.type and (o.type, new_type) not in allowed:
            self.log("INFO", f"modify {order_id} refused: {o.type} -> {new_type}")
            return False
        for k in ("qty", "price", "trigger"):
            if k in changes:
                setattr(o, k, int(changes[k]))
        if new_type != o.type:
            o.type = new_type
            o.status = "PENDING"
        o.t_activate = max(o.t_activate, self.now + self.latency_ns)
        if o.status != "PENDING":
            o.status = "PENDING"
        o.from_market = False
        o.limit = None
        self._event(self.book, o, "MODIFY", ",".join(f"{k}={v}" for k, v in sorted(changes.items())))
        return True

    def cancel(self, order_id: int, reason: str = "USER") -> bool:
        o = self.book.orders.get(order_id)
        if o is None or o.status not in ("PENDING", "OPEN", "TRIGGER_PENDING"):
            return False
        self._cancel(self.book, o, reason)
        return True

    def _cancel(self, book: Book, o: Order, reason: str) -> None:
        o.status, o.reason = "CANCELLED", reason
        book.ledger.reserved -= o.reserved
        o.reserved = 0
        self._event(book, o, "CANCEL", reason)
        if o.filled_qty:
            self._charge_order(book, o)

    # ── Fills (A4 §4) and completion (§5.4) ──────────────────────────────────

    def _style(self, iid: int, product: str) -> str:
        kind = self.inst[iid]["kind"]
        if kind in ("CE", "PE"):
            return "premium"
        if kind == "FUT" or product == "MIS":
            return "variation"
        return "premium"

    def _cnc_credit_ppb(self) -> int | None:
        rows = self.bundle["broker_policy"].get("cnc_sell_credit_same_day", [])
        d = self._date(self.now)
        best = None
        for r in rows:
            if r["effective_from"] <= d and (best is None or r["effective_from"] > best["effective_from"]):
                best = r
        return best.get("fraction_ppb") if best else None

    def _fill(self, book: Book, o: Order, qty: int, price: int, evidence: str, t: int) -> None:
        d = self._date(t)
        f = Fill(book.next_fill, o.order_id, t, o.instrument_id, o.side, qty, price, evidence)
        book.next_fill += 1
        book.fills.append(f)
        o.filled_qty += qty
        o.fill_value += qty * price
        o.trade_date = d
        if o.reserved:                                  # release the reserve pro rata (A4 §6.5)
            keep = numerics.floor_div(o.reserved * o.remaining, o.remaining + qty)
            book.ledger.reserved -= o.reserved - keep
            o.reserved = keep
        book.ledger.apply_fill(o.instrument_id, o.product, self._style(o.instrument_id, o.product), o.side, qty, price,
                               self._cnc_credit_ppb(), d)
        self._event(book, o, "FILL", f"{qty}@{price} {evidence}")
        book.callbacks.append(("fill", f))
        if o.remaining == 0:
            o.status = "COMPLETE"
            book.ledger.reserved -= o.reserved
            o.reserved = 0
            self._event(book, o, "COMPLETE")
            self._charge_order(book, o)

    def _charge_order(self, book: Book, o: Order) -> None:
        if o.charged or not o.filled_qty:
            return
        o.charged = True
        kind = self.inst[o.instrument_id]["kind"]
        parts = self.costs.order(kind, o.product, o.side, o.fill_value, o.trade_date)
        self._charge_row(book, "order", o.order_id, o.instrument_id, o.trade_date, parts)
        if o.product == "CNC" and o.side == "SELL" and (o.instrument_id, o.trade_date) not in book.dp_done:
            dp = self.costs.dp(o.trade_date)
            if dp:
                book.dp_done.add((o.instrument_id, o.trade_date))
                self._charge_row(book, "dp", 0, o.instrument_id, o.trade_date, dp)
        if o.tag in SYSTEM_TAGS:
            fee = self.costs.sqoff_fee(o.trade_date)
            if fee:
                self._charge_row(book, "sqoff_fee", o.order_id, o.instrument_id, o.trade_date, fee)

    def _charge_row(self, book: Book, kind: str, order_id: int, iid: int, d: str, parts: dict) -> None:
        tot = costs_mod.total(parts)
        book.charges.append({"kind": kind, "order_id": order_id, "instrument_id": iid, "trade_date": d, **parts,
                             "total": tot})
        book.ledger.debit(tot)

    def _eligible(self, iid: int, t_start: int) -> list[Order]:
        out = [o for o in self.live_orders()
               if o.instrument_id == iid and o.status in ACTIVE and o.t_activate <= t_start]
        return sorted(out, key=lambda o: (o.t_activate, o.order_id))

    def _band_ok(self, iid: int, price: int) -> bool:
        band = self.market.get(iid, Market()).band
        return band is None or band[0] <= price <= band[1]

    # bar model ---------------------------------------------------------------

    def _simulate_bar(self, book: Book, iid: int, bar: dict, high_first: bool, eligible_ids: list[int], tick: int,
                      lot: int) -> None:
        pts = path_points(bar["open"], bar["high"], bar["low"], bar["close"], high_first)
        touch = self.fill_cfg["limit_touch_policy"] == "touch"
        slip_cfg = self.fill_cfg["slippage"]

        opps = []
        for oid in eligible_ids:
            o = book.orders[oid]
            opp = bar_opportunity(o, pts, touch, lambda side, ref, o=o: protection_limit(
                side, ref, o.protection_bp if o.protection_bp is not None else self._bp(o), tick))
            if opp is not None:
                opps.append((opp.pos, o.t_activate, o.order_id, opp))
        opps.sort(key=lambda x: (x[0], x[1], x[2]))
        cap_left = participation_cap(bar["volume"], self.fill_cfg["participation_ppm"], lot)
        for _, _, oid, opp in opps:
            o = book.orders[oid]
            if opp.trigger:
                o.triggered = True
                o.status = "OPEN"
                o.limit = opp.new_limit
                o.from_market = opp.becomes_market
                self._event(book, o, "TRIGGER", f"at {opp.price if opp.price is not None else o.trigger}")
            if opp.price is None:
                continue
            price = slipped(o.side, opp.price, tick, slip_cfg, book.rng, opp.cap) if opp.slip else opp.price
            if opp.cap is not None and ((price > opp.cap) if o.side == "BUY" else (price < opp.cap)):
                continue
            if not self._band_ok(iid, price):
                continue
            qty = min(o.remaining, cap_left)
            if qty <= 0:
                continue
            cap_left -= qty
            self._fill(book, o, qty, price, opp.evidence, bar["t"])
        for oid in eligible_ids:
            book.orders[oid].first_eligible_done = True

    def _match_bar(self, iid: int, bar: dict) -> None:
        d = self._date(bar["start"])
        g = self.group_of(iid, d)
        cs = self.continuous_start(g, d)
        eff_start = max(bar["start"], cs) if cs is not None else bar["start"]
        eligible = [o.order_id for o in self._eligible(iid, eff_start)]
        if not eligible:
            return
        spec = self.spec(iid, d)
        tick, lot = spec["tick_paise"], spec["lot"]
        results = []
        for high_first in (True, False):                # P_HL, then P_LH
            sim = self.book.fork(iid, self.live_orders())
            self._simulate_bar(sim, iid, bar, high_first, eligible, tick, lot)
            nav = sim.ledger.nav(lambda i: bar["close"] if i == iid else self.mark_price(i))
            results.append((nav, high_first, sim))
        (nav_hl, _, sim_hl), (nav_lh, _, sim_lh) = results
        chosen = sim_hl if nav_hl < nav_lh else sim_lh   # ties -> P_LH
        filled = 0
        for f in chosen.fills:
            filled += f.qty
        if bar["volume"] and filled:
            self.participation_used = max(self.participation_used, numerics.floor_div(filled * 1_000_000, bar["volume"]))
        self._adopt(chosen, iid)

    def _adopt(self, sim: Book, iid: int) -> None:
        b = self.book
        b.ledger = sim.ledger
        for k, o in sim.orders.items():
            b.orders[k] = o
        b.rng = sim.rng
        b.fills += sim.fills
        b.charges += sim.charges
        b.order_events += sim.order_events
        b.callbacks += sim.callbacks
        b.dp_done = sim.dp_done
        b.next_fill = sim.next_fill

    # eod_close (A4 §4.9) -----------------------------------------------------

    def _match_eod(self, iid: int, bar: dict) -> None:
        d = self._date(bar["start"])
        g = self.group_of(iid, d)
        cs = self.continuous_start(g, d)
        eff_start = max(bar["start"], cs) if cs is not None else bar["start"]
        eligible = self._eligible(iid, eff_start)
        if not eligible:
            return                                       # also every index bar: indices are not tradable
        spec = self.spec(iid, d)
        tick, lot = spec["tick_paise"], spec["lot"]
        h = self.fill_cfg["half_spread_ticks"]
        cap_left = participation_cap(bar["volume"], self.fill_cfg["participation_ppm"], lot)
        c = bar["close"]
        for o in eligible:
            buy = o.side == "BUY"
            if o.status == "TRIGGER_PENDING":
                if (c >= o.trigger) if buy else (c <= o.trigger):
                    o.status, o.triggered = "OPEN", True
                    o.limit = o.price if o.type == "SL" else protection_limit(o.side, c, o.protection_bp or self._bp(o), tick)
                    o.from_market = o.type == "SL-M"
                    self._event(self.book, o, "TRIGGER", f"at close {c}")
                else:
                    continue
            price = c + h * tick if buy else c - h * tick
            if o.limit is not None:
                if (c > o.limit) if buy else (c < o.limit):
                    o.first_eligible_done = True
                    continue
                price = min(price, o.limit) if buy else max(price, o.limit)
            qty = min(o.remaining, cap_left)
            if qty <= 0 or not self._band_ok(iid, price):
                continue
            cap_left -= qty
            self._fill(self.book, o, qty, price, "eod_close", bar["t"])
            o.first_eligible_done = True
        # The fill is the close of `d`, published at 19:00, after an expiring contract's 16:00 FSP:
        # settle it now at that FSP rather than falling back at DAY_END.
        row = self.inst[iid]
        if row["kind"] in ("FUT", "CE", "PE") and (row.get("actual_expiry") or row["expiry"]) == d:
            fsp = self.fsp_seen.get((row["underlying_id"], d))
            if fsp is not None:
                self._settle_final(row["underlying_id"], d, fsp, "settle")

    # snapshot models (A4 §4.2, §4.3) -----------------------------------------

    def _evidence(self, m: Market) -> dict | None:
        s0, s1 = m.s0, m.s1
        if s0 is None or s1 is None:
            return None
        dv = numerics.check_int64(s1["volume"] - s0["volume"])
        if dv < 0:
            self.log("WARN", "day volume decreased; treated as 0")
            dv = 0
        lo, hi = min(s0["ltp"], s1["ltp"]), max(s0["ltp"], s1["ltp"])
        if s1["low"] < s0["low"]:
            lo = min(lo, s1["low"])
        if s1["high"] > s0["high"]:
            hi = max(hi, s1["high"])
        vwap = None
        if dv > 0:
            value = s1["atp"] * s1["volume"] - s0["atp"] * s0["volume"]
            if lo * dv <= value <= hi * dv:
                vwap = value
        return {"dv": dv, "lo": lo, "hi": hi, "value": vwap, "t0": s0["t"], "t1": s1["t"]}

    def _match_snapshot(self, iid: int, m: Market) -> None:
        kind = self.fill_cfg["kind"]
        d = self._date(self.now)
        spec = self.spec(iid, d)
        tick, lot = spec["tick_paise"], spec["lot"]
        stale_ns = self.bundle["pricing"]["stale_after_ms"] * 1_000_000
        ev = self._evidence(m)
        touch = self.fill_cfg["limit_touch_policy"] == "touch"
        cap_left = participation_cap(ev["dv"], self.fill_cfg["participation_ppm"], lot) if ev else 0
        working = [list(level) for level in m.book] if m.book else None
        t1 = m.s1["t"]
        for o in sorted(self._eligible(iid, t1), key=lambda o: (o.t_activate, o.order_id)):
            buy = o.side == "BUY"
            active_at_s0 = ev is not None and o.t_activate <= ev["t0"]
            if o.status == "TRIGGER_PENDING":
                if active_at_s0:
                    hit = (ev["hi"] >= o.trigger) if buy else (ev["lo"] <= o.trigger)
                else:
                    hit = (m.ltp >= o.trigger) if buy else (m.ltp <= o.trigger)
                if not hit:
                    continue
                o.status, o.triggered = "OPEN", True
                if o.type == "SL-M":
                    ref = o.trigger if ((m.ltp <= o.trigger) if buy else (m.ltp >= o.trigger)) else m.ltp
                    o.limit = protection_limit(o.side, ref, o.protection_bp or self._bp(o), tick)
                    o.from_market = True
                else:
                    o.limit = o.price
                self._event(self.book, o, "TRIGGER", f"ltp {m.ltp}")
            if o.limit is None or not (o.t_activate < t1):
                continue
            if kind == "snapshot_ltp":
                marketable = (o.from_market or ((m.ltp <= o.limit) if buy else (m.ltp >= o.limit)))
                if marketable and ev and ev["dv"] > 0 and active_at_s0:
                    if ev["value"] is not None:
                        base = (numerics.ceil_to(numerics.ceil_div(ev["value"], ev["dv"]), tick) if buy
                                else numerics.floor_to(numerics.floor_div(ev["value"], ev["dv"]), tick))
                        evidence = "vwap_int"
                    else:
                        base, evidence = m.ltp, "ltp"
                    price = slipped(o.side, base, tick, self.fill_cfg["slippage"], self.book.rng, o.limit)
                    if (price <= o.limit) if buy else (price >= o.limit):
                        qty = min(o.remaining, cap_left)
                        if qty > 0 and self._band_ok(iid, price):
                            cap_left -= qty
                            self._fill(self.book, o, qty, price, evidence, self.now)
                elif not marketable and ev and active_at_s0:
                    through = (ev["lo"] < o.limit if not touch else ev["lo"] <= o.limit) if buy else \
                              (ev["hi"] > o.limit if not touch else ev["hi"] >= o.limit)
                    if through:
                        qty = min(o.remaining, cap_left)
                        if qty > 0:
                            cap_left -= qty
                            self._fill(self.book, o, qty, o.limit, "trade_through", self.now)
            else:                                        # snapshot_quote
                if working is None or self.now - t1 > stale_ns:
                    continue
                side_levels = working[5:10] if buy else working[0:5]
                depth_ppm = self.fill_cfg["depth_take_ppm"]
                if not o.first_eligible_done:
                    for k, level in enumerate(side_levels, start=1):
                        price, qty_avail = level[0], level[1]
                        if price <= 0 or qty_avail <= 0:
                            continue
                        if (price > o.limit) if buy else (price < o.limit):
                            break
                        take = min(o.remaining, numerics.floor_to(numerics.floor_div(qty_avail * depth_ppm, 1_000_000), lot))
                        if take <= 0:
                            continue
                        level[1] -= take
                        self._fill(self.book, o, take, price, f"book_lvl_{k}", self.now)
                        if not o.remaining:
                            break
                else:
                    qa = 0
                    for level in side_levels:
                        if level[0] > 0 and ((level[0] <= o.limit) if buy else (level[0] >= o.limit)):
                            qa += level[1]
                    qa = numerics.floor_to(numerics.floor_div(qa * depth_ppm, 1_000_000), lot)
                    qb = 0
                    if ev and active_at_s0 and ((ev["lo"] < o.limit) if buy else (ev["hi"] > o.limit)):
                        qb = cap_left
                    q = min(o.remaining, max(qa, qb))
                    if q > 0:
                        evidence = "book_cross" if qa >= qb else "trade_through"
                        if evidence == "trade_through":
                            cap_left -= q
                        self._fill(self.book, o, q, o.limit, evidence, self.now)
            o.first_eligible_done = True

    def _after_market(self, iid: int) -> None:
        """IOC remainders are cancelled right after their first eligible event (A4 §3.7)."""
        for o in self.live_orders():
            if o.instrument_id == iid and o.validity == "IOC" and o.status in ACTIVE and o.first_eligible_done:
                self._cancel(self.book, o, "IOC")

    # ── Event handlers ───────────────────────────────────────────────────────

    def _on_market(self, rec: dict) -> None:
        iid = rec["instrument_id"]
        m = self.market.setdefault(iid, Market())
        d = self._date(rec["t_event"])
        if m.day != d:
            m.day, m.s0, m.s1 = d, None, None
        kind = rec["kind"]
        if kind == "BAR":
            isec = rec["interval_s"]
            if m.min_interval is None or isec < m.min_interval:
                m.min_interval = isec
            m.bars.setdefault(isec, []).append((rec["start_ns"], rec["open"], rec["high"], rec["low"], rec["close"],
                                                rec["volume"], rec["oi"]))
            m.oi = rec["oi"]
            fill_bar = isec == m.min_interval
            bar = {"start": rec["start_ns"], "open": rec["open"], "high": rec["high"], "low": rec["low"],
                   "close": rec["close"], "volume": rec["volume"], "t": rec["t_event"]}
            if fill_bar:
                if self.fill_cfg["kind"] == "bar":
                    self._match_bar(iid, bar)
                elif self.fill_cfg["kind"] == "eod_close":
                    self._match_eod(iid, bar)
                m.ltp, m.t = rec["close"], rec["t_event"]
                self._underlying_seen(iid, rec["close"])
                bar_day = self._date(rec["start_ns"])
                for o in self.live_orders():               # D-44: the session's daily bar has now been used
                    if o.instrument_id == iid and o.eod_after is not None and o.eod_after <= bar_day:
                        self._cancel(self.book, o, "EOD")
            self._after_market(iid)
            self._deliver()
            if iid in self.sub_bars and isec in self.sub_bars[iid] and self._hook_on("on_bar"):
                self.strategy.on_bar(self.ctx, Bar(iid, isec, rec["start_ns"], rec["t_event"], rec["open"], rec["high"],
                                                   rec["low"], rec["close"], rec["volume"], rec["oi"]))
                self._deliver()
            return
        if kind == "INDEX":
            m.ltp, m.t = rec["value"], rec["t_event"]
            self._underlying_seen(iid, rec["value"])
            self._build_bars(iid, rec["t_event"], rec["value"], None, None)
            if iid in self.sub_ticks and self._hook_on("on_tick"):
                self.strategy.on_tick(self.ctx, Tick(iid, rec["t_event"], rec["value"], 0, 0, None, None))
                self._deliver()
            return
        snap = {"t": rec["t_event"], "ltp": rec["ltp"], "volume": rec["volume"], "atp": rec["atp"],
                "high": rec["high"], "low": rec["low"]}
        m.s0, m.s1 = m.s1, snap
        m.ltp, m.t = rec["ltp"], rec["t_event"]
        if kind == "TICK_FULL":
            m.oi = rec["oi"]
            m.book = [(p, q, n) for p, q, n in rec["depth"]]
            bid = rec["depth"][0][0] or None
            ask = rec["depth"][5][0] or None
            m.bid, m.ask = bid, ask
        if self.fill_cfg["kind"] in ("snapshot_ltp", "snapshot_quote"):
            if self.fill_cfg["kind"] == "snapshot_quote" and kind != "TICK_FULL":
                pass
            else:
                self._match_snapshot(iid, m)
        self._underlying_seen(iid, rec["ltp"])
        prev_vol = m.s0["volume"] if m.s0 else None
        self._build_bars(iid, rec["t_event"], rec["ltp"], rec["volume"], prev_vol)
        self._after_market(iid)
        self._deliver()
        if iid in self.sub_ticks and self._hook_on("on_tick"):
            self.strategy.on_tick(self.ctx, Tick(iid, rec["t_event"], rec["ltp"], rec["volume"], rec["atp"], m.bid,
                                                 m.ask))
            self._deliver()

    def _underlying_seen(self, iid: int, price: int) -> None:
        self.underlying_last[iid] = price

    def _build_bars(self, iid: int, t: int, price: int, vol: int | None, prev_vol: int | None) -> None:
        """BAR_BUILT from snapshots (A4 §2.2) for subscribed tick instruments."""
        for isec in sorted(self.sub_bars.get(iid, ())):
            if self.market[iid].min_interval is not None:
                continue                                  # the pack has real bars for it
            d = self._date(t)
            cs = self.continuous_start(self.group_of(iid, d), d)
            if cs is None or t < cs:
                continue
            step = isec * 1_000_000_000
            start = bucket_start(cs, t, isec)
            acc = self.built.setdefault((iid, isec), {})
            dv = volume_delta(vol, prev_vol)
            if start not in acc:
                acc[start] = [price, price, price, price, dv, 0]
                self.push(numerics.check_int64(start + step), BAR_BUILT, "BAR_BUILT", (iid, isec, start))
            else:
                b = acc[start]
                b[1], b[2], b[3] = max(b[1], price), min(b[2], price), price
                b[4] = numerics.check_int64(b[4] + dv)


    def _on_gap(self, rec: dict) -> None:
        targets = [rec["instrument_id"]] if rec["scope"] == 1 else list(self.market)
        for iid in targets:
            m = self.market.get(iid)
            if m:
                m.s0, m.s1 = None, None
        self.log("INFO", f"GAP {rec['reason']} {'start' if rec['phase'] == 0 else 'end'} scope {rec['instrument_id']}")

    def _on_refdata(self, rec: dict) -> None:
        if rec["subtype"] == "strategy_control":
            self._on_strategy_control(rec["payload"])
            return
        if rec["subtype"] == "margin_requirement":     # MEZ 5.1: checked under every model, kept by the live one
            from stockify.fno import live_margin
            if rec["instrument_id"] != 0:
                raise RunError("margin_requirement: the record's instrument must be 0")
            try:
                live_margin.check_payload(rec["payload"])
            except live_margin.PayloadError as exc:
                raise RunError(f"margin_requirement: {exc}") from exc
            if self.live_answers is not None:
                self.live_answers.put(rec["payload"], self.now)
            return
        if rec["subtype"] == "price_band":
            pl = rec["payload"]
            self.market.setdefault(rec["instrument_id"], Market()).band = (pl["lower_paise"], pl["upper_paise"])

    def _on_settle(self, rec: dict) -> None:
        iid, price = rec["instrument_id"], rec["price_paise"]
        if rec["subtype"] == 1:                          # daily settlement price of a contract
            for (pid, prod), p in self.book.ledger.positions.items():
                if pid == iid and p.q and p.style == "variation" and self.inst[iid]["kind"] == "FUT":
                    self.book.ledger.settle_variation(p, price, "settle")
            return
        self.fsp_seen[(iid, rec["date"])] = price
        self._settle_final(iid, rec["date"], price, "settle")

    def _expiring(self, underlying_id: int, d: str) -> list:
        out = []
        for (iid, prod), p in self.book.ledger.positions.items():
            row = self.inst[iid]
            if p.q and row["kind"] in ("FUT", "CE", "PE") and row["underlying_id"] == underlying_id and \
                    (row.get("actual_expiry") or row["expiry"]) == d:
                out.append(p)
        return out

    def _settle_final(self, underlying_id: int, d: str, fsp: int, source: str) -> None:
        """Settlement at the FSP: in cash (A4 §7.2), or by delivery of the stock (§7.3)."""
        expiring = self._expiring(underlying_id, d)
        physical = [p for p in expiring if self.inst[p.instrument_id]["settlement"] == "physical"]
        if physical:
            self._settle_physical(underlying_id, d, fsp, source, physical)
        for p in expiring:
            row = self.inst[p.instrument_id]
            if row["settlement"] == "physical":
                continue
            if row["settlement"] != "cash":
                raise RunError(f"{row['tradingsymbol']}: unknown settlement {row['settlement']!r}")
            led = self.book.ledger
            closing_side = "SELL" if p.d > 0 else "BUY"
            units = p.q
            if row["kind"] == "FUT":
                self.settle_closes.append((self.now, p.instrument_id, p.product, closing_side, units, fsp))
                led.settle_variation(p, fsp, source)
                p.realised = p.pnl(fsp)
                p.vm = p.realised
                p.q, p.c = 0, 0
            else:
                k = row["strike_paise"]
                intrinsic = max(0, numerics.check_int64(fsp - k)) if row["kind"] == "CE" else max(0, numerics.check_int64(k - fsp))
                self.settle_closes.append((self.now, p.instrument_id, p.product, closing_side, units, intrinsic))
                led.cash += p.qty * intrinsic
                p.realised = numerics.traded_pnl(p.realised, p.d, p.q * intrinsic, p.c)
                # D-59: brokerage on every option open at expiry (exercised, assigned or expired);
                # exercise STT only on the intrinsic value of a long ITM one
                exercised = p.q * intrinsic if p.d > 0 else 0
                parts = self.costs.exercise(row["kind"], exercised, d)
                self._charge_row(self.book, "settlement", 0, p.instrument_id, d, parts)
                p.q, p.c = 0, 0
                p.last_mark, p.mark_source = intrinsic, source
            self.log("INFO", f"settled {p.instrument_id} {p.product} at FSP {fsp} ({source})")

    def _settle_physical(self, stock: int, d: str, fsp: int, source: str, positions: list) -> None:
        """Stock F&O at expiry (A4 §7.3, D-58): close each position, then deliver the stock as CNC trades."""
        if stock not in self.inst or self.inst[stock]["kind"] != "EQ":
            raise RunError(f"{self.inst[positions[0].instrument_id]['tradingsymbol']}: physical settlement needs "
                           f"its underlying stock in the bundle")
        led = self.book.ledger
        legs = []                                   # (take, units, price, contract id, kind, exercised intrinsic)
        for p in sorted(positions, key=lambda p: (p.instrument_id, p.product)):
            row = self.inst[p.instrument_id]
            closing_side = "SELL" if p.d > 0 else "BUY"
            units = p.q
            if row["kind"] == "FUT":
                self.settle_closes.append((self.now, p.instrument_id, p.product, closing_side, units, fsp))
                led.settle_variation(p, fsp, source)
                p.realised = p.pnl(fsp)
                p.vm = p.realised
                legs.append((p.d > 0, units, fsp, p.instrument_id, row["kind"], 0))
            else:
                k = row["strike_paise"]
                self.settle_closes.append((self.now, p.instrument_id, p.product, closing_side, units, 0))
                p.realised += p.d * (0 - p.c)        # the option closes at 0: its premium is its P&L
                p.last_mark, p.mark_source = 0, source
                itm = fsp > k if row["kind"] == "CE" else fsp < k
                if itm:
                    take = (row["kind"] == "CE") == (p.d > 0)
                    exercised = units * abs(fsp - k) if p.d > 0 else 0
                    legs.append((take, units, k, p.instrument_id, row["kind"], exercised))
                else:                                # D-59: an expiring OTM option pays the expiry brokerage
                    self._charge_row(self.book, "settlement", 0, p.instrument_id, d,
                                     self.costs.exercise(row["kind"], 0, d))
            p.q, p.c = 0, 0
            self.log("INFO", f"settled {p.instrument_id} {p.product} at FSP {fsp} ({source})")
        netted = {True: min(sum(u for t, u, *_ in legs if t), sum(u for t, u, *_ in legs if not t))}
        netted[False] = netted[True]
        for take, units, price, iid, kind, exercised in legs:
            n = min(units, netted[take])
            netted[take] -= n
            side = "BUY" if take else "SELL"
            led.apply_fill(stock, "CNC", self._style(stock, "CNC"), side, units, price, self._cnc_credit_ppb(), d)
            held = led.position(stock, "CNC")
            if held is not None and held.qty < 0:
                self.labels.add("short_delivery")
            parts = self.costs.delivery(kind, units * price, n * price, exercised, d)
            self._charge_row(self.book, "settlement", 0, iid, d, parts)
            self.log("INFO", f"delivered {stock} CNC {side} {units} at {price} ({source})")

    def _on_phase(self, payload: tuple) -> None:
        g, phase = payload
        if g == "CM_CAS" and phase == "CAS" and self.policies["cas"] == "none":
            d = self._date(self.now)
            for o in list(self.book.orders.values()):
                if o.status in ("PENDING", "OPEN", "TRIGGER_PENDING") and self.group_of(o.instrument_id, d) == "CM_CAS":
                    self._cancel(self.book, o, "R_CAS")
        if self._hook_on("on_session_phase"):
            self.strategy.on_session_phase(self.ctx, PhaseChange(g, phase, self.now))
        self._deliver()

    def _daily_bars(self, iid: int) -> bool:
        m = self.market.get(iid)
        return m is not None and m.min_interval is not None and m.min_interval >= 86400

    def _seg_close(self, payload: tuple) -> None:
        g, d = payload
        for o in self.live_orders():
            if o.validity == "DAY" and self.group_of(o.instrument_id, d) == g and (o.trade_date or d) <= d:
                if o.status == "PENDING" and o.t_activate > self.now:
                    continue                              # queued for a later session
                if self._daily_bars(o.instrument_id):
                    o.eod_after = d                       # D-44: cancelled once today's daily bar is matched
                    continue
                self._cancel(self.book, o, "EOD")
        if g in ("FO_FUT", "FO_OPT"):
            for (iid, prod), p in self.book.ledger.positions.items():
                if p.q and p.style == "variation" and self.inst[iid]["session_group"] == g and \
                        self.inst[iid]["kind"] == "FUT":
                    m = self.mark_price(iid)
                    if m is not None:
                        self.book.ledger.settle_variation(p, m, "ltp")
        self._deliver()

    def _mis_sqoff(self, payload: tuple) -> None:
        seg, d = payload
        for (iid, prod), p in sorted(self.book.ledger.positions.items()):
            if prod != "MIS" or not p.q:
                continue
            if _cutoff_key(self.group_of(iid, d)) != seg:
                continue
            side = "SELL" if p.d > 0 else "BUY"
            self.place(iid, side, p.q, "MIS", "MARKET", tag="AUTO_SQOFF", system=True,
                       note=self.system_notes.get("AUTO_SQOFF", ""))
        self._deliver()

    def _day_end(self, d: str) -> None:
        # expiry fallback (A4 §7.2): no FSP by DAY_END
        for (iid, prod), p in list(self.book.ledger.positions.items()):
            row = self.inst[iid]
            if p.q and row["kind"] in ("FUT", "CE", "PE") and (row.get("actual_expiry") or row["expiry"]) == d:
                uid = row["underlying_id"]
                last = self.underlying_last.get(uid) if uid else None
                if last is None:
                    raise RunError(f"{row['tradingsymbol'] or iid}: expired on {d} with no FSP and no underlying price")
                self.labels.add("settlement_fallback")
                self._settle_final(uid, d, last, "fallback")
        led = self.book.ledger
        for (iid, prod), p in sorted(led.positions.items()):
            if not p.q:
                continue
            m = self.mark_price(iid)
            src = self.run_cfg["mark"] if m is not None else "last"
            if m is None:
                m = p.last_mark if p.last_mark is not None else p.avg_price
            unreal = p.d * (p.q * m - p.c)
            row = self.inst[iid]
            # D-55: a derivative's underlying mark at day end (the run's mark rule), for Greeks in the API
            uid = row.get("underlying_id") if row["kind"] in ("FUT", "CE", "PE") else None
            under = self.mark_price(uid) if uid is not None else None
            self.positions_eod.append((d, iid, prod, p.qty, p.avg_price, m, src, unreal, p.realised, under))
        nav = led.nav(self.mark_price)
        blocked = self.blocked(led, d)
        self.equity.append((d, nav, led.cash, blocked, nav - led.capital + led.charges_total, led.charges_total))
        if hasattr(self.margin, "breakdown"):       # per-underlying day rows (M4.8; flat writes none)
            for b in self.margin.breakdown(self._fo_book(led), self.inst.__getitem__, self._ref_price, d):
                self.margin_daily.append((d, b.underlying_id, self.margin_model, b.step, b.scan, b.somc, b.elm,
                                          b.addons, b.total))
        if self._hook_on("on_session_end"):
            self.strategy.on_session_end(self.ctx)
        self._deliver()
        led.release_day(self._next_date(d))
        if d in self.day_by_date:                     # not the D-52 close-out's own (maybe non-trading) date
            self.state_date = d
        if self.day_bundle:                           # paper sessions: save_state() at DAY_END, checked (A4 §1.10)
            from stockify.oracle import sod_state
            try:
                self.day_end_strategy_state = sod_state.strategy_state(self.strategy)
            except sod_state.StateError as e:
                raise RunError(str(e)) from e
        self.settled = True                           # until the next event: eod_state() is the end of this day

    def _next_date(self, d: str) -> str:
        return _date.fromordinal(_date.fromisoformat(d).toordinal() + 1).isoformat()

    def _on_timer(self, timer_id: int) -> None:
        spec = self.timers.get(timer_id)
        if spec is None:
            return
        t, period = spec
        if period:
            t_next = numerics.check_int64(t + period)
            self.timers[timer_id] = (t_next, period)
            self.push(t_next, TIMER, "TIMER", timer_id)
        else:
            del self.timers[timer_id]
        if self._hook_on("on_timer"):
            self.strategy.on_timer(self.ctx, timer_id)
        self._deliver()

    def _on_built(self, payload: tuple) -> None:
        iid, isec, start = payload
        b = self.built.get((iid, isec), {}).pop(start, None)
        if b is None:
            return
        if self.market[iid].min_interval is not None:
            return                                        # real bars arrived while it was building: never both (ST 5.9)
        self.market[iid].bars.setdefault(isec, []).append((start, b[0], b[1], b[2], b[3], b[4], b[5]))
        if self._hook_on("on_bar"):
            self.strategy.on_bar(self.ctx, Bar(iid, isec, start, self.now, b[0], b[1], b[2], b[3], b[4], b[5],
                                               built=True))
        self._deliver()

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self) -> None:
        self._enter("run")
        try:
            self._run()
        finally:
            self.busy = None

    def _run(self) -> None:
        self.started = True
        self.settled = False
        self.day_end_strategy_state = None            # a loaded state's strategy part stands in only until the run starts
        self._schedule_days()
        first = [self.internal[0][0][0]] + ([self.records[0][0][0]] if self.records else [])
        self.now = min(first)                     # on_start sees the earliest time of the run
        self._reseed_for(self.now)
        if not self.resumed:                      # a resumed day ran strategy.load_state instead (A4 §1.10)
            self.strategy.on_start(self.ctx)
            self._deliver()
        i = 0
        last_day_end = self.days[-1]["day_end_ns"]
        while i < len(self.records) or self.internal:
            nxt_in = self.records[i] if i < len(self.records) else None
            nxt_int = self.internal[0] if self.internal else None
            if nxt_int is not None and (nxt_in is None or nxt_int[0] < nxt_in[0]):
                key, kind, payload = heapq.heappop(self.internal)
                if key[0] > last_day_end:                 # timers after the last day never fire here;
                    self.carried.append((key, kind, payload))   # a paper session's next day gets them (sod_state)
                    continue
                self.now, self.key = key[0], key
                self.settled = False
                self._reseed_for(self.now)
                self._activate_due(self.now)
                self._dispatch_internal(kind, payload)
            else:
                key, rec = nxt_in
                i += 1
                self.now, self.key = key[0], key
                self.settled = False
                self._reseed_for(self.now)
                self._activate_due(self.now)
                self._dispatch_input(rec)
        if self.now > last_day_end:                     # D-52: the final close-out (A4 §1.10)
            self.internal_seq += 1
            self.key = (self.now, DAY_END, 0, self.internal_seq)
            self.settled = False
            self._activate_due(self.now)
            self._dispatch_internal("DAY_END", self._date(self.now))
        self.labels |= self.margin.labels                # e.g. delivery_margin_approx (D-60)

    def _track_open(self) -> None:
        is_open = False
        for p in self.book.ledger.positions.values():
            if p.q:
                is_open = True
                break
        if (self.open_changes[-1][1] if self.open_changes else False) != is_open:
            self.open_changes.append((self.now, is_open))

    def exposure(self) -> float | None:
        """Continuous-phase seconds with any open position / all continuous-phase seconds (A4 §8)."""
        groups = sorted({self.inst[i]["session_group"] for i in self.inst if self.inst[i]["kind"] != "INDEX"}) or ["CM"]
        total = held = 0
        for day in self.days:
            spans = []
            for g in groups:
                spans += [(p["start_ns"], p["end_ns"]) for p in day["groups"][g] if p["phase"] == "CONTINUOUS"]
            spans.sort()
            merged = []
            for a, b in spans:
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            for a, b in merged:
                total = numerics.check_int64(total + numerics.check_int64(b - a))
                state = False
                cursor = a
                for t, flag in self.open_changes:
                    if t <= a:
                        state = flag
                        continue
                    if t >= b:
                        break
                    if state:
                        held = numerics.check_int64(held + numerics.check_int64(t - cursor))
                    cursor, state = t, flag
                if state:
                    held = numerics.check_int64(held + numerics.check_int64(b - cursor))
        return held / total if total else None

    def _dispatch_internal(self, kind: str, payload) -> None:  # noqa: ANN001
        self._dispatch_internal_kind(kind, payload)
        self._track_open()

    def _dispatch_internal_kind(self, kind: str, payload) -> None:  # noqa: ANN001
        if kind == "PHASE":
            self._on_phase(payload)
        elif kind == "SEG_CLOSE":
            self._seg_close(payload)
        elif kind == "MIS_SQOFF":
            self._mis_sqoff(payload)
        elif kind == "DAY_END":
            self._day_end(payload)
        elif kind == "TIMER":
            self._on_timer(payload)
        elif kind == "BAR_BUILT":
            self._on_built(payload)

    def _dispatch_input(self, rec: dict) -> None:
        k = rec["kind"]
        if k in ("BAR", "TICK_FULL", "TICK_QUOTE", "INDEX"):
            if rec["instrument_id"] not in self.inst:
                raise RunError(f"pack record for instrument {rec['instrument_id']} not in the bundle")
            self.event_no += 1
            self._on_market(rec)
        elif k == "GAP":
            self._on_gap(rec)
        elif k == "REFDATA":
            self._on_refdata(rec)
        elif k == "SETTLE":
            self._on_settle(rec)
        elif k == "CLOCK":
            pass
        self._deliver()
        self._track_open()


def load_records(pack_paths: list) -> list[tuple]:  # noqa: ANN001
    """Every record of every pack with its key (t, rank, pack index, seq), merged (A4 §1.3)."""
    from stockify.data import packs
    out = []
    for src, path in enumerate(pack_paths):
        _, recs = packs.read(path)
        for r in recs:
            out.append(((r["t_event"], RANK[r["kind"]], src, r["seq"]), r))
    out.sort(key=lambda x: x[0])
    return out


def position_view(p) -> PositionView | None:  # noqa: ANN001
    return PositionView(p.instrument_id, p.product, p.qty, p.avg_price, p.realised) if p else None
