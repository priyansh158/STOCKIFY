"""SPAN-lite calibration against Kite (M4.9): `python -m stockify margin calibrate --underlying NIFTY --samples 200`.

During market hours, with a fresh Kite login (the owner's run; tests use a fake client and make no Kite call):

1. snapshot: the underlying's index, nearest future and the option chains of its two nearest expiries (strikes
   around the money) from refdata, priced with Kite quotes (`client.quote`, ≤ 1 request/s, K-8);
2. sample portfolios from it (seeded): single legs across moneyness and expiries, straddles, strangles, verticals,
   iron flies, iron condors, futures, futures hedged with options;
3. ask Kite's basket margin for each (`client.basket_order_margins(orders, consider_positions=False)`,
   `POST /margins/basket`, K-10; ≤ 10 requests/s, K-8) and store Kite's answer with the snapshot, our T0 (flat)
   and our T1 (SPAN-lite with the current parameters) in lab.sqlite → margin_calibration;
4. fit PSR, VSR and somc_per_lot (coordinate search), then futures' own price move (D-62), to minimise the mean
   absolute relative error of T1 against Kite's final span + exposure + additional (`kite_target`: not `total`,
   which counts bought premium); report the median and 90th-percentile error per portfolio type. `--refit` does
   4–5 again offline from the samples stored for a day (`refit`);
5. write the fit as a new dated row under `margin_params.json` → `spanlite_pending` (no model reads that key):
   🛑 the owner reviews the report and moves the row into `spanlite` to adopt it. Never automatic.

Only the read-only quote and margin endpoints are called; no order is ever placed. Nothing about the login (keys,
tokens) is logged or stored.
"""
from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from stockify import numerics

TYPES = ("single", "straddle", "strangle", "vertical", "iron_fly", "iron_condor", "future", "hedged_future")
QUOTE_INTERVAL_S = 1.0          # K-8: quote endpoint 1 request/s
MARGIN_INTERVAL_S = 0.1         # K-8: other endpoints 10 requests/s
BOUNDS = {"psr": (0.01, 0.25), "vsr": (0.0, 0.8), "somc": (0, 2_000_000),     # somc in paise per lot
          "psr_fut": (0.01, 0.25)}
FUTURE_KINDS = ("future",)   # the samples psr_futures is fitted from (D-62). Not hedged futures: Kite's SPAN on
                              # those credits the bought option (one BANKNIFTY pair on 2026-11-09 has SPAN 0), which
                              # no price move reproduces; fitting to them pulls the futures move off plain futures.


class CalibrationError(RuntimeError):
    pass


@dataclass
class Snapshot:
    """The market a calibration run priced: bundle-shaped instrument rows and quotes in paise."""
    underlying: str
    day: str
    wall_ns: int
    index_id: int
    rows: dict[int, dict]                                  # iid → bundle-shaped row (with "lot")
    quotes: dict[int, dict]                                # iid → {ltp, bid, ask, oi} (paise; None if missing)
    futures: list[int] = field(default_factory=list)
    expiries: list[str] = field(default_factory=list)
    skip_expiring: bool = False                            # IWB 4.2: contracts expiring on `day` left out
    skipped: list[str] = field(default_factory=list)       # the expiries left out for that reason

    def choice(self) -> dict:
        """What the snapshot priced, for the report and each margin_calibration row."""
        return {"skip_expiring": self.skip_expiring, "expiries": list(self.expiries), "skipped": list(self.skipped)}

    def options(self, expiry: str) -> list[dict]:
        return sorted((r for r in self.rows.values() if r["kind"] in ("CE", "PE") and r["expiry"] == expiry),
                      key=lambda r: (r["strike_paise"], r["kind"]))

    def mark(self, iid: int) -> int | None:
        q = self.quotes.get(iid) or {}
        if q.get("bid") and q.get("ask"):
            return (q["bid"] + q["ask"]) // 2
        return q.get("ltp")


@dataclass
class Portfolio:
    kind: str
    legs: list[tuple[int, int]]                            # (iid, signed units)


# ── 1. The snapshot ──────────────────────────────────────────────────────────

def _paise(rupees) -> int | None:  # noqa: ANN001
    return None if rupees in (None, 0) else int((Decimal(str(rupees)) * 100).to_integral_value())


