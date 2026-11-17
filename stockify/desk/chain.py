"""HD 6.1: a holding's option chain, read once (`snapshot`) and summed up (`gauge`).

`snapshot` takes the near and next monthly expiries (stock options are monthly: the first two option expiries on
or after the day), the `strikes_each_side` strikes nearest the price below (strike ≤ price) and above it, calls and
puts, plus each expiry's future, and reads them all through Kite `quote` in calls of at most
`kite_limits.json` quote_max_instruments names, each call paced by a `quote_rps` token bucket. Kite prices are
float rupees; rows keep paise. Kite fills an empty depth level with price 0, which counts as no quote.

Implied volatility (Black-76, `stockify.fno.black76`) is read from the mid of bid and ask; a row without both gets
`iv=None` and `delta=None`. The forward is the expiry's future (its mid, else its last price) when quoted, else the
spot. `t` runs from `now_ns` to the end of the contracts' last trading session (refdata `actual_expiry` when NSE
moved it, the calendar's continuous/closing-auction end, stepping back over a holiday the refdata does not know
yet); `r` is pricing.json's dated risk-free rate on the day.

`gauge` reads the near expiry only, where open interest gathers, and leaves out rows with no open interest or
neither a bid nor an ask (plan Review Focus 5):

* put/call OI ratio = put OI / call OI (None without call OI);
* call walls: the three call strikes above the price with the most OI (resistance), put walls the three put
  strikes below it (support), most OI first;
* max pain: the strike S minimising Σ call OI × max(0, S − K) + Σ put OI × max(0, K − S) (lowest S on a tie);
* ATM IV: the mean of the call's and the put's IV at the strike nearest the price (the lower on a tie);
* IV percentile: the share of the past year's daily ATM IVs (`iv_history`) at or below it, 0–100; None with
  fewer than MIN_HISTORY_DAYS days.

`iv_history` rebuilds a daily ATM IV for the past year from refdata `fo_eod` settle prices: on each trade date the
first option expiry whose last trading day (refdata `actual_expiry` when moved) is after that date (an expiring
contract has no time left at the close), the strike nearest the day's share close (`cm_eod`, else that
expiry's future settle), the forward from the future's settle (else the
close), valued at the end of that day's session.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from stockify import numerics
from stockify.data import calendar, history, instruments, refdata
from stockify.fno import black76 as B
from stockify.strategy import fmt_price

EXCHANGE = "NFO"
SESSION_GROUP = "FO_OPT"
MIN_HISTORY_DAYS = 60
WALLS = 3
HISTORY_DAYS = 365
_HOLIDAY_STEPS = 10           # days to step back from an expiry that falls on a holiday

_bucket: history.TokenBucket | None = None


# ── refdata lookups ──────────────────────────────────────────────────────────

def is_fno(underlying: str, day: date) -> bool:
    return bool(instruments.expiries(underlying, "FUT", day))


def lot_size(underlying: str, day: date) -> int | None:
    """The near future's lot on the day, or None when the stock has no futures."""
    exps = instruments.expiries(underlying, "FUT", day)
    if not exps:
        return None
    fid = instruments.derivative(underlying, "FUT", exps[0], exchange=EXCHANGE)
    spec = instruments.spec_at(fid, day) if fid is not None else None
    return int(spec["lot_size"]) if spec else None


def _rate_rows() -> list[dict]:
    """pricing.json's dated risk-free rows with their exact `rate_ppb`, as `black76.rate_on` reads them."""
    from stockify.config import loader
    rows = []
    for r in loader.load_json("pricing.json").get("risk_free", []):
        r = dict(r)
        if isinstance(r.get("rate"), str):
            r["rate_ppb"] = numerics.to_ppb(r["rate"])
        rows.append(r)
    return rows


def _session_end_ns(d: date) -> int | None:
    ends = [p.end_ns for p in calendar.phases(SESSION_GROUP, d) if p.phase in ("CONTINUOUS", "CAS")]
    return max(ends) if ends else None


def _last_day(underlying: str, expiry: str) -> str:
    row = refdata._connect().execute(
        "SELECT MAX(IFNULL(actual_expiry, expiry)) AS last FROM instruments WHERE underlying=? AND expiry=? "
        "AND kind IN ('CE','PE')", (underlying.upper(), expiry)).fetchone()
    return row["last"] if row and row["last"] else expiry


