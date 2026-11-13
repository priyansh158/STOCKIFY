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
from functools import lru_cache
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
# the bundle's instrument kind to what a card says; any other kind (or none) is None, never guessed
_KINDS = {"EQ": "equity", "ETF": "equity", "FUT": "future", "FUT_CONT": "future", "CE": "option", "PE": "option", "INDEX": "index"}


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


def _settlement_close(row: dict | None, fsp: int) -> int | None:
    """The price a cash-settled contract closes at: the FSP for a future, the intrinsic value for an option (the
    runner's own close price, so the profit matches metrics.json). None when the bundle cannot say what the
    contract is (no row, no kind, an option without a strike): the close is never guessed."""
    kind = (row or {}).get("kind")
    strike = (row or {}).get("strike_paise")
    if kind == "FUT":
        return fsp
    if kind == "CE" and strike is not None:
        return max(0, fsp - strike)
    if kind == "PE" and strike is not None:
        return max(0, strike - fsp)
    return None


def _cash_closes(fills: list[_Fill], products: dict, settles: list, inst: dict) -> tuple[list[tuple], dict[int, int], dict[int, int]]:
    """The runner's `settle_closes` of the cash settlements: (t, iid, product, closing side, units, price). The units
    and side are what is open then in that (instrument, product), by the fills up to the settlement's time; a
    settlement with nothing open makes none. A contract that cannot be priced makes none either and is counted per
    instrument in the second value."""
    by_t = sorted(range(len(fills)), key=lambda n: (fills[n].t_ns, n))
    pos: dict[tuple[int, str], int] = {}
    nxt = 0
    out = []
    skipped: dict[int, int] = {}
    idle: dict[int, int] = {}
    for st in sorted((s for s in settles if s.kind == "cash"), key=lambda s: s.t_ns):
        while nxt < len(by_t) and fills[by_t[nxt]].t_ns <= st.t_ns:
            f = fills[by_t[nxt]]
            key = (f.instrument_id, products[f.order_id])
            pos[key] = pos.get(key, 0) + (f.qty if f.side == "BUY" else -f.qty)
            nxt += 1
        key = (st.instrument_id, st.product)
        net = pos.get(key, 0)
        if net == 0:
            idle[st.instrument_id] = idle.get(st.instrument_id, 0) + 1     # nothing open: no marker, but counted
            continue
        close = _settlement_close(inst.get(st.instrument_id), st.price_paise)
        if close is None:
            skipped[st.instrument_id] = skipped.get(st.instrument_id, 0) + 1
            continue
        out.append((st.t_ns, st.instrument_id, st.product, "SELL" if net > 0 else "BUY", abs(net), close))
        pos[key] = 0
    return out, skipped, idle


def _trade(cs: list) -> tuple[int | None, list[dict]]:
    if not cs:
        return None, []
    return (sum(c.pnl for c in cs),
            [{"qty": c.qty, "open_price_paise": c.open_price, "pnl_paise": c.pnl} for c in cs])


def _markers(folder: Path, bundle: dict | None) -> tuple[list[dict], dict[int, int], bool, dict[int, int]]:
    """Every marker of the run (all instruments), ordered by (t_ns, kind == "settlement", fill_id), each with its
    FIFO trade profit from the whole run's fills; also the settlements skipped per instrument and whether the run
    has a physical delivery."""
    return markers_of(_read(folder, "orders.csv"), _read(folder, "fills.csv"), _read(folder, "charges.csv"),
                      read_settlements(folder), bundle)