def _quote_fields(q: dict) -> dict:
    depth = q.get("depth") or {}
    buy, sell = depth.get("buy") or [], depth.get("sell") or []
    return {"ltp": _paise(q.get("last_price")), "bid": _paise(buy[0]["price"]) if buy else None,
            "ask": _paise(sell[0]["price"]) if sell else None, "oi": q.get("oi")}


def _last_trading_ns(expiry: str, group: str) -> int | None:
    from stockify.data import calendar
    ends = [p.end_ns for p in calendar.phases(group, date.fromisoformat(expiry)) if p.phase in ("CONTINUOUS", "CAS")]
    return max(ends) if ends else None


def _last_day(underlying: str, expiry: str, kinds: tuple[str, ...]) -> str:
    """The last trading day of one expiry's contracts: refdata's actual_expiry when NSE moved it, else the expiry."""
    from stockify.data import refdata
    marks = ",".join("?" * len(kinds))
    row = refdata._connect().execute(
        f"SELECT MAX(IFNULL(actual_expiry, expiry)) AS last FROM instruments WHERE underlying=? AND expiry=? "
        f"AND kind IN ({marks})", (underlying.upper(), expiry, *kinds)).fetchone()
    return (row["last"] if row and row["last"] else expiry)


def _pick(underlying: str, found: list[str], kinds: tuple[str, ...], day: str, n: int,
          skip_expiring: bool) -> tuple[list[str], list[str]]:
    """The first `n` expiries; with skip_expiring, any whose last trading day is `day` is left out (IWB 4.2)."""
    if not skip_expiring:
        return found[:n], []
    skipped = [e for e in found if _last_day(underlying, e, kinds) == day]
    return [e for e in found if e not in skipped][:n], skipped


def _underlying_row(underlying: str) -> tuple[int, bool]:
    """(instrument id of the index or stock, whether it is an index)."""
    from stockify.data import instruments, refdata
    conn = refdata._connect()
    index_rows = {r["key"]: r["index_symbol"] for r in conn.execute("SELECT key, index_symbol FROM index_underlyings")}
    is_index = underlying in index_rows
    if is_index:
        exch, _, sym = index_rows[underlying].partition(":")
        index_id = instruments.find(exch, sym)
    else:
        index_id = instruments.find("NSE", underlying)
    if index_id is None:
        raise CalibrationError(f"{underlying}: no index or stock row in refdata")
    return index_id, is_index


def _row_for(iid: int, kind: str, underlying: str, index_id: int, is_index: bool, day: str) -> dict:
    """A bundle-shaped instrument row (with "lot") for the snapshot, from refdata."""
    from stockify.data import instruments
    info = instruments.get(iid)
    spec = instruments.spec_at(iid, day) or {}
    expiry = info.get("expiry")
    group = "FO_OPT" if kind in ("CE", "PE") else "FO_FUT" if kind == "FUT" else "CM"
    last = info.get("actual_expiry") or expiry
    return {"id": iid, "kind": kind, "exchange": info["exchange"], "tradingsymbol": info["tradingsymbol"],
            "underlying": underlying if kind != "INDEX" else None,
            "underlying_id": index_id if iid != index_id else None,
            "underlying_class": "INDEX" if is_index else "STOCK",
            "expiry": expiry, "actual_expiry": info.get("actual_expiry"),
            "strike_paise": info.get("strike_paise"), "lot": spec.get("lot_size") or 1,
            "last_trading_ns": _last_trading_ns(last, group) if last else None}


