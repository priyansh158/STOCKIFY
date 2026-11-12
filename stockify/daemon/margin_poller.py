"""IWB 5.1 (M5.6; D-69; spec §5.3, §9): asking Kite what the paper book's margin is, and logging every disagreement.

The poller is a thread beside the dispatcher's engine thread and never blocks it. It holds no engine: after each batch
the engine thread, when it is time, reads `Engine.margin_now()` and puts a `MarginSnapshot` (plain data) on
`snapshots` with `put_latest`; this thread takes the snapshots, asks Kite, and hands the answer back through the
dispatcher's queue like any producer (`put_inject`, stamped by this thread's clock). Network calls happen here and
nowhere else.

When a request goes out for a session's book:

* a fill pokes the poller (`poke`): one request `margin_poll.debounce_s` after the first poke of a burst, priced from
  the newest snapshot, so a burst of fills costs one request;
* otherwise every `margin_poll.every_s` of stamp time the dispatcher puts a snapshot (`reason="periodic"`) while the
  book has legs, and each one is asked at once;
* never more than `margin_poll.max_rps` requests in any one second, counted over a sliding second.

Each answer is `POST /margins/basket` through `ReadOnlyKite` (`consider_positions=False`, always) with the book's
staircase legs as NRML market orders (`calibrate.kite_orders`' shape). It is converted to integer paise
(`Decimal(str(x)) * 100`, the `calibrate.kite_target` rule), checked with `live_margin.check_payload`, journaled as a
REFDATA `margin_requirement` record through `dispatcher.put_inject` (journal first, D-68), and only then written to
`margin_live` beside the model's own answer (`model_json` is the staircase rows) and the gap between them. The row
also keeps what the X-9 expiry-day add-on check needs later: whether a leg expires that day, each leg's contract, the
SPAN file in force and its age. An error (403, 429, network, an answer that is not a valid payload, a contract
refdata does not know) writes a row with the redacted error text and no journal record; a 403 also calls
`on_token_rejected`, which starts the daemon's token-expiry path (Task 19).

In a replay the poller does nothing: Kite's answers are already in the journal.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from collections import deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Callable

from stockify import store, timeutil
from stockify.daemon.clock import Clock
from stockify.data import packs
from stockify.fno import live_margin
from server.log_redaction import redact

log = logging.getLogger("stockify.daemon.margin_poller")

SEC = 1_000_000_000
STEP_CURRENT = "current"
ERROR_MAX = 300                                     # characters of redacted error text kept
IDLE_WAIT_S = 0.5                                   # the longest the thread sleeps with nothing due
MIN_WAIT_S = 0.001
BOUNDS = {"debounce_s": 60, "every_s": 86_400, "max_rps": 100}      # the loader's PAPER_BOUNDS, checked again here
ANSWER_KEYS = (("span", "span_paise"), ("exposure", "exposure_paise"), ("additional", "additional_paise"),
               ("option_premium", "option_premium_paise"))


@dataclass(frozen=True)
class MarginSnapshot:
    """The book as `Engine.margin_now()` saw it after a batch: plain data, never an engine."""
    session_id: str
    engine_ns: int                       # the dispatcher's stamp for the batch
    legs: list                           # [(instrument id, signed units)] the hash covers; empty when flat
    portfolio_sha256: str
    fallback: list                       # the staircase's rows: {underlying_id, step, scan, somc, elm, addons, total}
    reason: str = "periodic"             # "poke" (a fill; wait out the debounce) or "periodic" (ask at once)


def put_latest(q: queue.Queue, item) -> None:  # noqa: ANN001
    """`put_nowait`, and when the queue is full drop its oldest item: the newest snapshot is the one that matters."""
    while True:
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass


def gap_ppm(gap: int, kite: int) -> int | None:
    """gap / kite in parts per million, a whole number rounded half away from zero (None when Kite's is not above 0)."""
    if kite <= 0:
        return None
    q = (abs(gap) * 2_000_000 + kite) // (2 * kite)
    return q if gap >= 0 else -q


def kite_parts(answer) -> dict[str, int]:  # noqa: ANN001
    """Kite's `final` span, exposure, additional and option premium as paise (`Decimal(str(x)) * 100`, as
    `calibrate.kite_target` does); ValueError for an answer that has no such block or a figure that is not a number."""
    final = answer.get("final") if isinstance(answer, dict) else None
    if not isinstance(final, dict):
        raise ValueError("Kite's answer has no final block")
    out = {}
    for key, name in ANSWER_KEYS:
        value = final.get(key)
        if value is None:
            value = 0
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            raise ValueError(f"Kite's final.{key} is not a number")
        try:
            paise = Decimal(str(value)) * 100
            out[name] = int(paise.to_integral_value())
        except (InvalidOperation, ValueError, OverflowError) as e:
            raise ValueError(f"Kite's final.{key} is not a number") from e
    return out


def refdata_contracts(iids: list[int]) -> list[dict | None]:
    """The refdata row of each instrument (None for one it does not know); the poller's default `contracts`."""
    from stockify.data import instruments
    return [instruments.get(i) for i in iids]


def default_span_info(trade_date: str, now_ns: int) -> tuple[str, int] | None:
    """(name, created_ns) of the SPAN file in force at `now_ns` on the trade date, from the archive; None if none."""
    from pathlib import Path
    from stockify.data import nse_margin_files as mf
    try:
        usable = [(p, c) for p, c in mf.usable_files(date.fromisoformat(trade_date)) if c <= now_ns]
    except Exception:  # noqa: BLE001 — a missing archive only leaves the age out of the log
        return None
    return (Path(usable[-1][0]).name, usable[-1][1]) if usable else None


def portfolio_type(rows: list[tuple[dict, int]]) -> str:
    """A name for the book's shape, by the legs' kinds (the `calibrate.TYPES` names; "other" when none fits)."""
    options = [(r, u) for r, u in rows if r.get("kind") in ("CE", "PE")]
    futures = [r for r, _ in rows if r.get("kind") == "FUT"]
    if not options:
        return "future" if futures else "other"
    if futures:
        return "hedged_future"
    if len(options) == 1:
        return "single"
    kinds = sorted(r["kind"] for r, _ in options)
    if len(options) == 2:
        if kinds == ["CE", "PE"]:
            same = options[0][0].get("strike_paise") == options[1][0].get("strike_paise")
            return "straddle" if same else "strangle"
        return "vertical"
    if len(options) == 4 and kinds == ["CE", "CE", "PE", "PE"]:
        sold = {r.get("strike_paise") for r, u in options if u < 0}
        return "iron_fly" if len(sold) == 1 else "iron_condor"
    return "other"


class MarginPoller(threading.Thread):
    """`MarginPoller(snapshots, dispatcher, client_factory=..., clock=..., cfg=...)`; see the module doc.

    `cfg` is paper.json (its `margin_poll` block is read). `client_factory()` returns a `ReadOnlyKite`; it is called
    for every request (the login can change) on this thread. `contracts(iids)` maps instrument ids to refdata rows
    (default: refdata) and `span_info(trade_date, now_ns)` names the SPAN file in force. `step()` is one instant of
    the thread's loop: tests call it directly."""

    def __init__(self, snapshots: queue.Queue, dispatcher, *, client_factory: Callable, clock: Clock,  # noqa: ANN001
                 cfg: dict, contracts: Callable[[list[int]], list] | None = None,
                 span_info: Callable[[str, int], tuple[str, int] | None] | None = None,
                 on_token_rejected: Callable[[], None] | None = None) -> None:
        super().__init__(name="margin-poller", daemon=True)
        poll = cfg["margin_poll"]
        for key, top in BOUNDS.items():
            value = poll[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
                raise ValueError(f"margin_poll.{key} must be a whole number from 1 to {top}, got {value!r}")
        self.snapshots = snapshots
        self.dispatcher = dispatcher
        self.client_factory = client_factory
        self.clock = clock
        self.debounce_ns = poll["debounce_s"] * SEC
        self.every_ns = poll["every_s"] * SEC
        self.max_rps = poll["max_rps"]
        self.contracts = contracts or refdata_contracts
        self.span_info = span_info or default_span_info
        self.on_token_rejected = on_token_rejected
        self.disabled = bool(getattr(dispatcher, "replay", False))     # a replay's answers are in the journal
        self.requests = 0
        self._lock = threading.Lock()
        self._pending: dict[str, int] = {}              # session -> clock ns of the first poke not yet answered
        self._latest: dict[str, MarginSnapshot] = {}
        self._unused: set[str] = set()                  # sessions whose newest periodic snapshot is not yet asked
        self._window: deque[int] = deque()              # the clock ns of the requests of the last second
        self._halt = threading.Event()
        self._wake = threading.Event()

    # ── the engine thread's side ─────────────────────────────────────────────
    def poke(self, session_id: str) -> None:
        """A fill: ask once, `debounce_s` after the first poke of a burst. Cheap; any thread."""
        if self.disabled:
            return
        with self._lock:
            self._pending.setdefault(session_id, self.clock.now_ns())
        self._wake.set()

    def pending(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._pending

    # ── this thread ──────────────────────────────────────────────────────────
    def stop(self) -> None:
        self._halt.set()
        self._wake.set()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                sent = self.step()
            except Exception:  # noqa: BLE001 — a failing poll never ends the thread
                log.exception("the margin poller's step failed")
                sent = 0
            if sent:
                continue
            self._wake.wait(self._next_wait_s())
            self._wake.clear()

    def _drain(self) -> None:
        while True:
            try:
                s = self.snapshots.get_nowait()
            except queue.Empty:
                return
            if self.disabled:
                continue
            with self._lock:
                self._latest[s.session_id] = s
                if s.reason == "periodic":
                    self._unused.add(s.session_id)

    def _room(self, now: int) -> bool:
        while self._window and self._window[0] <= now - SEC:
            self._window.popleft()
        return len(self._window) < self.max_rps

    def step(self) -> int:
        """Take what is queued, then send every request that is due and fits the rate; returns how many were sent."""
        self._drain()
        if self.disabled:
            return 0
        now = self.clock.now_ns()
        with self._lock:
            due = [(first, sid) for sid, first in self._pending.items()
                   if now - first >= self.debounce_ns and sid in self._latest]
            work = [sid for _, sid in sorted(due)]
            work += sorted(sid for sid in self._unused if sid not in work)
        sent = 0
        for sid in work:
            if not self._room(now):
                break
            with self._lock:
                snap = self._latest.get(sid)
                self._pending.pop(sid, None)
                self._unused.discard(sid)
            if snap is None or not snap.legs:
                continue                                # flat: nothing to ask
            sent += self._ask(snap, now)
            now = self.clock.now_ns()
        return sent

    def _next_wait_s(self) -> float:
        now = self.clock.now_ns()
        waits = [IDLE_WAIT_S]
        rate = max(0, self._window[0] + SEC - now) if len(self._window) >= self.max_rps else 0
        with self._lock:
            if self._unused:
                waits.append(rate / SEC)
            for first in self._pending.values():
                waits.append(max(first + self.debounce_ns - now, rate, 0) / SEC)
        return max(min(waits), MIN_WAIT_S)

    # ── one request ──────────────────────────────────────────────────────────
    def _ask(self, snap: MarginSnapshot, request_ns: int) -> int:
        """One book: a Kite request (returns 1) or, for a contract refdata does not know, an error row (returns 0)."""
        ours = sum(int(r["total"]) for r in snap.fallback)
        row = {"session_id": snap.session_id, "wall_ns": request_ns, "portfolio_sha256": snap.portfolio_sha256,
               "model_json": json.dumps(snap.fallback, sort_keys=True), "engine_ns": snap.engine_ns,
               "trade_date": timeutil.ns_to_ist(snap.engine_ns).date().isoformat(),
               "legs_json": json.dumps([list(x) for x in snap.legs]), "step": STEP_CURRENT,
               "ours_total_paise": ours, "request_ns": request_ns}
        try:
            rows = self._contract_rows(snap)
        except Exception as e:  # noqa: BLE001
            self._error_row(row, f"unknown contract: {e}")
            return 0
        info = self.span_info(row["trade_date"], request_ns)
        row["span_file"] = info[0] if info else None
        row["market_json"] = json.dumps(self._market(snap, rows, row["trade_date"], info, request_ns), sort_keys=True)
        self._window.append(request_ns)                 # from here a request goes out: it counts against max_rps
        self.requests += 1
        try:
            client = self.client_factory()
            answer = client.basket_order_margins(self._orders(rows), consider_positions=False)
        except Exception as e:  # noqa: BLE001 — 403, 429, the network: all become a row
            row["response_ns"] = self.clock.now_ns()
            self._error_row(row, f"{type(e).__name__}: {e}")
            from stockify.daemon.ticker import is_auth_failure
            if is_auth_failure(getattr(e, "code", None), e) and self.on_token_rejected is not None:
                try:
                    self.on_token_rejected()
                except Exception:  # noqa: BLE001
                    log.exception("starting the token-expiry path failed")
            return 1
        response_ns = self.clock.now_ns()
        row["response_ns"] = response_ns
        try:
            payload = live_margin.check_payload({
                "portfolio_sha256": snap.portfolio_sha256, "legs": [list(x) for x in snap.legs],
                **kite_parts(answer), "requested_ns": request_ns, "source": live_margin.SOURCE})
        except (ValueError, live_margin.PayloadError) as e:
            self._error_row(row, f"answer refused: {e}")
            return 1
        # Journal first (D-68): the dispatcher stages the record before anything reads the log.
        self.dispatcher.put_inject(response_ns, snap.session_id, packs.RANK[packs.REFDATA_K],
                                   packs.enc_refdata(0, response_ns, "margin_requirement", payload))
        kite = payload["span_paise"] + payload["exposure_paise"] + payload["additional_paise"]
        row.update(kite_json=json.dumps(answer, sort_keys=True), kite_total_paise=kite, gap_paise=ours - kite,
                   gap_ppm=gap_ppm(ours - kite, kite))
        self._store(row)
        return 1

    def _contract_rows(self, snap: MarginSnapshot) -> list[tuple[dict, int]]:
        found = self.contracts([iid for iid, _ in snap.legs])
        rows = []
        for (iid, units), r in zip(snap.legs, found):
            if not r or not r.get("exchange") or not r.get("tradingsymbol"):
                raise ValueError(f"instrument {iid} is not in refdata")
            rows.append((r, units))
        return rows

    @staticmethod
    def _orders(rows: list[tuple[dict, int]]) -> list[dict]:
        return [{"exchange": r["exchange"], "tradingsymbol": r["tradingsymbol"],
                 "transaction_type": "BUY" if u > 0 else "SELL", "variety": "regular", "product": "NRML",
                 "order_type": "MARKET", "quantity": abs(u), "price": 0} for r, u in rows]

    @staticmethod
    def _market(snap: MarginSnapshot, rows: list[tuple[dict, int]], trade_date: str,
                info: tuple[str, int] | None, now_ns: int) -> dict:
        unds = sorted({r.get("underlying") for r, _ in rows if r.get("underlying")})
        legs = [{"id": iid, "symbol": r["tradingsymbol"], "kind": r.get("kind"), "units": u,
                 "expiry": r.get("actual_expiry") or r.get("expiry"), "strike_paise": r.get("strike_paise")}
                for (iid, _), (r, u) in zip(snap.legs, rows)]
        return {"underlying": "+".join(unds), "type": portfolio_type(rows), "legs": legs,
                "expiry_day": any(leg["expiry"] == trade_date for leg in legs),     # the X-9 add-on can apply
                "span_age_s": None if info is None else max(0, (now_ns - info[1]) // SEC)}

    def _error_row(self, row: dict, text: str) -> None:
        row["error"] = redact(" ".join(str(text).split()))[:ERROR_MAX]
        log.warning("margin poll for %s failed: %s", row["session_id"], row["error"])
        self._store(row)

    def _store(self, row: dict) -> None:
        try:
            store.add_margin_live(row)
        except Exception:  # noqa: BLE001 — the journal already holds the answer; the log row is best effort
            log.exception("storing the margin_live row failed")