def _last_trading_ns(expiry: str, underlying: str | None = None) -> int | None:
    """End of the last trading session of an expiry's options, or None if the calendar has no session near it."""
    d = date.fromisoformat(_last_day(underlying, expiry) if underlying else expiry)
    for _ in range(_HOLIDAY_STEPS):
        end = _session_end_ns(d)
        if end is not None:
            return end
        d -= timedelta(days=1)
    return None


# ── quotes ───────────────────────────────────────────────────────────────────

def _default_bucket() -> history.TokenBucket:
    """The process-wide quote bucket (quote_rps, K-8)."""
    global _bucket
    if _bucket is None:
        from stockify.daemon.clock import WallClock
        _bucket = history.TokenBucket(history.limits()["quote_rps"], WallClock())
    return _bucket


def _price(x) -> int | None:  # noqa: ANN001
    """A Kite price to paise; missing or non-positive (Kite's empty depth level) is None."""
    if x is None:
        return None
    p = history._paise(x)
    return p if p > 0 else None


def _fields(q: dict | None) -> tuple[int | None, int | None, int | None, int]:
    """(bid, ask, ltp, oi) of one quote; bid/ask from the best depth level."""
    if not q:
        return None, None, None, 0
    depth = q.get("depth") or {}
    buy, sell = depth.get("buy") or [], depth.get("sell") or []
    bid = _price(buy[0].get("price")) if buy and buy[0].get("quantity", 1) else None
    ask = _price(sell[0].get("price")) if sell and sell[0].get("quantity", 1) else None
    return bid, ask, _price(q.get("last_price")), int(q.get("oi") or 0)


def _quote_all(names: list[str], client, bucket) -> dict:  # noqa: ANN001
    per_call = int(history.limits()["quote_max_instruments"])
    out: dict = {}
    for i in range(0, len(names), per_call):
        bucket.acquire()
        out.update(client.quote(names[i:i + per_call]) or {})
    return out


def _pick_strikes(strikes: list[int], spot_paise: int, n: int) -> set[int]:
    below = [s for s in strikes if s <= spot_paise]
    above = [s for s in strikes if s > spot_paise]
    return set(below[-n:] if n > 0 else []) | set(above[:n])


def snapshot(underlying: str, spot_paise: int, day: date, now_ns: int, *, client,  # noqa: ANN001
             strikes_each_side: int = 10, bucket=None) -> dict:  # noqa: ANN001
    """One poll of the near and next monthly chains. `bucket` paces the quote calls (default: a process-wide
    `quote_rps` bucket on the wall clock); `client` needs only `.quote(list[str]) -> dict`."""
    und = underlying.upper()
    expiries = instruments.expiries(und, "OPT", day)[:2]
    snap = {"underlying": und, "trade_date": day.isoformat(), "taken_ns": int(now_ns), "spot_paise": int(spot_paise),
            "lot_size": lot_size(und, day), "expiries": expiries, "forwards": {}, "t_years": {},
            "rate": B.rate_on(_rate_rows(), day.isoformat()), "rows": []}
    if not expiries:
        return snap
    legs: list[tuple[str, dict]] = []
    futures: dict[str, str] = {}
    for exp in expiries:
        listed = instruments.option_chain(und, exp, day)
        keep = _pick_strikes(sorted({r["strike_paise"] for r in listed}), spot_paise, strikes_each_side)
        legs += [(exp, r) for r in listed if r["strike_paise"] in keep]
        fid = instruments.derivative(und, "FUT", exp, exchange=EXCHANGE)
        info = instruments.get(fid) if fid is not None else None
        if info and info.get("tradingsymbol"):
            futures[exp] = f"{EXCHANGE}:{info['tradingsymbol']}"
    names = sorted({f"{EXCHANGE}:{r['tradingsymbol']}" for _e, r in legs if r.get("tradingsymbol")}
                   | set(futures.values()))
    quotes = _quote_all(names, client, bucket or _default_bucket())

    r = snap["rate"]
    for exp in expiries:
        fwd, source = spot_paise, "spot"
        if exp in futures:
            fbid, fask, fltp, _oi = _fields(quotes.get(futures[exp]))
            fut = (fbid + fask) // 2 if fbid is not None and fask is not None else fltp
            if fut is not None:
                fwd, source = fut, "future"
        snap["forwards"][exp] = {"paise": fwd, "source": source}
        last = _last_trading_ns(exp, und)
        snap["t_years"][exp] = B.year_fraction(now_ns, last) if last is not None else None

    for exp, leg in sorted(legs, key=lambda x: (x[0], x[1]["strike_paise"], x[1]["kind"])):
        sym = leg.get("tradingsymbol")
        bid, ask, ltp, oi = _fields(quotes.get(f"{EXCHANGE}:{sym}") if sym else None)
        iv = delta = None
        t, fwd = snap["t_years"][exp], snap["forwards"][exp]["paise"]
        if bid is not None and ask is not None and t is not None and fwd and fwd > 0:
            is_call, k = leg["kind"] == "CE", leg["strike_paise"] / 100
            sigma, _why = B.implied_vol(is_call, (bid + ask) / 200, fwd / 100, k, t, r)
            if math.isfinite(sigma):
                iv, delta = sigma, B.greeks(is_call, fwd / 100, k, t, r, sigma).delta
        snap["rows"].append({"instrument_id": leg["instrument_id"], "tradingsymbol": sym, "kind": leg["kind"],
                             "strike_paise": leg["strike_paise"], "expiry": exp, "bid_paise": bid, "ask_paise": ask,
                             "ltp_paise": ltp, "oi": oi, "iv": iv, "delta": delta})
    return snap