def build_snapshot(client, underlying: str, day: str, *, strikes: int = 8, sleep=time.sleep,  # noqa: ANN001
                   now_ns=None, skip_expiring: bool = False) -> Snapshot:  # noqa: ANN001
    """With `skip_expiring`, contracts whose last trading day is `day` are left out and the next ones taken
    (IWB 4.2: on an expiry day the expiring options and the expiry-day add-on would skew the fit)."""
    from stockify import timeutil
    from stockify.data import instruments
    index_id, is_index = _underlying_row(underlying)
    rows: dict[int, dict] = {}

    def add(iid: int, kind: str) -> None:
        rows[iid] = _row_for(iid, kind, underlying, index_id, is_index, day)

    add(index_id, "INDEX" if is_index else "EQ")
    fut_exp, fut_skipped = _pick(underlying, instruments.expiries(underlying, "FUT", day), ("FUT",), day, 1,
                                 skip_expiring)
    futures = [iid for e in fut_exp if (iid := instruments.derivative(underlying, "FUT", e)) is not None]
    for iid in futures:
        add(iid, "FUT")
    first = client.quote([f"{rows[i]['exchange']}:{rows[i]['tradingsymbol']}" for i in rows])
    quotes = {i: _quote_fields(first.get(f"{rows[i]['exchange']}:{rows[i]['tradingsymbol']}", {})) for i in rows}
    spot = quotes[index_id]["ltp"]
    if spot is None:
        raise CalibrationError(f"{underlying}: no price for the underlying in the quote")
    expiries, opt_skipped = _pick(underlying, instruments.expiries(underlying, "OPT", day), ("CE", "PE"), day, 2,
                                  skip_expiring)
    for e in expiries:
        chain = instruments.option_chain(underlying, e, day)
        ks = sorted({r["strike_paise"] for r in chain})
        if not ks:
            continue
        atm = min(range(len(ks)), key=lambda n: (abs(ks[n] - spot), ks[n]))
        keep = set(ks[max(0, atm - strikes): atm + strikes + 1])
        for r in chain:
            if r["strike_paise"] in keep:
                add(r["instrument_id"], r["kind"])
    opts = [i for i in rows if rows[i]["kind"] in ("CE", "PE")]
    for n in range(0, len(opts), 500):                     # ≤ 500 instruments per quote call (K-8 ⚠)
        sleep(QUOTE_INTERVAL_S)
        part = opts[n:n + 500]
        answer = client.quote([f"{rows[i]['exchange']}:{rows[i]['tradingsymbol']}" for i in part])
        for i in part:
            quotes[i] = _quote_fields(answer.get(f"{rows[i]['exchange']}:{rows[i]['tradingsymbol']}", {}))
    wall = now_ns() if now_ns else timeutil.now_ns()
    return Snapshot(underlying, day, wall, index_id, rows, quotes, futures, expiries, skip_expiring,
                    sorted(set(fut_skipped) | set(opt_skipped)))


# ── 2. Portfolios ────────────────────────────────────────────────────────────

def sample_portfolios(snap: Snapshot, n: int, seed: int = 0) -> list[Portfolio]:
    """n portfolios cycling through TYPES (seeded), built from quoted instruments only."""
    rng = random.Random(seed)
    out: list[Portfolio] = []
    usable = [e for e in snap.expiries if snap.options(e)]
    for k in range(n):
        kind = TYPES[k % len(TYPES)]
        p = _one(snap, kind, rng, usable)
        if p is not None:
            out.append(p)
    return out


def _one(snap: Snapshot, kind: str, rng: random.Random, usable: list[str]) -> Portfolio | None:
    spot = snap.quotes[snap.index_id]["ltp"]
    lots = rng.choice([1, 1, 2])
    if kind in ("future", "hedged_future"):
        if not snap.futures:
            return None
        fut = snap.futures[0]
        side = rng.choice([1, -1])
        legs = [(fut, side * lots * snap.rows[fut]["lot"])]
        if kind == "hedged_future" and usable:
            chain = snap.options(usable[0])
            hedge_kind = "PE" if side > 0 else "CE"
            cands = [r for r in chain if r["kind"] == hedge_kind]
            if not cands:
                return None
            r = min(cands, key=lambda r: (abs(r["strike_paise"] - spot), r["strike_paise"]))
            legs.append((r["id"], lots * r["lot"]))
        return Portfolio(kind, legs)
    if not usable:
        return None
    chain = snap.options(rng.choice(usable))
    strikes = sorted({r["strike_paise"] for r in chain})
    by = {(r["strike_paise"], r["kind"]): r for r in chain}
    a = min(range(len(strikes)), key=lambda i: (abs(strikes[i] - spot), strikes[i]))

    def leg(i: int, k: str, sign: int) -> tuple[int, int] | None:
        if not 0 <= i < len(strikes) or (strikes[i], k) not in by:
            return None
        r = by[(strikes[i], k)]
        return (r["id"], sign * lots * r["lot"])

    w = rng.randint(1, 3)
    if kind == "single":
        legs = [leg(a + rng.randint(-4, 4), rng.choice(["CE", "PE"]), rng.choice([1, -1]))]
    elif kind == "straddle":
        legs = [leg(a, "CE", -1), leg(a, "PE", -1)]
    elif kind == "strangle":
        legs = [leg(a + w, "CE", -1), leg(a - w, "PE", -1)]
    elif kind == "vertical":
        k = rng.choice(["CE", "PE"])
        legs = [leg(a, k, -1), leg(a + w if k == "CE" else a - w, k, 1)]
    elif kind == "iron_fly":
        legs = [leg(a, "CE", -1), leg(a, "PE", -1), leg(a + w, "CE", 1), leg(a - w, "PE", 1)]
    else:                                                    # iron_condor
        legs = [leg(a + 1, "CE", -1), leg(a - 1, "PE", -1), leg(a + 1 + w, "CE", 1), leg(a - 1 - w, "PE", 1)]
    if any(x is None for x in legs):
        return None
    return Portfolio(kind, legs)


