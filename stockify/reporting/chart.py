"""The chart model of one run (CH 6.10): its bars, a marker per fill and per expiry settlement with the strategy's
note, the profit of the trade each closing fill ends, and the lines the strategy plotted.

Reads only: the run folder's `bundle.json`, `orders.csv`, `fills.csv`, `charges.csv`, `plots.csv` and
`events_log.txt`, and the run's own pack (through `chartbars`). Nothing is fetched and nothing is written; no
database is opened. An old folder degrades: no `note` column reads as "", no `plots.csv` as no plots, no
`system_notes` as the shipped words. Every note and tag is returned exactly as stored (the page renders text).

Units: prices, charges and profit in paise, quantities in units, times in UTC epoch ns, `interval_s` in seconds.
"""
from __future__ import annotations

import csv
import json
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import NamedTuple

from stockify import paths, runs, timeutil
from stockify.data import packs
from stockify.oracle import metrics
from stockify.oracle.orders import SYSTEM_TAGS
from stockify.reporting import chartbars
from stockify.reporting.settlements import read_settlements
from stockify.strategy import fmt_price

MAX_BARS = 50_000
TAIL_BARS = 5_000                  # the window of a run with no marker and no range: its last bars
DEFAULT_INTERVAL_S = chartbars.DEFAULT_INTERVAL_S
_MINUTE_NS = 60 * 1_000_000_000


class _Fill(NamedTuple):
    fill_id: int
    order_id: int
    t_ns: int
    instrument_id: int
    side: str
    qty: int
    price: int


class _Products(dict):
    """order id to product; a fill whose order is missing from a damaged orders.csv has product ""."""

    def __missing__(self, key):  # noqa: ANN001, ANN204
        return ""


def _int_arg(name: str, v: object, *, positive: bool = False) -> None:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{name} must be a whole number, not {v!r}")
    if positive and v <= 0:
        raise ValueError(f"{name} must be above 0, not {v}")


def _read(folder: Path, name: str) -> list[dict]:
    return runs.read_csv(folder / name) if (folder / name).is_file() else []


def _bundle(folder: Path) -> dict | None:
    try:
        doc = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _system_notes(bundle: dict | None) -> dict:
    notes = (bundle or {}).get("system_notes")
    if isinstance(notes, dict):
        return notes
    from stockify.config import loader
    return loader.load_json("system_notes.json")["notes"]


def _ist_date(t_ns: int) -> str:
    return timeutil.ns_to_ist(t_ns).date().isoformat()


def _settlement_close(row: dict | None, fsp: int) -> int:
    """The price a cash-settled contract closes at: the FSP for a future, the intrinsic value for an option (the
    runner's own close price, so the profit matches metrics.json)."""
    if row is None:
        return fsp
    kind, strike = row.get("kind"), row.get("strike_paise")
    if kind == "CE" and strike is not None:
        return max(0, fsp - strike)
    if kind == "PE" and strike is not None:
        return max(0, strike - fsp)
    return fsp


def _cash_closes(fills: list[_Fill], products: dict, settles: list, inst: dict) -> list[tuple]:
    """The runner's `settle_closes` of the cash settlements: (t, iid, product, closing side, units, price). The units
    and side are what is open then in that (instrument, product), by the fills up to the settlement's time; a
    settlement with nothing open makes none."""
    by_t = sorted(range(len(fills)), key=lambda n: (fills[n].t_ns, n))
    pos: dict[tuple[int, str], int] = {}
    nxt = 0
    out = []
    for st in sorted((s for s in settles if s.kind == "cash"), key=lambda s: s.t_ns):
        while nxt < len(by_t) and fills[by_t[nxt]].t_ns <= st.t_ns:
            f = fills[by_t[nxt]]
            key = (f.instrument_id, products[f.order_id])
            pos[key] = pos.get(key, 0) + (f.qty if f.side == "BUY" else -f.qty)
            nxt += 1
        key = (st.instrument_id, st.product)
        net = pos.get(key, 0)
        if net == 0:
            continue
        out.append((st.t_ns, st.instrument_id, st.product, "SELL" if net > 0 else "BUY", abs(net),
                    _settlement_close(inst.get(st.instrument_id), st.price_paise)))
        pos[key] = 0
    return out


def _trade(cs: list) -> tuple[int | None, list[dict]]:
    if not cs:
        return None, []
    return (sum(c.pnl for c in cs),
            [{"qty": c.qty, "open_price_paise": c.open_price, "pnl_paise": c.pnl} for c in cs])


