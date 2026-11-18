"""HD 9.2: the holdings desk during the session. A poller thread, the day's prices, and the live recompute.

`DeskPoller` is a thread the API starts (server/main.py lifespan). Every INTERVAL_S seconds it calls `take_pass`,
which does nothing unless `scheduler.session_in_progress(now)` and Kite is logged in. A pass:

1. reads the holdings (`portfolio_sync.cached_snapshot`) and keeps the ones the desk looks at (`nightly.resolve`);
2. subscribes their tokens on the API's own ticker (`ticker_bridge.ensure_subscribed`), so the tick buffer has
   their prices;
3. reads in ONE Kite `quote` batch (split only at quote_max_instruments names, each call paced by the quote_rps
   bucket shared with `chain.snapshot`) every holding's share price, open, high and low, plus each F&O holding's
   chain: `chain.snapshot` is run twice, first with a client that only records the names it asks for, then on the
   batch's answers. The recorder process quotes the price bands too and Kite's limit is per account, so a refused or
   failed batch keeps the last snapshots and the pass reports the error; nothing raises;
4. stores each F&O holding's snapshot (`store.put_chain`) and warms the live view of every holding.

The live view (`live_view`) is `nightly.closing_view` on the daily bars plus today's candle
(`replay.with_live_candle`), replaying each rule over its look-back only (`tail=True`), cached LIVE_CACHE_S
seconds per holding. Its price is the newest of the tick buffer's last tick and the pass's quote; the day's open,
high and low come from the pass's quote, or, before a pass has run (the app started mid-session), from one Kite
`ohlc` call for that stock. No live price, or outside the session: None, and the endpoints serve the stored
closing view with `live` False.

Kite and the tick buffer give float rupees and epoch seconds: they become paise and epoch ns here. Only
`kite_readonly.client()` calls: no order can be placed.
"""
from __future__ import annotations

import logging
import threading
from datetime import date

import kite_client
import portfolio_sync
import tick_buffer
from stockify import timeutil
from stockify.daemon import scheduler
from stockify.data import history
from stockify.desk import chain, nightly, replay, store

log = logging.getLogger("stockify.desk")

INTERVAL_S = 60
LIVE_CACHE_S = 30
NS = timeutil.NS_PER_S
CHAIN_FRESH_NS = 3 * INTERVAL_S * NS          # a snapshot this recent, in the session, is live
EXCHANGE = "NSE"

_lock = threading.Lock()
_day: dict[int, dict] = {}                    # instrument_id -> {day, open, high, low, last, t_ns} (paise, ns)
_views: dict[int, tuple[int, dict | None]] = {}     # instrument_id -> (made_ns, live_view result)
_iv: dict[tuple[str, str], list[float]] = {}  # (underlying, IST date) -> chain.iv_history
_status: dict = {"last_pass_ns": None, "last_error": None}


def now_ns() -> int:
    return timeutil.now_ns()


def _bucket():  # noqa: ANN202
    """The quote_rps bucket every desk quote call takes a token from (the one `chain.snapshot` uses)."""
    return chain._default_bucket()


def reset() -> None:
    """Forget the day's prices, the cached views and IV histories (tests; a new day)."""
    with _lock:
        _day.clear()
        _views.clear()
        _iv.clear()
        _status.update(last_pass_ns=None, last_error=None)


def status() -> dict:
    with _lock:
        return dict(_status)


def in_session(t_ns: int) -> bool:
    return scheduler.session_in_progress(t_ns)


def ist_day(t_ns: int) -> date:
    return timeutil.ns_to_ist(t_ns).date()


def iv_history(underlying: str, day: date) -> list[float]:
    """`chain.iv_history`, read once per underlying and IST date (it walks a year of bhavcopy rows)."""
    key = (underlying.upper(), day.isoformat())
    with _lock:
        hit = _iv.get(key)
    if hit is None:
        hit = chain.iv_history(underlying, day)
        with _lock:
            if len(_iv) > 256:
                _iv.clear()
            _iv[key] = hit
    return hit


def resolved(snap, day: date) -> list[tuple]:  # noqa: ANN001
    """[(holding, instrument_id)] of the holdings the desk looks at, in the snapshot's order."""
    out = []
    for h in snap.holdings:
        iid, reason = nightly.resolve(h, day)
        if reason is None:
            out.append((h, iid))
    return out


def nse_token(h, iid: int, day: date) -> int | None:  # noqa: ANN001
    """The NSE stock's Kite token on `day`: the desk looks at the NSE listing, so a holding bought through BSE
    still ticks, quotes and charts on NSE. The holding's own token only when the refdata has none and it is NSE."""
    from stockify.data import instruments
    tok = instruments.token_at(iid, day)
    if tok:
        return int(tok)
    if h is not None and (h.exchange or "").upper() == EXCHANGE and h.instrument_token:
        return int(h.instrument_token)
    return None