# ── 3. Kite and our models on the same snapshot ──────────────────────────────

def kite_orders(snap: Snapshot, p: Portfolio) -> list[dict]:
    return [{"exchange": snap.rows[i]["exchange"], "tradingsymbol": snap.rows[i]["tradingsymbol"],
             "transaction_type": "BUY" if u > 0 else "SELL", "variety": "regular", "product": "NRML",
             "order_type": "MARKET", "quantity": abs(u), "price": 0} for i, u in p.legs]


class _SnapshotEngine:
    """Just enough of the oracle runner for its Context (the chain view) and the margin models, on a snapshot."""

    def __init__(self, snap: Snapshot, margin_params: dict, pricing: dict):
        from stockify.oracle.context import Context
        from stockify.oracle.runner import Market
        from stockify import bundle as B
        from stockify.config import loader
        from stockify.data import calendar
        start = date.fromisoformat(snap.day)
        expiries = [r["actual_expiry"] or r["expiry"] for r in snap.rows.values() if r.get("expiry")]
        end = max([date.fromisoformat(x) for x in expiries] + [start])
        policy = B._dated_section(loader.load_json("broker_policy.json"), start, end)
        B._ppb_fields(policy, ("pct", "fraction"))          # as a bundle holds them (delivery margin, MIS)
        days = [{"date": d.isoformat()} for d in calendar.trading_days("FO", start, end)]
        self.bundle = {"instruments": list(snap.rows.values()), "margin_params": margin_params,
                       "pricing": {"stale_after_ms": 10**12, **pricing}, "broker_policy": policy, "days": days,
                       "strategy": {"params": {}}}
        self.now, self.key, self.labels, self.strategy_rng = snap.wall_ns, (snap.wall_ns, 0, 0, 0), set(), None
        self.market = {}
        for iid, q in snap.quotes.items():
            m = Market()
            m.ltp, m.bid, m.ask, m.oi, m.t = q.get("ltp"), q.get("bid"), q.get("ask"), q.get("oi"), snap.wall_ns
            self.market[iid] = m
        self._snap = snap
        self.ctx = Context(self)

    def _date(self, t: int) -> str:
        return self._snap.day

    def spec(self, iid: int, d: str) -> dict:
        return {"lot": self._snap.rows[iid]["lot"]}


def _config_params() -> tuple[dict, dict]:
    """margin_params and pricing as a bundle holds them (ppb fields added)."""
    from stockify import bundle as B
    from stockify.config import loader
    margin = {k: v for k, v in loader.load_json("margin_params.json").items() if k != "schema"}
    B._ppb_fields(margin, ("index_pct", "stock_pct", "pct", "psr_pct", "psr_futures_pct", "vsr_pct",
                           "extreme_weight", "elm_pct", "default_iv"))
    pricing = {k: v for k, v in loader.load_json("pricing.json").items() if k == "risk_free"}
    B._ppb_fields(pricing, ("rate",))
    return margin, pricing


class Pricer:
    """T0 and T1 for portfolios on one snapshot: one engine, one chain view, models built once. T1 takes the
    SPAN-lite row to use, so the fit can try many rows cheaply."""

    def __init__(self, snap: Snapshot, margin: dict, pricing: dict):
        from stockify.fno.spanlite import SpanLiteMargin
        from stockify.oracle.margin import FlatMargin
        self.snap = snap
        self.engine = _SnapshotEngine(snap, margin, pricing)
        self.flat = FlatMargin(self.engine.bundle)
        self.span = SpanLiteMargin(self.engine.bundle, self.engine)

    def _legs(self, p: Portfolio) -> list:
        return [(i, "NRML", u) for i, u in p.legs]

    def t0(self, p: Portfolio) -> int:
        return self.flat.requirement(self._legs(p), self.snap.rows.__getitem__, self.snap.mark, self.snap.day)

    def t1(self, p: Portfolio, row: dict):  # noqa: ANN201
        self.span.rows = [{**row, "applies": {"underlying": self.snap.underlying}}]
        rows = self.span.breakdown(self._legs(p), self.snap.rows.__getitem__, self.snap.mark, self.snap.day)
        return sum(b.total for b in rows), rows