# ── the gauge ────────────────────────────────────────────────────────────────

def _rupees(paise: int) -> str:
    return "₹" + fmt_price(int(paise))


def _usable(row: dict) -> bool:
    return (row.get("oi") or 0) > 0 and (row.get("bid_paise") is not None or row.get("ask_paise") is not None)


def _max_pain(rows: list[dict]) -> int | None:
    strikes = sorted({r["strike_paise"] for r in rows})
    best = None
    for s in strikes:
        pay = sum(r["oi"] * (max(0, s - r["strike_paise"]) if r["kind"] == "CE" else max(0, r["strike_paise"] - s))
                  for r in rows)
        if best is None or pay < best[0]:
            best = (pay, s)
    return best[1] if best else None


def _atm_iv(rows: list[dict], spot_paise: int) -> float | None:
    priced = [r for r in rows if r.get("iv") is not None and math.isfinite(r["iv"])]
    if not priced:
        return None
    atm = min({r["strike_paise"] for r in priced}, key=lambda s: (abs(s - spot_paise), s))
    ivs = [r["iv"] for r in priced if r["strike_paise"] == atm]
    return sum(ivs) / len(ivs)


def _percentile(value: float | None, past: list[float]) -> float | None:
    past = [v for v in past if v is not None and math.isfinite(v)]
    if value is None or len(past) < MIN_HISTORY_DAYS:
        return None
    return 100.0 * sum(1 for v in past if v <= value) / len(past)