def _markers(folder: Path, bundle: dict | None) -> list[dict]:
    """Every marker of the run (all instruments), ordered by (t_ns, kind == "settlement", fill_id), each with its
    FIFO trade profit from the whole run's fills."""
    orders = {r["order_id"]: r for r in _read(folder, "orders.csv")}
    fills = [_Fill(r["fill_id"], r["order_id"], r["t_ns"], r["instrument_id"], r["side"], r["qty"], r["price_paise"])
             for r in _read(folder, "fills.csv")]
    products = _Products({oid: o.get("product") or "" for oid, o in orders.items()})
    inst = {i["id"]: i for i in (bundle or {}).get("instruments", []) if isinstance(i, dict) and "id" in i}
    settles = read_settlements(folder)
    closes = _cash_closes(fills, products, settles, inst)
    chunks, _ = metrics.fifo_trades(fills, closes, products)
    by_fill: dict[int, list] = {}
    by_close: dict[tuple, list] = {}
    for c in chunks:
        if c.close_fill_id is not None:
            by_fill.setdefault(c.close_fill_id, []).append(c)
        else:
            by_close.setdefault((c.close_t_ns, c.instrument_id, c.product), []).append(c)

    order_cost: dict[int, int] = {}
    settle_cost: dict[tuple[int, str], int] = {}
    for c in _read(folder, "charges.csv"):
        if c["kind"] == "order":
            order_cost[c["order_id"]] = order_cost.get(c["order_id"], 0) + (c["total"] or 0)
        elif c["kind"] == "settlement":
            key = (c["instrument_id"], c["trade_date"])
            settle_cost[key] = settle_cost.get(key, 0) + (c["total"] or 0)
    last_fill: dict[int, int] = {}
    for f in fills:
        last_fill[f.order_id] = max(last_fill.get(f.order_id, f.fill_id), f.fill_id)

    def note_of(order: dict | None) -> str:
        note = (order or {}).get("note") or ""
        if not note and order is not None and order.get("parent_id") is not None:
            note = (orders.get(order["parent_id"]) or {}).get("note") or ""
        return note

    out = []
    for f in fills:
        order = orders.get(f.order_id)
        note = note_of(order)
        tag = (order or {}).get("tag") or ""
        pnl, cks = _trade(by_fill.get(f.fill_id, []))
        out.append({"kind": "fill", "t_ns": f.t_ns, "bar_start_ns": None, "side": f.side, "qty": f.qty,
                    "price_paise": f.price, "fill_id": f.fill_id, "order_id": f.order_id,
                    "instrument_id": f.instrument_id, "note": note, "tag": tag,
                    "tag_is_code": note == "" and tag in SYSTEM_TAGS,
                    "charges_paise": order_cost.get(f.order_id) if last_fill[f.order_id] == f.fill_id else None,
                    "trade_pnl_paise": pnl, "chunks": cks})
    words = _system_notes(bundle).get("SETTLEMENT") or ""
    closed = {(t, iid, prod): (side, units, price) for t, iid, prod, side, units, price in closes}
    for st in settles:
        if st.kind == "cash":
            hit = closed.get((st.t_ns, st.instrument_id, st.product))
            if hit is None:
                continue                                            # nothing was open
            side, qty, price = hit
            pnl, cks = _trade(by_close.get((st.t_ns, st.instrument_id, st.product), []))
        else:
            side, qty, price, pnl, cks = st.side, st.qty, st.price_paise, None, []
        out.append({"kind": "settlement", "t_ns": st.t_ns, "bar_start_ns": None, "side": side, "qty": qty,
                    "price_paise": price, "fill_id": None, "order_id": None, "instrument_id": st.instrument_id,
                    "note": words.replace("{price}", fmt_price(price)), "tag": "", "tag_is_code": False,
                    "charges_paise": settle_cost.get((st.instrument_id, _ist_date(st.t_ns))),
                    "trade_pnl_paise": pnl, "chunks": cks})
    out.sort(key=lambda m: (m["t_ns"], m["kind"] == "settlement", m["fill_id"] or 0))
    return out


def _plots(folder: Path) -> list[dict]:
    """Every series of plots.csv (all instruments), in order of first appearance; an old folder has none."""
    path = folder / "plots.csv"
    if not path.is_file():
        return []
    series: dict[tuple[int, str], dict] = {}
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            iid = int(r["instrument_id"])
            s = series.setdefault((iid, r["name"]), {"name": r["name"], "kind": r["kind"], "instrument_id": iid,
                                                     "points": []})
            s["points"].append([int(r["t_ns"]), int(r["value"])])
    return list(series.values())


def _pack_bars(bundle: dict, iid: int, interval_s: int | None) -> tuple[str, str | None, list, int | None, str | None]:
    """(data_state, basis, bars, interval, notice). A pack that is gone is "pruned"; no pack at all is "no_pack"."""
    rows = [p for p in (bundle.get("packs") or []) if isinstance(p, dict) and p.get("sha256")]
    fallback = (bundle.get("run") or {}).get("price_basis")
    if not rows:
        return "no_pack", fallback, [], interval_s, None
    records, basis = [], None
    for p in rows:
        try:
            path = packs.pack_path(p["sha256"])
        except (ValueError, TypeError, packs.PackError):            # not a pack hash: nothing to read
            return "pruned", fallback, [], interval_s, None
        if not path.is_file():
            return "pruned", fallback, [], interval_s, None
        header, recs = packs.read(path)
        basis = basis or header["price_basis"]
        records.extend(recs)
    if len(rows) > 1:
        records.sort(key=lambda r: r["t_event"])                     # stable: a pack's own order breaks ties
    try:
        anchors = chartbars.anchors_from_bundle(bundle, iid)
    except (KeyError, StopIteration, TypeError):
        anchors = {}
    bars, got = chartbars.bars_from_records(records, iid, interval_s=interval_s, anchors=anchors)
    notice = None
    if got is not None and interval_s is not None and got != interval_s:
        notice = f"This run holds {got}-second bars only, not {interval_s}-second bars; they are shown instead."
    return "ok", basis, bars, got if got is not None else (interval_s or DEFAULT_INTERVAL_S), notice