def class_row(margin: dict, snap: Snapshot) -> dict:
    cls = "INDEX" if snap.rows[snap.index_id]["kind"] == "INDEX" else "STOCK"
    rows = [r for r in margin.get("spanlite", []) if r["effective_from"] <= snap.day and
            (r.get("applies", {}).get("underlying") == snap.underlying
             or r.get("applies", {}).get("underlying_class") == cls)]
    if not rows:
        raise CalibrationError(f"no spanlite row in force for {snap.underlying} on {snap.day}")
    named = [r for r in rows if r.get("applies", {}).get("underlying") == snap.underlying]
    return dict(max(named or rows, key=lambda r: r["effective_from"]))


def with_params(row: dict, psr: float, vsr: float, somc: int, psr_fut: float | None = None) -> dict:
    """The row with these settings; psr_fut (D-62) sets `psr_futures_pct`, None leaves the row's own (or none)."""
    out = dict(row)
    out.update(psr_pct=f"{psr:.4f}", vsr_pct=f"{vsr:.4f}", somc_per_lot_paise=int(somc))
    out.update(psr_pct_ppb=numerics.to_ppb(out["psr_pct"]), vsr_pct_ppb=numerics.to_ppb(out["vsr_pct"]))
    if psr_fut is not None:
        out.update(psr_futures_pct=f"{psr_fut:.4f}", psr_futures_pct_ppb=numerics.to_ppb(f"{psr_fut:.4f}"))
    return out


def _golden(f, a: float, b: float, steps: int = 18) -> float:  # noqa: ANN001
    """The x in [a, b] minimising f (golden-section search, one new evaluation per step)."""
    phi = (math.sqrt(5) - 1) / 2
    x1, x2 = b - phi * (b - a), a + phi * (b - a)
    f1, f2 = f(x1), f(x2)
    for _ in range(steps):
        if f1 <= f2:
            b, x2, f2 = x2, x1, f1
            x1 = b - phi * (b - a)
            f1 = f(x1)
        else:
            a, x1, f1 = x1, x2, f2
            x2 = a + phi * (b - a)
            f2 = f(x2)
    return (a + b) / 2


# ── 4. Fit and report ────────────────────────────────────────────────────────

@dataclass
class Sample:
    portfolio: Portfolio
    final_paise: int
    t0_paise: int
    t1_paise: int


TARGET = "span + exposure + additional"
TARGET_WORDS = ("Kite's margin without the premium of bought options (span + exposure + additional): Kite's "
                "`final.total` also counts that premium, which a run pays as cash, not as margin")


def kite_target(answer: dict) -> int:
    """What SPAN-lite is fitted to (IWB 4.5): Kite's final span + exposure + additional, in paise. Not `final.total`,
    which adds `option_premium` (the premium of bought legs, debited as cash in a run, never part of SPAN-lite)."""
    final = answer.get("final") or {}
    total = sum((Decimal(str(final.get(k) or 0)) for k in ("span", "exposure", "additional")), Decimal(0))
    return int((total * 100).to_integral_value())


def _rel(model: int, kite: int) -> float:
    return abs(model - kite) / kite


def fit(pricer: Pricer, samples: list[Sample], base: dict, rounds: int = 3,
        futures: bool = True) -> tuple[dict, float]:
    """Coordinate search (golden section on each of PSR, VSR, SOMC in turn) of the mean relative error against Kite's
    target (`kite_target`). Then, with `futures` and any future samples, the futures price move `psr_futures` (D-62)
    alone, on the FUTURE_KINDS samples; it is kept only if it lowers their error by more than 5 %."""
    usable = [s for s in samples if s.final_paise > 0]
    if not usable:
        raise CalibrationError("no sample with a positive Kite margin to fit")
    cur = {"psr": base["psr_pct_ppb"] / 1e9, "vsr": base["vsr_pct_ppb"] / 1e9, "somc": base["somc_per_lot_paise"],
           "psr_fut": None}

    def err(c: dict, group: list[Sample] = usable) -> float:
        row = with_params(base, c["psr"], c["vsr"], round(c["somc"]), c["psr_fut"])
        return sum(_rel(pricer.t1(s.portfolio, row)[0], s.final_paise) for s in group) / len(group)

    best = err(cur)
    for _ in range(rounds):
        for key in ("psr", "vsr", "somc"):
            cand = {**cur, key: _golden(lambda x: err({**cur, key: x}), *BOUNDS[key])}
            e = err(cand)
            if e < best:
                cur, best = cand, e
    fut = [s for s in usable if s.portfolio.kind in FUTURE_KINDS]
    if futures and fut:
        same = err(cur, fut)
        cand = {**cur, "psr_fut": _golden(lambda x: err({**cur, "psr_fut": x}, fut), *BOUNDS["psr_fut"])}
        if err(cand, fut) < same * 0.95:
            cur, best = cand, err(cand)
    return with_params(base, cur["psr"], cur["vsr"], round(cur["somc"]), cur["psr_fut"]), best