def gauge(snap: dict, spot_paise: int, iv_history: list[float]) -> dict:
    """The near expiry's chain in a few numbers and plain sentences (see the module docstring)."""
    exps = snap.get("expiries") or []
    near = exps[0] if exps else None
    rows = [r for r in snap.get("rows") or [] if r.get("expiry") == near and _usable(r)]
    calls = [r for r in rows if r["kind"] == "CE"]
    puts = [r for r in rows if r["kind"] == "PE"]
    call_oi, put_oi = sum(r["oi"] for r in calls), sum(r["oi"] for r in puts)
    pcr = put_oi / call_oi if call_oi > 0 else None
    by_oi = lambda r: (-r["oi"], r["strike_paise"])  # noqa: E731
    call_walls = [r["strike_paise"] for r in sorted((r for r in calls if r["strike_paise"] > spot_paise), key=by_oi)]
    put_walls = [r["strike_paise"] for r in sorted((r for r in puts if r["strike_paise"] < spot_paise), key=by_oi)]
    call_walls, put_walls = call_walls[:WALLS], put_walls[:WALLS]
    pain = _max_pain(rows)
    atm_iv = _atm_iv(rows, spot_paise)
    pct = _percentile(atm_iv, iv_history)

    said: list[str] = []
    if not rows:
        said.append("No option prices with open interest yet, so there is nothing to read from the chain."
                    if exps else "No options are listed on NSE for this stock.")
    else:
        if pcr is not None:
            lean = "more puts than calls are open" if pcr > 1 else "more calls than puts are open" if pcr < 1 \
                else "as many puts as calls are open"
            said.append(f"Put/call open interest ratio is {pcr:.2f}: {lean}.")
        if call_walls:
            said.append(f"The most open calls above the price sit at {_rupees(call_walls[0])}, read as resistance.")
        if put_walls:
            said.append(f"The most open puts below the price sit at {_rupees(put_walls[0])}, read as support.")
        if pain is not None:
            said.append(f"Max pain is {_rupees(pain)}: the expiry price at which option buyers would be paid least.")
        if atm_iv is None:
            said.append("Implied volatility could not be read: the strikes near the price have no bid and ask.")
        elif pct is None:
            said.append(f"Implied volatility near the price is {atm_iv * 100:.1f}%; there are not yet "
                        f"{MIN_HISTORY_DAYS} days of the past year to compare it with.")
        else:
            said.append(f"Implied volatility near the price is {atm_iv * 100:.1f}%, at or above {pct:.0f}% of the "
                        f"past year's days.")
    return {"expiry": near, "pcr_oi": pcr, "call_walls": call_walls, "put_walls": put_walls,
            "max_pain_paise": pain, "atm_iv": atm_iv, "iv_percentile": pct, "sentences": said}


# ── IV history from the archived F&O bhavcopies ──────────────────────────────

def iv_history(underlying: str, day: date) -> list[float]:
    """ATM IV per trade date over the HISTORY_DAYS days up to `day`, oldest first; [] without fo_eod rows."""
    und = underlying.upper()
    lo, hi = (day - timedelta(days=HISTORY_DAYS)).isoformat(), day.isoformat()
    conn = refdata._connect()
    rows = conn.execute(
        "SELECT f.trade_date, i.kind, i.expiry, IFNULL(i.actual_expiry, i.expiry) AS last, i.strike_paise, "
        "IFNULL(NULLIF(f.settle_paise, 0), f.close_paise) AS px FROM fo_eod f JOIN instruments i USING (instrument_id) "
        "WHERE i.underlying=? AND i.exchange=? "
        "AND i.kind IN ('CE','PE','FUT') AND f.trade_date BETWEEN ? AND ? ORDER BY f.trade_date",
        (und, EXCHANGE, lo, hi)).fetchall()
    if not rows:
        return []
    eq = instruments.find("NSE", und)
    closes = {}
    if eq is not None:
        closes = {r["trade_date"]: r["close_paise"] for r in conn.execute(
            "SELECT trade_date, close_paise FROM cm_eod WHERE instrument_id=? AND series='EQ' "
            "AND trade_date BETWEEN ? AND ?", (eq, lo, hi))}
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["trade_date"], []).append(r)
    rate_rows, ends = _rate_rows(), {}
    out = []
    for d, items in by_day.items():
        opts = [r for r in items if r["kind"] in ("CE", "PE") and r["last"] > d and r["px"]]
        if not opts:
            continue
        exp = min(r["expiry"] for r in opts)
        fut = next((r["px"] for r in items if r["kind"] == "FUT" and r["expiry"] == exp and r["px"]), None)
        spot = closes.get(d) or fut
        fwd = fut or spot
        now = _session_end_ns(date.fromisoformat(d))
        if exp not in ends:
            ends[exp] = _last_trading_ns(exp, und)
        if not spot or not fwd or now is None or ends[exp] is None:
            continue
        near = [r for r in opts if r["expiry"] == exp]
        atm = min({r["strike_paise"] for r in near}, key=lambda s: (abs(s - spot), s))
        t, rate = B.year_fraction(now, ends[exp]), B.rate_on(rate_rows, d)
        ivs = []
        for r in near:
            if r["strike_paise"] != atm:
                continue
            sigma, _why = B.implied_vol(r["kind"] == "CE", r["px"] / 100, fwd / 100, atm / 100, t, rate)
            if math.isfinite(sigma):
                ivs.append(sigma)
        if ivs:
            out.append(sum(ivs) / len(ivs))
    return out


__all__ = ["gauge", "is_fno", "iv_history", "lot_size", "snapshot"]
