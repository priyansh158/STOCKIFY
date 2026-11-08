"""SPAN-lite calibration against Kite (M4.9): `python -m stockify margin calibrate --underlying NIFTY --samples 200`.

During market hours, with a fresh Kite login (the owner's run; tests use a fake client and make no Kite call):

1. snapshot: the underlying's index, nearest future and the option chains of its two nearest expiries (strikes
   around the money) from refdata, priced with Kite quotes (`client.quote`, ≤ 1 request/s, K-8);
2. sample portfolios from it (seeded): single legs across moneyness and expiries, straddles, strangles, verticals,
   iron flies, iron condors, futures, futures hedged with options;
3. ask Kite's basket margin for each (`client.basket_order_margins(orders, consider_positions=False)`,
   `POST /margins/basket`, K-10; ≤ 10 requests/s, K-8) and store Kite's answer with the snapshot, our T0 (flat)
   and our T1 (SPAN-lite with the current parameters) in lab.sqlite → margin_calibration;
4. fit PSR, VSR and somc_per_lot (coordinate search) to minimise the mean absolute relative error of T1 against
   Kite's `final`; report the median and 90th-percentile error per portfolio type;
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
BOUNDS = {"psr": (0.01, 0.25), "vsr": (0.0, 0.8), "somc": (0, 2_000_000)}   # somc in paise per lot


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


def build_snapshot(client, underlying: str, day: str, *, strikes: int = 8, sleep=time.sleep,  # noqa: ANN001
                   now_ns=None) -> Snapshot:  # noqa: ANN001
    from stockify import timeutil
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
    cls = "INDEX" if is_index else "STOCK"
    rows: dict[int, dict] = {}

    def add(iid: int, kind: str) -> None:
        info = instruments.get(iid)
        spec = instruments.spec_at(iid, day) or {}
        expiry = info.get("expiry")
        group = "FO_OPT" if kind in ("CE", "PE") else "FO_FUT" if kind == "FUT" else "CM"
        last = info.get("actual_expiry") or expiry
        rows[iid] = {"id": iid, "kind": kind, "exchange": info["exchange"], "tradingsymbol": info["tradingsymbol"],
                     "underlying": underlying if kind != "INDEX" else None,
                     "underlying_id": index_id if iid != index_id else None, "underlying_class": cls,
                     "expiry": expiry, "actual_expiry": info.get("actual_expiry"),
                     "strike_paise": info.get("strike_paise"), "lot": spec.get("lot_size") or 1,
                     "last_trading_ns": _last_trading_ns(last, group) if last else None}

    add(index_id, "INDEX" if is_index else "EQ")
    fut_exp = instruments.expiries(underlying, "FUT", day)[:1]
    futures = [iid for e in fut_exp if (iid := instruments.derivative(underlying, "FUT", e)) is not None]
    for iid in futures:
        add(iid, "FUT")
    first = client.quote([f"{rows[i]['exchange']}:{rows[i]['tradingsymbol']}" for i in rows])
    quotes = {i: _quote_fields(first.get(f"{rows[i]['exchange']}:{rows[i]['tradingsymbol']}", {})) for i in rows}
    spot = quotes[index_id]["ltp"]
    if spot is None:
        raise CalibrationError(f"{underlying}: no price for the underlying in the quote")
    expiries = instruments.expiries(underlying, "OPT", day)[:2]
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
    return Snapshot(underlying, day, wall, index_id, rows, quotes, futures, expiries)


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
    B._ppb_fields(margin, ("index_pct", "stock_pct", "pct", "psr_pct", "vsr_pct", "extreme_weight", "elm_pct",
                           "default_iv"))
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


def with_params(row: dict, psr: float, vsr: float, somc: int) -> dict:
    out = dict(row)
    out.update(psr_pct=f"{psr:.4f}", vsr_pct=f"{vsr:.4f}", somc_per_lot_paise=int(somc))
    out.update(psr_pct_ppb=numerics.to_ppb(out["psr_pct"]), vsr_pct_ppb=numerics.to_ppb(out["vsr_pct"]))
    return out


# ── 4. Fit and report ────────────────────────────────────────────────────────

@dataclass
class Sample:
    portfolio: Portfolio
    final_paise: int
    t0_paise: int
    t1_paise: int


def _rel(model: int, kite: int) -> float:
    return abs(model - kite) / kite


def fit(pricer: Pricer, samples: list[Sample], base: dict, rounds: int = 3) -> tuple[dict, float]:
    """Coordinate search (golden section on each of PSR, VSR, SOMC in turn) of the mean relative error on final."""
    usable = [s for s in samples if s.final_paise > 0]
    if not usable:
        raise CalibrationError("no sample with a positive Kite margin to fit")
    cur = {"psr": base["psr_pct_ppb"] / 1e9, "vsr": base["vsr_pct_ppb"] / 1e9, "somc": base["somc_per_lot_paise"]}

    def err(c: dict) -> float:
        row = with_params(base, c["psr"], c["vsr"], round(c["somc"]))
        return sum(_rel(pricer.t1(s.portfolio, row)[0], s.final_paise) for s in usable) / len(usable)

    best = err(cur)
    phi = (math.sqrt(5) - 1) / 2
    for _ in range(rounds):
        for key in ("psr", "vsr", "somc"):
            a, b = BOUNDS[key]
            x1, x2 = b - phi * (b - a), a + phi * (b - a)
            f1, f2 = err({**cur, key: x1}), err({**cur, key: x2})
            for _ in range(18):                              # golden section, one new evaluation per step
                if f1 <= f2:
                    b, x2, f2 = x2, x1, f1
                    x1 = b - phi * (b - a)
                    f1 = err({**cur, key: x1})
                else:
                    a, x1, f1 = x1, x2, f2
                    x2 = a + phi * (b - a)
                    f2 = err({**cur, key: x2})
            cand = {**cur, key: (a + b) / 2}
            e = err(cand)
            if e < best:
                cur, best = cand, e
    return with_params(base, cur["psr"], cur["vsr"], round(cur["somc"])), best


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
             f"- Settings now: price move {base['psr_pct']}, volatility move {base['vsr_pct']}, minimum "
             f"₹{base['somc_per_lot_paise'] / 100:,.0f} per short lot; off by {before * 100:.1f} % on average.",
             f"- Fitted settings: price move {fitted['psr_pct']}, volatility move {fitted['vsr_pct']}, minimum "
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
    keep = ("psr_pct", "vsr_pct", "extreme_multiple", "extreme_weight", "somc_per_lot_paise", "elm_pct", "default_iv")
    return {"applies": {"underlying": snap.underlying}, "effective_from": snap.day,
            **{k: fitted[k] for k in keep}, "calibration_id": f"calibration-{snap.day}",
            "source": f"calibration {snap.day}", "samples": n, "mean_relative_error": f"{after:.4f}",
            "verified": False, "pending_review": True}


def write_pending(row: dict, path: Path | None = None) -> Path:
    """Append the row to margin_params.json → spanlite_pending (kept, never read by a margin model)."""
    from stockify.config import loader
    path = path or Path(loader.__file__).with_name("margin_params.json")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.setdefault("spanlite_pending", []).append(row)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# ── The whole run ────────────────────────────────────────────────────────────

def calibrate(client, underlying: str, n: int, *, seed: int = 0, day: str | None = None, sleep=time.sleep,  # noqa: ANN001
              snapshot: Snapshot | None = None, params_path: Path | None = None) -> tuple[Path, Path]:
    """Run 1–5; returns (report path, margin_params path). `snapshot` skips the quote calls (tests)."""
    from stockify import paths, store, timeutil
    day = day or timeutil.ist_today().isoformat()
    snap = snapshot or build_snapshot(client, underlying, day, sleep=sleep)
    margin, pricing = _config_params()
    base = class_row(margin, snap)
    pricer = Pricer(snap, margin, pricing)
    samples: list[Sample] = []
    for p in sample_portfolios(snap, n, seed):
        sleep(MARGIN_INTERVAL_S)
        answer = client.basket_order_margins(kite_orders(snap, p), consider_positions=False)
        final = _paise((answer.get("final") or {}).get("total")) or 0
        t0 = pricer.t0(p)
        t1, rows = pricer.t1(p, base)
        samples.append(Sample(p, final, t0, t1))
        market = {str(i): snap.quotes.get(i) for i, _ in p.legs} | {"underlying": snap.quotes[snap.index_id]}
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