def _quantile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def error_table(samples: list[Sample], t1_fitted: dict[int, int] | None = None) -> dict[str, dict]:
    """Per portfolio type: count and median / p90 absolute relative error of T0, T1 and (if given) fitted T1."""
    out: dict[str, dict] = {}
    for kind in TYPES:
        group = [(n, s) for n, s in enumerate(samples) if s.portfolio.kind == kind and s.final_paise > 0]
        if not group:
            continue
        row = {"count": len(group)}
        for name, get in (("t0", lambda n, s: s.t0_paise), ("t1", lambda n, s: s.t1_paise),
                          ("t1_fit", (lambda n, s: t1_fitted[n]) if t1_fitted else None)):
            if get is None:
                continue
            errs = [_rel(get(n, s), s.final_paise) for n, s in group]
            row[name] = {"median": _quantile(errs, 0.5), "p90": _quantile(errs, 0.9)}
        out[kind] = row
    return out


def report_text(snap: Snapshot, table: dict, base: dict, fitted: dict, before: float, after: float) -> str:
    words = {"single": "single options", "straddle": "short straddles", "strangle": "short strangles",
             "vertical": "spreads", "iron_fly": "iron flies", "iron_condor": "iron condors",
             "future": "futures", "hedged_future": "hedged futures"}
    lines = [f"# SPAN-lite calibration: {snap.underlying}, {snap.day}", "",
             "How close our margin estimates came to Kite's, for the same portfolios priced at the same moment. ",
             "\"Within 5 %\" means our number was no more than 5 % above or below Kite's.", "",
             f"- Fitted to {TARGET_WORDS}.",
             f"- Expiries priced: {', '.join(snap.expiries) or 'none'}"
             + (f"; contracts expiring today left out (--skip-expiring): {', '.join(snap.skipped) or 'none'}."
                if snap.skip_expiring else "; contracts expiring today were not left out."),
             f"- Settings now: price move {base['psr_pct']}"
             + (f" ({base['psr_futures_pct']} for futures)" if "psr_futures_pct" in base else "")
             + f", volatility move {base['vsr_pct']}, minimum "
             f"₹{base['somc_per_lot_paise'] / 100:,.0f} per short lot; off by {before * 100:.1f} % on average.",
             f"- Fitted settings: price move {fitted['psr_pct']}"
             + (f" ({fitted['psr_futures_pct']} for futures)" if "psr_futures_pct" in fitted else "")
             + f", volatility move {fitted['vsr_pct']}, minimum "
             f"₹{fitted['somc_per_lot_paise'] / 100:,.0f} per short lot; off by {after * 100:.1f} % on average.",
             "", "| Portfolios | How many | Flat model: half within / 9 in 10 within | SPAN-lite now | SPAN-lite fitted |",
             "|---|---|---|---|---|"]

    def cell(x: dict | None) -> str:
        return "–" if not x else f"{x['median'] * 100:.1f} % / {x['p90'] * 100:.1f} %"

    for kind, row in table.items():
        lines.append(f"| {words.get(kind, kind)} | {row['count']} | {cell(row.get('t0'))} | {cell(row.get('t1'))} | "
                     f"{cell(row.get('t1_fit'))} |")
    lines += ["", "The fitted settings are written to margin_params.json under `spanlite_pending`. They are NOT used "
              "until you review this report and move the row into `spanlite`."]
    return "\n".join(lines) + "\n"