def markers_of(order_rows: list[dict], fill_rows: list[dict], charge_rows: list[dict], settles: list,
               bundle: dict | None) -> tuple[list[dict], dict[int, int], bool, dict[int, int]]:
    """`_markers` over rows already read (a run's CSVs, or a paper session's stored rows): the same markers, the
    same FIFO profit. Order and fill ids must be unique across the rows given."""
    orders = {r["order_id"]: r for r in order_rows}
    fills = [_Fill(r["fill_id"], r["order_id"], r["t_ns"], r["instrument_id"], r["side"], r["qty"], r["price_paise"])
             for r in fill_rows]
    products = _Products({oid: o.get("product") or "" for oid, o in orders.items()})
    inst = {i["id"]: i for i in (bundle or {}).get("instruments", []) if isinstance(i, dict) and "id" in i}
    closes, skipped, idle = _cash_closes(fills, products, settles, inst)
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
    for c in charge_rows:
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
    delivered = False
    for f in fills:
        order = orders.get(f.order_id)
        note = note_of(order)
        tag = (order or {}).get("tag") or ""
        pnl, cks = _trade(by_fill.get(f.fill_id, []))
        out.append({"kind": "fill", "t_ns": f.t_ns, "bar_start_ns": None, "side": f.side, "qty": f.qty,
                    "price_paise": f.price, "fill_id": f.fill_id, "order_id": f.order_id,
                    "instrument_id": f.instrument_id, "note": note, "tag": tag,
                    "tag_is_code": note == "" and tag in SYSTEM_TAGS,
                    "fsp_paise": None,
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
            delivered = True
        out.append({"kind": "settlement", "t_ns": st.t_ns, "bar_start_ns": None, "side": side, "qty": qty,
                    "price_paise": price, "fsp_paise": st.price_paise, "fill_id": None, "order_id": None, "instrument_id": st.instrument_id,
                    "note": words.replace("{price}", fmt_price(price)), "tag": "", "tag_is_code": False,
                    "charges_paise": settle_cost.get((st.instrument_id, _ist_date(st.t_ns))),
                    "trade_pnl_paise": pnl, "chunks": cks})
    out.sort(key=lambda m: (m["t_ns"], m["kind"] == "settlement", m["fill_id"] or 0))
    return out, skipped, delivered, idle


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


@lru_cache(maxsize=2)
def _read_pack(sha: str) -> tuple[dict, list]:
    """(header, records) of a stored pack. A pack is named by its own hash, so a cached read cannot go stale; callers
    must not change the list."""
    return packs.read(packs.pack_path(sha))


def _whole_seconds(v: object) -> int | None:
    if isinstance(v, str) and v.isascii() and v.isdecimal():
        v = int(v)
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def strategy_interval(bundle: dict | None) -> int | None:
    """The bar interval (seconds) the run's strategy subscribed to, as far as the bundle says: its `interval_s`
    param, else that param's default in the strategy's own schema, else the `BAR_INTERVAL_S` the strategy declares
    for its one fixed subscription. None when none of these exists (a script strategy subscribes to many intervals, a
    user's strategy declares nothing, the bundle names nothing): the chart then rebuilds minute bars and says the
    run does not record its interval. A param that is present but not a positive whole number is not guessed at.
    The durable fix is for the runner to write the intervals it subscribed to into the run folder (D-112)."""
    s = (bundle or {}).get("strategy")
    if not isinstance(s, dict):
        return None
    params = s.get("params")
    if isinstance(params, dict) and "interval_s" in params:
        return _whole_seconds(params["interval_s"])
    from stockify.strategies import _catalogue
    try:
        schema = _catalogue.schema_of(s.get("kind"), s.get("ref"))
        declared = _catalogue.bar_interval_of(s.get("kind"), s.get("ref"))
    except Exception:                                                # a strategy that will not load has no default
        return None
    for row in schema or []:
        if isinstance(row, dict) and row.get("name") == "interval_s":
            return _whole_seconds(row.get("default"))
    return declared


def _span_words(seconds: int) -> str:
    if seconds % 86400 == 0:
        n = seconds // 86400
        return f"{n} day{'s' if n > 1 else ''}"
    if seconds % 60 == 0:
        n = seconds // 60
        return f"{n} minute{'s' if n > 1 else ''}"
    return f"{seconds} seconds"


def _span_adjective(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}-day"
    if seconds % 60 == 0:
        return f"{seconds // 60}-minute"
    return f"{seconds}-second"


def _pack_bars(bundle: dict, iid: int, interval_s: int | None) -> tuple[str, str | None, list, int | None, str | None]:
    """(data_state, basis, bars, interval, notice). `interval_s` is the caller's; None builds at the strategy's own
    interval (`strategy_interval`), else a minute. A pack that is gone is "pruned", one that cannot be read
    "pack unreadable", no pack at all "no_pack"."""
    strategy_s = strategy_interval(bundle)
    want = interval_s or strategy_s
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
        try:
            header, recs = _read_pack(p["sha256"])
        except (packs.PackError, OSError):
            return "pack unreadable", fallback, [], interval_s, None
        basis = basis or header["price_basis"]
        records.extend(recs)
    if len(rows) > 1:
        records.sort(key=lambda r: r["t_event"])                     # stable: a pack's own order breaks ties
    try:
        anchors = chartbars.anchors_from_bundle(bundle, iid)
    except (KeyError, StopIteration, TypeError):
        anchors = {}
    bars, got = chartbars.bars_from_records(records, iid, interval_s=want, anchors=anchors)
    notice = None
    if got is not None:
        if interval_s is not None and got != interval_s:
            notice = f"This run holds {got}-second bars only, not {interval_s}-second bars; they are shown instead."
        elif interval_s is None and strategy_s is not None and got != strategy_s:
            notice = (f"The strategy used {_span_adjective(strategy_s)} bars, but this run holds {got}-second bars "
                      "only; they are shown instead.")
    elif strategy_s is None and any(True for _ in bars):
        notice = (f"This run does not record its bar interval, so these bars are rebuilt at "
                  f"{_span_words(want or DEFAULT_INTERVAL_S)} and may differ from what the strategy used.")
    elif interval_s is not None and interval_s != strategy_s:
        notice = (f"These bars were rebuilt at {_span_words(interval_s)}; the strategy traded on "
                  f"{_span_adjective(strategy_s)} bars.")
    return "ok", basis, bars, got if got is not None else (want or DEFAULT_INTERVAL_S), notice


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


def _cut_for_caps(starts: list[int], i: int, j: int, more: bool, lo: int, hi: int | None,
                  series: list[list[int]]) -> tuple[int, bool, bool]:
    """(j, more, capped): the window's end, pulled back to a bar start when the markers, or the points of one plot,
    in [lo, hi) would pass MAX_BARS. `series` holds each list of times, sorted. A cap that falls inside the first bar
    cannot move the end (j stays one bar past i) and the caller drops the surplus."""
    cut = None
    for times in series:
        k = bisect_left(times, lo)
        end = len(times) if hi is None else bisect_left(times, hi)
        if end - k > MAX_BARS:
            at = times[k + MAX_BARS]
            cut = at if cut is None else min(cut, at)
    if cut is None:
        return j, more, False
    bar = bisect_right(starts, cut) - 1
    if bar < j:
        return max(bar, i + 1), True, True
    return j, more, True


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
    everything, skipped, delivered, idle = _markers(folder, bundle)
    traded = sorted({m["instrument_id"] for m in everything})
    listed = traded or sorted(inst)
    instruments = [{"id": i, "symbol": (inst.get(i) or {}).get("tradingsymbol") or str(i),
                    "kind": _KINDS.get((inst.get(i) or {}).get("kind"))} for i in listed]
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
    series = [s for s in _plots(folder) if s["instrument_id"] in (0, instrument_id)]
    capped = False
    if bars and j > i:
        edge = starts[j] if truncated else to_ns
        j, truncated, capped = _cut_for_caps(starts, i, j, truncated, starts[i], edge,
                                             [[m["t_ns"] for m in mine], *([p[0] for p in s["points"]] for s in series)])
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
            if len(out_markers) >= MAX_BARS:                # the surplus of one bar that alone holds more than a page
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
    for s in series:
        pts = [p for p in s["points"] if lo is None or (p[0] >= lo and (hi is None or p[0] < hi))]
        if lo is not None:
            pts = pts[:MAX_BARS]
        if pts:
            plots.append({**s, "points": pts})
    here = skipped.get(instrument_id, 0)
    notes = [notice] if notice else []
    if capped:
        notes.append(f"This stretch has more markers or plotted points than one page can show ({MAX_BARS:,}), so the "
                     "chart is cut short; load the next part to continue.")
    if delivered:
        notes.append("Profit for contracts settled by delivery isn't shown per trade; the run's total in its metrics "
                     "includes it.")
    if here:
        notes.append(f"{here} expiry settlement{'s' if here > 1 else ''} of this contract could not be priced from "
                     "the run's bundle and are not shown; its profit is incomplete.")
    return {"instruments": instruments, "instrument_id": instrument_id, "basis": basis, "interval_s": interval,
            "bars": [{"start_ns": b.start_ns, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                      "volume": b.volume} for b in shown],
            "markers": out_markers, "omitted_markers": omitted, "plots": plots, "truncated": truncated,
            "next_from_ns": next_from, "data_state": state, "notice": " ".join(notes) or None,
            "pnl_complete": not delivered and not skipped, "skipped_settlements": here,
            "idle_settlements": idle.get(instrument_id, 0)}
