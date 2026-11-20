"""HD 8.1: the nightly desk job. After the close, for each Kite holding: its daily bars, its grades, its closing view.

`run(day)` reads the holdings (`portfolio_sync.fetch_snapshot`, the app's existing Kite path), keeps the NSE
equities and ETFs with at least a year of daily bars, and for each one:

1. tops up the bar cache to six years of daily bars (`history.top_up_daily`, rate-limited; a history already fetched
   that far back is fetched only from its last cached bar, and a split or bonus refetch takes the six years again);
2. grades the voting strategies on it (`evidence.run_for`, which stores its own evidence rows), unless nothing the
   grades rest on has changed since its latest rows: the same first bar, the same last bar up to the evidence
   end, the same evidence end (the holdout pins it, so this saves the backtests nearly every night), the same
   voters, and no refetch tonight;
3. stores the day's closing view (`store.put_view`): each strategy's "now" at the last daily close
   (`replay.now`), the verdict from them and the stored grades (`thesis.decide`), the close, ATR(14) and the
   evidence end date. Everything the page needs, so it serves the view without recomputing. The owner's cost,
   P&L and quantity are not in it.

The others are listed with a reason: "BSE only", "not an NSE stock (e.g. SGB)", "less than a year of daily prices".
No Kite login: one log line and `logged_in` False, so the daemon tries again later. One holding's trouble is
logged and listed under `failed`; the others carry on. `deadline_ns` (checked before the holdings call, between
holdings, and by the bar top-up between its chunks) stops the run before the next session's preparation; what is left
is not `complete`, and runs on a later pass.

Money paise, time UTC epoch ns; Kite's float rupees never enter (the holding's prices are not used).
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import date, timedelta

import portfolio_sync
from stockify import strategy as S
from stockify import timeutil
from stockify.data import history, instruments
from stockify.desk import evidence, replay, store, thesis

log = logging.getLogger("stockify.desk")

HISTORY_YEARS = 6                       # daily bars fetched for each holding
MIN_HISTORY_DAYS = 365                  # calendar days from the first daily bar to `day`
ATR_DAYS = 14

BSE_ONLY = "BSE only"
NOT_NSE_STOCK = "not an NSE stock (e.g. SGB)"
SHORT_HISTORY = "less than a year of daily prices"


def _is_government_security(h) -> bool:  # noqa: ANN001
    """Sovereign gold bonds and other government securities: NSE lists them, but they are not stocks."""
    return instruments.is_government_security(h.tradingsymbol, h.isin)


def resolve(h, day: date) -> tuple[int | None, str | None]:  # noqa: ANN001
    """(instrument_id, None) for a holding the desk can look at (an NSE equity or ETF), else (None, reason). The
    token first (`instruments.resolve_token` on `day`), then the NSE symbol; a BSE holding of a stock NSE also lists
    is the NSE stock. The history check is separate (`has_a_year`), as it needs the bars fetched."""
    if _is_government_security(h):
        return None, NOT_NSE_STOCK
    iid = None
    if h.exchange.upper() == "NSE" and h.instrument_token:
        iid = instruments.resolve_token(int(h.instrument_token), day)
    if iid is None:
        iid = instruments.find("NSE", h.tradingsymbol)
    if iid is None:
        return None, BSE_ONLY if h.exchange.upper() == "BSE" else NOT_NSE_STOCK
    info = instruments.get(iid) or {}
    if info.get("exchange") != "NSE" or info.get("kind") != "EQ":
        return None, NOT_NSE_STOCK
    return iid, None


def has_a_year(bars: list[S.Bar], day: date) -> bool:
    return bool(bars) and timeutil.ns_to_ist(bars[0].start_ns).date() <= day - timedelta(days=MIN_HISTORY_DAYS)


# ── bars ─────────────────────────────────────────────────────────────────────

class _Stopped(Exception):
    """The bar top-up reached the deadline between its chunks: the run stops, and the rest runs later. `refetched`
    keeps whether a split or bonus refetch happened first (the next pass regrades from the adjustment row)."""

    def __init__(self, refetched: bool = False):
        super().__init__("stopped at the deadline")
        self.refetched = refetched


def ensure_bars(instrument_id: int, day: date, *, client=None, deadline_ns: int | None = None) -> bool:  # noqa: ANN001
    """Six years of daily bars up to `day` in the cache (`history.top_up_daily`); only what is missing is asked for.
    True when the history was refetched for a split or bonus (so the grades must be remade). A stop at
    `deadline_ns` inside the top-up raises `_Stopped`, so nothing is graded on a part-fetched history."""
    res = history.top_up_daily(instrument_id, day, years=HISTORY_YEARS, client=client, deadline_ns=deadline_ns,
                               today=day)
    if res.outcome == "stopped":
        raise _Stopped(res.refetched)
    return res.refetched


# ── grades ───────────────────────────────────────────────────────────────────

def _grading_key(bars_to_end: list[S.Bar], end: date) -> tuple:
    first = timeutil.ns_to_ist(bars_to_end[0].start_ns).date().isoformat() if bars_to_end else None
    last = timeutil.ns_to_ist(bars_to_end[-1].start_ns).date().isoformat() if bars_to_end else None
    return first, last, end.isoformat(), tuple(sorted(ref for ref, _ in replay.VOTERS))


def _stored_key(rows: list[dict]) -> tuple | None:
    if not rows:
        return None
    prev_end = evidence.evidence_end(date.fromisoformat(rows[0]["trade_date"]))
    return (min(r["from_date"] for r in rows), max(r["to_date"] for r in rows), prev_end.isoformat(),
            tuple(sorted(r["ref"] for r in rows)))


def grades_for(instrument_id: int, symbol: str, day: date, *, force: bool = False) -> list[dict]:
    """The evidence rows the verdict weighs: tonight's `evidence.run_for`, or the latest stored rows when nothing
    they rest on has changed (see the module doc)."""
    end = evidence.evidence_end(day)
    stored = store.latest_evidence(instrument_id)
    if not force and stored:
        to_end = replay.daily_bars(instrument_id, until_ns=timeutil.ist_ns(end, "00:00"))
        if _stored_key(stored) == _grading_key(to_end, end):
            return stored
    return evidence.run_for(instrument_id, symbol, day)


# ── the closing view ─────────────────────────────────────────────────────────

def closing_view(instrument_id: int, symbol: str, bars: list[S.Bar], rows: list[dict]) -> tuple[str, dict]:
    """(verdict label, the view's JSON) at the last bar's close, from the strategies' "now" and the graded `rows`."""
    sym = symbol if ":" in symbol else f"NSE:{symbol}"
    grades = {r["ref"]: r["grade"] for r in rows}
    signals = [replay.now(ref, {"symbol": sym, **extra}, bars) for ref, extra in replay.VOTERS]
    close = bars[-1].close
    atr = S.atr([(b.high, b.low, b.close) for b in bars], ATR_DAYS)
    atr = atr if math.isfinite(atr) else 0.0
    v = thesis.decide(signals, grades, close, atr)
    view = {
        "verdict": v.label, "exit_share": v.exit_share, "nearest_exit_paise": v.nearest_exit_paise,
        "reasons": list(v.reasons), "agree": list(v.for_), "disagree": list(v.against), "counts": v.counts,
        "signals": [{"ref": s.ref, "display_name": s.display_name, "in_position": s.in_position,
                     "buying_today": s.buying_today, "exit_level_paise": s.exit_level_paise, "note": s.note,
                     "grade": grades.get(s.ref, "none")} for s in signals],
        "close_paise": close, "close_date": timeutil.ns_to_ist(bars[-1].start_ns).date().isoformat(),
        "atr_paise": round(atr), "evidence_to": max((r["to_date"] for r in rows), default=None),
    }
    return v.label, view


# ── the job ──────────────────────────────────────────────────────────────────

def _adjusted_since_grades(instrument_id: int) -> bool:
    """True when a split or bonus refetch was recorded on or after the day of the stored grades: a pass whose refill
    stopped at the deadline did not regrade, and the next pass's top-up finds no new adjustment. Known cost, for that
    stock only: a refetch recorded on day D also remakes the grades on D's later passes and once more on D + 1 (the
    grades made on D carry D's date, which is not after the adjustment's); from then on the grades are newer.)"""
    stored = store.latest_evidence(instrument_id)
    if not stored:
        return False
    graded = max(r["trade_date"] for r in stored)
    return any(a["action"] == "refetched" and a["detected_on"] >= graded for a in history.adjustments(instrument_id))


def _one(h, iid: int, day: date, client, now: Callable[[], int],  # noqa: ANN001
         deadline_ns: int | None = None) -> tuple[str | None, bool]:
    """Bars, grades and the closing view of one resolved holding: (a skip reason, or None when the view is stored;
    whether its history was refetched)."""
    refetched = ensure_bars(iid, day, client=client, deadline_ns=deadline_ns)
    bars = replay.daily_bars(iid, until_ns=timeutil.ist_ns(day, "00:00"))
    if not has_a_year(bars, day):
        return SHORT_HISTORY, refetched
    symbol = (instruments.get(iid) or {}).get("tradingsymbol") or h.tradingsymbol
    rows = grades_for(iid, symbol, day, force=refetched or _adjusted_since_grades(iid))
    label, view = closing_view(iid, symbol, bars, rows)
    store.put_view(day.isoformat(), iid, symbol, label, view, now())
    return None, refetched


def run(day: date, *, client=None, deadline_ns: int | None = None,  # noqa: ANN001
        now: Callable[[], int] | None = None) -> dict:
    """The nightly desk run for IST trade date `day`. Returns {holdings, analysed, skipped [{symbol, reason}],
    failed [{symbol, error}], refetched [symbols whose history a split or bonus refetch replaced], complete,
    logged_in}; `complete` when every holding was analysed or skipped."""
    now = now or timeutil.now_ns
    out: dict = {"holdings": 0, "analysed": 0, "skipped": [], "failed": [], "refetched": [], "complete": False,
                 "logged_in": True}
    if deadline_ns is not None and now() >= deadline_ns:            # past it: not even the holdings call
        log.info("desk for %s: too close to the next session; runs later", day)
        return out
    snap = portfolio_sync.fetch_snapshot()
    if snap.error:
        log.info("desk for %s: no holdings from Kite (%s); tried again later", day, snap.error)
        out["logged_in"] = False
        return out
    if client is None:
        from stockify import kite_readonly
        client = kite_readonly.client()
    out["holdings"] = len(snap.holdings)
    stopped = False
    for h in snap.holdings:
        if deadline_ns is not None and now() >= deadline_ns:
            log.info("desk for %s: stopping before the next session; the rest runs later", day)
            stopped = True
            break
        try:
            iid, reason = resolve(h, day)           # inside the try: a lookup that raises fails this holding only
            if reason is None:
                reason, refetched = _one(h, iid, day, client, now, deadline_ns)
                if refetched:
                    out["refetched"].append(h.tradingsymbol)
        except _Stopped as e:
            if e.refetched:
                out["refetched"].append(h.tradingsymbol)
            log.info("desk for %s: stopping before the next session; the rest runs later", day)
            stopped = True
            break
        except Exception as e:  # noqa: BLE001 — one holding's trouble never stops the others
            log.exception("desk for %s: %s failed", day, h.tradingsymbol)
            out["failed"].append({"symbol": h.tradingsymbol, "error": f"{type(e).__name__}: {e}"})
            continue
        if reason is None:
            out["analysed"] += 1
        else:
            out["skipped"].append({"symbol": h.tradingsymbol, "reason": reason})
    out["complete"] = not stopped and not out["failed"]
    return out