def pending_row(fitted: dict, snap: Snapshot, n: int, after: float) -> dict:
    keep = ("psr_pct", "psr_futures_pct", "vsr_pct", "extreme_multiple", "extreme_weight", "somc_per_lot_paise",
            "elm_pct", "default_iv")
    return {"applies": {"underlying": snap.underlying}, "effective_from": snap.day,
            **{k: fitted[k] for k in keep if k in fitted}, "calibration_id": f"calibration-{snap.day}",
            "source": f"calibration {snap.day}", "samples": n, "mean_relative_error": f"{after:.4f}",
            "verified": False, "pending_review": True}


def write_pending(row: dict, path: Path | None = None) -> Path:
    """Add the row to margin_params.json → spanlite_pending (kept, never read by a margin model).

    A later run on the same day for the same underlying replaces that day's row: the config loader refuses two rows
    with the same `applies` and `effective_from`, and every earlier sample stays in lab.sqlite either way.
    """
    from stockify.config import loader
    path = path or Path(loader.__file__).with_name("margin_params.json")
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = [r for r in doc.get("spanlite_pending", [])
            if (r.get("applies"), r.get("effective_from")) != (row["applies"], row["effective_from"])]
    doc["spanlite_pending"] = rows + [row]
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# ── The whole run ────────────────────────────────────────────────────────────

def calibrate(client, underlying: str, n: int, *, seed: int = 0, day: str | None = None, sleep=time.sleep,  # noqa: ANN001
              snapshot: Snapshot | None = None, params_path: Path | None = None,
              skip_expiring: bool = False) -> tuple[Path, Path]:
    """Run 1–5; returns (report path, margin_params path). `snapshot` skips the quote calls (tests)."""
    from stockify import paths, store, timeutil
    day = day or timeutil.ist_today().isoformat()
    snap = snapshot or build_snapshot(client, underlying, day, sleep=sleep, skip_expiring=skip_expiring)
    margin, pricing = _config_params()
    base = class_row(margin, snap)
    pricer = Pricer(snap, margin, pricing)
    samples: list[Sample] = []
    for p in sample_portfolios(snap, n, seed):
        sleep(MARGIN_INTERVAL_S)
        answer = client.basket_order_margins(kite_orders(snap, p), consider_positions=False)
        final = kite_target(answer)
        t0 = pricer.t0(p)
        t1, rows = pricer.t1(p, base)
        samples.append(Sample(p, final, t0, t1))
        market = {str(i): snap.quotes.get(i) for i, _ in p.legs} | {
            "underlying": snap.quotes[snap.index_id],
            "snapshot": snap.choice() | {"target": TARGET, "target_paise": final,
                                         "quotes": {str(i): q for i, q in sorted(snap.quotes.items())}}}
        store.add_margin_calibration({
            "wall_ns": snap.wall_ns, "underlying": underlying,
            "portfolio_json": json.dumps({"kind": p.kind, "legs": kite_orders(snap, p)}, sort_keys=True),
            "market_json": json.dumps(market, sort_keys=True), "kite_json": json.dumps(answer, sort_keys=True),
            "t0_paise": t0, "t1_paise": t1,
            "t1_breakdown_json": json.dumps([b.__dict__ for b in rows], sort_keys=True)})
    usable = [s for s in samples if s.final_paise > 0]
    before = sum(_rel(s.t1_paise, s.final_paise) for s in usable) / len(usable) if usable else float("nan")
    fitted, after = fit(pricer, samples, base)
    t1_fit = {n_: pricer.t1(s.portfolio, fitted)[0] for n_, s in enumerate(samples)}
    text = report_text(snap, error_table(samples, t1_fit), base, fitted, before, after)
    out_dir = paths.lab_dir() / "margin_calibration"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"{underlying}-{day}.md"
    report.write_text(text, encoding="utf-8")
    written = write_pending(pending_row(fitted, snap, len(samples), after), params_path)
    return report, written



# ── Refit offline from stored samples (IWB 4.5) ─────────────────────────────

def stored_run(underlying: str, day: str) -> list[dict]:
    """The margin_calibration rows of the last calibration of `underlying` on IST trade date `day`."""
    from stockify import store, timeutil
    rows = [r for r in store.margin_calibrations(underlying)
            if timeutil.ns_to_ist(r["wall_ns"]).date().isoformat() == day]
    if not rows:
        raise CalibrationError(f"no stored calibration samples for {underlying} on {day}")
    last = max(r["wall_ns"] for r in rows)
    return [r for r in rows if r["wall_ns"] == last]