def _symbol(h, iid: int) -> str:  # noqa: ANN001
    from stockify.data import instruments
    return (instruments.get(iid) or {}).get("tradingsymbol") or h.tradingsymbol


# ── the day's prices ─────────────────────────────────────────────────────────

def _paise(x) -> int | None:  # noqa: ANN001
    if x is None or isinstance(x, bool):
        return None
    try:
        p = history._paise(x)
    except Exception:  # noqa: BLE001 — a field Kite sent as something else
        return None
    return p if p > 0 else None


def _remember(iid: int, q: dict | None, t_ns: int) -> bool:
    """Keep one quote's (or ohlc answer's) day range and last price; False when it has no last price."""
    if not q:
        return False
    last = _paise(q.get("last_price"))
    if last is None:
        return False
    o = q.get("ohlc") or {}
    rec = {"day": ist_day(t_ns).isoformat(), "last": last, "t_ns": int(t_ns),
           "open": _paise(o.get("open")) or last, "high": _paise(o.get("high")) or last,
           "low": _paise(o.get("low")) or last}
    with _lock:
        _day[iid] = rec
    return True


def _ohlc_once(iid: int, symbol: str, t_ns: int) -> dict | None:
    """The fallback before any pass: one Kite `ohlc` call for this stock. None when Kite cannot answer."""
    if not kite_client.is_authenticated():
        return None
    try:
        from stockify import kite_readonly
        name = f"{EXCHANGE}:{symbol}"
        _bucket().acquire()
        ans = kite_readonly.client().ohlc([name]) or {}
    except Exception as e:  # noqa: BLE001 — logged out, refused, the network
        log.info("desk: no day prices for %s from Kite (%s)", symbol, type(e).__name__)
        return None
    if not _remember(iid, ans.get(name), t_ns):
        return None
    with _lock:
        return _day.get(iid)


def live_price(iid: int, symbol: str, token: int | None, t_ns: int, *, allow_call: bool = True) -> dict | None:
    """{ltp, t_ns, open, high, low} in paise for today, or None. See the module docstring."""
    today = ist_day(t_ns).isoformat()
    with _lock:
        rec = _day.get(iid)
    if rec is not None and rec["day"] != today:
        rec = None
    tick = None
    if token:
        try:
            tick = tick_buffer.latest_ticks_bulk([int(token)]).get(int(token))
        except Exception:  # noqa: BLE001 — the tick buffer is a cache: no tick, not an error
            tick = None
    tick_ns = int(tick["ts"]) * NS if tick and tick.get("ts") else None
    tick_paise = _paise(tick.get("ltp")) if tick else None
    if tick_ns is None or tick_paise is None or ist_day(tick_ns).isoformat() != today or tick_ns > t_ns:
        tick_ns = tick_paise = None
    if rec is None and allow_call:
        rec = _ohlc_once(iid, symbol, t_ns)
    if rec is None and tick_paise is None:
        return None
    if tick_paise is not None and (rec is None or tick_ns >= rec["t_ns"]):
        ltp, at = tick_paise, tick_ns
    else:
        ltp, at = rec["last"], rec["t_ns"]
    o, hi, lo = (rec["open"], rec["high"], rec["low"]) if rec else (ltp, ltp, ltp)
    return {"ltp": ltp, "t_ns": at, "open": o, "high": hi, "low": lo}


def live_view(iid: int, symbol: str, token: int | None, t_ns: int, *, allow_call: bool = True,
              max_age_s: int = LIVE_CACHE_S) -> dict | None:
    """{verdict, view, as_of_ns} recomputed from the live price, cached `max_age_s` s (the list of chips takes the
    pass's views up to two passes old, so it never replays every holding at once); None outside the session,
    without a stored closing view (never graded), or without a live price."""
    if not in_session(t_ns):
        return None
    with _lock:
        hit = _views.get(iid)
    if hit is not None and 0 <= t_ns - hit[0] < max_age_s * NS:
        return hit[1]
    out = None
    try:
        out = _recompute(iid, symbol, token, t_ns, allow_call)
    except Exception:  # noqa: BLE001 — the page falls back to the closing view
        log.exception("desk: live view of %s failed", symbol)
    with _lock:
        _views[iid] = (t_ns, out)
    return out


def _recompute(iid: int, symbol: str, token: int | None, t_ns: int, allow_call: bool) -> dict | None:
    if store.latest_view(iid) is None:
        return None
    price = live_price(iid, symbol, token, t_ns, allow_call=allow_call)
    if price is None:
        return None
    bars = replay.daily_bars(iid)
    if not bars or price["t_ns"] < bars[-1].start_ns:
        return None
    bars = replay.with_live_candle(bars, price["ltp"], price["t_ns"], price["open"], price["high"], price["low"])
    label, view = nightly.closing_view(iid, symbol, bars, store.latest_evidence(iid), tail=True)
    return {"verdict": label, "view": view, "as_of_ns": price["t_ns"]}


# ── one pass ─────────────────────────────────────────────────────────────────