def _window(starts: list[int], first_t: int | None, from_ns: int | None, to_ns: int | None) -> tuple[int, int, bool]:
    """([i, j), more): the bars to return. With a range, the first MAX_BARS bars with start in [from_ns, to_ns), and
    `more` when the range holds others; without, the MAX_BARS around the first marker (or the last TAIL_BARS), and
    `more` when bars follow the window."""
    n = len(starts)
    if from_ns is not None or to_ns is not None:
        i = bisect_left(starts, from_ns) if from_ns is not None else 0
        end = max(i, bisect_left(starts, to_ns)) if to_ns is not None else n
        j = min(end, i + MAX_BARS)
        return i, j, j < end
    if first_t is None:
        return max(0, n - TAIL_BARS), n, False
    at = max(0, bisect_right(starts, first_t) - 1)
    i = min(max(0, at - MAX_BARS // 2), max(0, n - MAX_BARS))
    j = min(n, i + MAX_BARS)
    return i, j, j < n


def run_chart(run_id: str, instrument_id: int | None = None, from_ns: int | None = None, to_ns: int | None = None,
              *, interval_s: int | None = None) -> dict:
    """The chart model of one instrument of a run. `instrument_id` None is the first traded instrument (lowest id
    among the fills and settlements, else the bundle's first). `interval_s` None is the bars the run saw (60 s for
    a tick run). An unknown run raises LookupError; a bad id, range or interval raises ValueError."""
    if instrument_id is not None:
        _int_arg("instrument_id", instrument_id)
    for name, v in (("from_ns", from_ns), ("to_ns", to_ns)):
        if v is not None:
            _int_arg(name, v)
    if interval_s is not None:
        _int_arg("interval_s", interval_s, positive=True)
    folder = paths.run_path(run_id)
    if not folder.is_dir():
        raise LookupError(f"no run {run_id!r}")
    bundle = _bundle(folder)
    inst = {i["id"]: i for i in (bundle or {}).get("instruments", []) if isinstance(i, dict) and "id" in i}
    everything = _markers(folder, bundle)
    traded = sorted({m["instrument_id"] for m in everything})
    listed = traded or sorted(inst)
    instruments = [{"id": i, "symbol": (inst.get(i) or {}).get("tradingsymbol") or str(i)} for i in listed]
    if instrument_id is None:
        instrument_id = listed[0] if listed else None
    elif instrument_id not in listed and instrument_id not in inst:
        raise LookupError(f"run {run_id!r} has no instrument {instrument_id}")
    mine = [m for m in everything if m["instrument_id"] == instrument_id]

    state, basis, bars, interval, notice = "no_pack", None, [], interval_s, None
    if bundle is not None and instrument_id is not None:
        state, basis, bars, interval, notice = _pack_bars(bundle, instrument_id, interval_s)
    starts = [b.start_ns for b in bars]
    i, j, truncated = _window(starts, mine[0]["t_ns"] if mine else None, from_ns, to_ns) if bars else (0, 0, False)
    shown = bars[i:j]
    next_from = starts[j] if truncated else None

    omitted = 0
    if shown:
        lo = shown[0].start_ns
        hi = next_from if truncated else to_ns
        wstarts = starts[i:j]
        out_markers = []
        for m in mine:
            t = m["t_ns"]
            if t < lo or (hi is not None and t >= hi):
                omitted += 1
                continue
            out_markers.append({**m, "bar_start_ns": wstarts[bisect_right(wstarts, t) - 1]})
    elif bars:                                      # a range that holds no bar: nothing to place a marker on
        lo, hi, out_markers, omitted = 0, 0, [], len(mine)
    else:
        lo, hi = None, None
        out_markers = [{**m, "bar_start_ns": m["t_ns"] - m["t_ns"] % _MINUTE_NS} for m in mine]
    for m in out_markers:
        del m["instrument_id"]

    plots = []
    for s in _plots(folder):
        if s["instrument_id"] not in (0, instrument_id):
            continue
        pts = [p for p in s["points"] if lo is None or (p[0] >= lo and (hi is None or p[0] < hi))]
        if pts:
            plots.append({**s, "points": pts})
    return {"instruments": instruments, "instrument_id": instrument_id, "basis": basis, "interval_s": interval,
            "bars": [{"start_ns": b.start_ns, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                      "volume": b.volume} for b in shown],
            "markers": out_markers, "omitted_markers": omitted, "plots": plots, "truncated": truncated,
            "next_from_ns": next_from, "data_state": state, "notice": notice}