def snapshot_from_rows(underlying: str, day: str, rows: list[dict]) -> tuple[Snapshot, list[Sample], str]:
    """Rebuild the snapshot and samples of a stored run, offline: instruments from refdata, quotes from the rows.

    Rows written from IWB 4.5 on carry every quote of the snapshot ("quotes"), so the rebuild is exact. Earlier
    rows carry only their own legs' quotes; their union stands in for the chain (the forward and the ATM-IV
    fallback can then differ a little from the live run), and the third value says which it was."""
    from stockify.data import instruments
    index_id, is_index = _underlying_row(underlying)
    snap_rows: dict[int, dict] = {index_id: _row_for(index_id, "INDEX" if is_index else "EQ", underlying, index_id,
                                                     is_index, day)}
    quotes: dict[int, dict] = {}
    choice: dict = {}
    exact = True
    parsed = []
    for r in sorted(rows, key=lambda r: r["sample_id"]):
        market = json.loads(r["market_json"])
        choice = market.get("snapshot") or choice
        if "quotes" in (market.get("snapshot") or {}):
            for k, q in market["snapshot"]["quotes"].items():
                quotes[int(k)] = q
        else:
            exact = False
            for k, q in market.items():
                if k.isdigit():
                    quotes[int(k)] = q
        quotes[index_id] = market["underlying"]
        doc = json.loads(r["portfolio_json"])
        legs = []
        for o in doc["legs"]:
            iid = instruments.find(o["exchange"], o["tradingsymbol"])
            if iid is None:
                raise CalibrationError(f"{o['tradingsymbol']} is not in refdata")
            if iid not in snap_rows:
                snap_rows[iid] = _row_for(iid, instruments.get(iid)["kind"], underlying, index_id, is_index, day)
            legs.append((iid, o["quantity"] * (1 if o["transaction_type"] == "BUY" else -1)))
        parsed.append((Portfolio(doc["kind"], legs), json.loads(r["kite_json"]), r["t0_paise"]))
    for iid in quotes:
        if iid not in snap_rows:
            info = instruments.get(iid)
            if info is not None:
                snap_rows[iid] = _row_for(iid, info["kind"], underlying, index_id, is_index, day)
    futures = sorted(i for i, r in snap_rows.items() if r["kind"] == "FUT")
    expiries = choice.get("expiries") or sorted({r["expiry"] for r in snap_rows.values() if r["kind"] in ("CE", "PE")})
    snap = Snapshot(underlying, day, rows[0]["wall_ns"], index_id, snap_rows,
                    {i: q for i, q in quotes.items() if i in snap_rows}, futures, list(expiries),
                    bool(choice.get("skip_expiring")), list(choice.get("skipped") or []))
    samples = [Sample(p, kite_target(answer), 0, 0) for p, answer, _ in parsed]
    return snap, samples, "exact" if exact else "legs_only"


def refit(underlying: str, day: str, *, params_path: Path | None = None) -> tuple[Path, Path]:
    """`margin calibrate --refit`: fit again from the stored samples of `day`, with no Kite call at all; replaces that
    day's pending row and writes `<underlying>-<day>-refit.md` beside the live run's report."""
    from stockify import paths
    snap, samples, how = snapshot_from_rows(underlying, day, stored_run(underlying, day))
    margin, pricing = _config_params()
    base = class_row(margin, snap)
    pricer = Pricer(snap, margin, pricing)
    for s in samples:
        s.t0_paise = pricer.t0(s.portfolio)
        s.t1_paise = pricer.t1(s.portfolio, base)[0]
    usable = [s for s in samples if s.final_paise > 0]
    before = sum(_rel(s.t1_paise, s.final_paise) for s in usable) / len(usable) if usable else float("nan")
    fitted, after = fit(pricer, samples, base)
    t1_fit = {n_: pricer.t1(s.portfolio, fitted)[0] for n_, s in enumerate(samples)}
    text = report_text(snap, error_table(samples, t1_fit), base, fitted, before, after)
    text = text.replace("\n\n", f"\n\nRefitted offline from the {len(samples)} samples stored for that day (no Kite "
                        f"call); quotes: {'the full snapshot' if how == 'exact' else 'only the legs of each sample'}."
                        "\n\n", 1)
    out_dir = paths.lab_dir() / "margin_calibration"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"{underlying}-{day}-refit.md"
    report.write_text(text, encoding="utf-8")
    row = pending_row(fitted, snap, len(samples), after)
    row["source"] = f"calibration {day} (refit offline, target {TARGET})"
    return report, write_pending(row, params_path)