class _Recorder:
    """A `quote` client that answers nothing and keeps the names asked for."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def quote(self, names: list[str]) -> dict:
        self.names.update(names)
        return {}


class _Canned:
    def __init__(self, quotes: dict) -> None:
        self.quotes = quotes

    def quote(self, names: list[str]) -> dict:
        return {n: self.quotes[n] for n in names if n in self.quotes}


class _Free:
    def acquire(self) -> None:
        pass


def _spot_guess(iid: int, symbol: str, token: int | None, t_ns: int) -> int | None:
    """The price the pass picks strikes around: the newest known today, else the last close."""
    p = live_price(iid, symbol, token, t_ns, allow_call=False)
    if p is not None:
        return p["ltp"]
    v = store.latest_view(iid)
    if v is not None and v["view"].get("close_paise"):
        return int(v["view"]["close_paise"])
    bars = replay.daily_bars(iid)
    return bars[-1].close if bars else None


def take_pass(t_ns: int | None = None, *, client=None, bucket=None) -> dict:  # noqa: ANN001
    """One poll (see the module docstring). {active, reason, quoted, snapshots, error}; never raises."""
    t_ns = now_ns() if t_ns is None else int(t_ns)
    out: dict = {"active": False, "reason": None, "quoted": 0, "snapshots": 0, "error": None}
    try:
        if not in_session(t_ns):
            out["reason"] = "outside the session"
            return out
        if not kite_client.is_authenticated():
            out["reason"] = "Kite is not logged in"
            return out
        return _pass(t_ns, client, bucket, out)
    except Exception as e:  # noqa: BLE001 — a pass never takes the API down
        log.exception("desk: chain pass failed")
        out["error"] = f"{type(e).__name__}"
        return out


def _pass(t_ns: int, client, bucket, out: dict) -> dict:  # noqa: ANN001
    snap = portfolio_sync.cached_snapshot(60)
    if snap.error:
        out["reason"] = "no holdings from Kite"
        return out
    out["active"] = True
    day = ist_day(t_ns)
    held = [(h, iid, _symbol(h, iid), nse_token(h, iid, day)) for h, iid in resolved(snap, day)]
    if not held:
        return out
    from server.services import ticker_bridge
    try:
        ticker_bridge.ensure_subscribed(sorted({tok for _h, _i, _s, tok in held if tok}))
    except Exception:  # noqa: BLE001 — the ticker is a nicety; quotes still come
        log.warning("desk: could not subscribe the holdings' ticks", exc_info=True)
    if client is None:
        from stockify import kite_readonly
        client = kite_readonly.client()

    plans = []                                          # (iid, underlying, spot) of each F&O holding
    rec = _Recorder()
    for _h, iid, sym, tok in held:
        if not chain.is_fno(sym, day):
            continue
        spot = _spot_guess(iid, sym, tok, t_ns)
        if spot is None:
            continue
        chain.snapshot(sym, spot, day, t_ns, client=rec, bucket=_Free())
        plans.append((iid, sym, spot))
    names = sorted({f"{EXCHANGE}:{sym}" for _h, _i, sym, _t in held} | rec.names)
    try:
        quotes = chain._quote_all(names, client, bucket or _bucket())
    except Exception as e:  # noqa: BLE001 — refused (the recorder shares the limit), logged out, the network
        log.warning("desk: the quote batch failed (%s); the last chain snapshots stay", type(e).__name__)
        out["error"] = f"quote batch failed ({type(e).__name__})"
        with _lock:
            _status.update(last_error=out["error"])
        return out
    out["quoted"] = len(quotes)
    for _h, iid, sym, _t in held:
        _remember(iid, quotes.get(f"{EXCHANGE}:{sym}"), t_ns)
    canned = _Canned(quotes)
    for iid, sym, spot in plans:
        s = chain.snapshot(sym, spot, day, t_ns, client=canned, bucket=_Free())
        if not any(r.get("bid_paise") or r.get("ask_paise") or r.get("ltp_paise") for r in s["rows"]):
            log.info("desk: no option quotes for %s this pass; its last snapshot stays", sym)
            continue
        store.put_chain(iid, t_ns, s)
        out["snapshots"] += 1
    with _lock:
        _views.clear()                                  # the new prices make the cached views old
        _status.update(last_pass_ns=t_ns, last_error=None)
    for _h, iid, sym, tok in held:
        live_view(iid, sym, tok, t_ns, allow_call=False)
    return out


# ── the thread ───────────────────────────────────────────────────────────────

class DeskPoller:
    """take_pass every `interval_s` seconds until stop(); a pass outside the session does nothing."""

    def __init__(self, interval_s: int = INTERVAL_S) -> None:
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        while not self._stop.is_set():
            take_pass()
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="desk-poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


_poller: DeskPoller | None = None


def start() -> DeskPoller:
    global _poller
    if _poller is None:
        _poller = DeskPoller()
    _poller.start()
    return _poller


def stop() -> None:
    if _poller is not None:
        _poller.stop()
