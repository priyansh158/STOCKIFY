"""Event packs (`.evt` v1, A1 §5): the engine's input files (M1.8).

    sha = packs.build({"dataset": "ticks", "from": "2026-11-05", "to": "2026-11-05",
                       "instruments": [{"chain": {"underlying": "NIFTY", "expiry": "nearest",
                                                  "strikes": 10}}]})
    header, records = packs.read(packs.pack_path(sha))

Spec (JSON):

| key              | meaning                                                              |
|------------------|----------------------------------------------------------------------|
| dataset          | `ticks` (recorded days), `bars` (Kite cache), `bhav_eod` (NSE files) |
| from, to         | IST trade dates, inclusive                                           |
| instruments      | list of selectors (below); omitted = every recorded instrument (ticks only) |
| tiers            | ticks: subset of `full`, `quote`, `index` (default all)              |
| interval         | bars: Kite interval (`minute` … `day`)                               |
| clock_cadence_ms | CLOCK events every N ms inside each day's data span (0 = none)       |
| include_gaps     | GAP records from the recording (default true)                        |
| skip_missing_days| ticks: trading days without a recording are skipped instead of an error |

Selectors: `"NSE:RELIANCE"` (shorthand), `{"ids": [..]}`, `{"symbols": ["NSE:RELIANCE", "NFO:NIFTY26NOVFUT"]}`,
`{"chain": {"underlying": "NIFTY", "expiry": "nearest" | "next" | "YYYY-MM-DD", "strikes": N,
"kinds": ["CE", "PE"], "futures": true}}`. A chain's ATM comes from the
underlying's close on the previous trading day (`index_eod` / `cm_eod`), which
was known before the session; `strikes` = N strikes either side.

Records and `t_event` (A4 §1.1): ticks → receive time; Kite bars → bar start +
interval; bhav_eod bars → `pricing.json` `eod_publication_ist` on the trade
date; SETTLE daily → the same EOD time, final → `fsp_publication_ist` on the
expiry; REFDATA `instrument_add` → 00:00 IST of the first day an instrument
appears, except an instrument the recorder first subscribed later in the day
(a chain re-centre, any reason but `start…`): then the moment it was
subscribed, when the daemon learned it (A4 §1.1, M4.12); `spec_change` → 00:00 IST of its effective date; `price_band` → the
snapshot time; GAP → its start / end.

Order: `(t_event, rank, source seq, instrument_id)` with A4 §1.3 ranks, then
`seq` = 1…N. Ticks are already in receive order within a day, so each day is
a merge of its tick records with the (small) other events; a day whose
recorded clock stepped backwards is sorted instead. One day's records are in
memory at a time.

The file is named by its SHA-256; `<sha>.json` beside it is the manifest. The
same spec over the same sources gives the same bytes (P-05).
"""
from __future__ import annotations

import hashlib
import heapq
import json
import os
import re
import secrets
import struct
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from stockify import numerics, paths, timeutil

MAGIC = b"STKEVT01"
VERSION = 1
HEADER = struct.Struct("<8sHHIQqqB23x")
PREFIX = struct.Struct("<HHIqQ")
QUOTE = struct.Struct("<10q")
FULL_EXTRA = struct.Struct("<5q")
LEVEL = struct.Struct("<qqii")
INDEX = struct.Struct("<7q")
BAR = struct.Struct("<iiq4qqq")
GAP = struct.Struct("<BBHI")
REFDATA = struct.Struct("<HHI")
SETTLE = struct.Struct("<B3xiq")

TICK_QUOTE, TICK_FULL, INDEX_K, BAR_K, CLOCK, GAP_K, REFDATA_K, SETTLE_K = range(1, 9)
# The blocks below must match docs/plan/A1_schemas.md (or history.py); blockwatch checks them.
# <block name="event-kinds" same-as="docs/plan/A1_schemas.md:event-kinds" same-as-pattern='(?P<key>\d+): "(?P<value>[A-Z_]+)"'>
KIND_NAMES = {1: "TICK_QUOTE", 2: "TICK_FULL", 3: "INDEX", 4: "BAR", 5: "CLOCK", 6: "GAP", 7: "REFDATA", 8: "SETTLE"}
# </block>
RANK = {TICK_QUOTE: 4, TICK_FULL: 4, INDEX_K: 4, BAR_K: 4, CLOCK: 5, GAP_K: 1, REFDATA_K: 2, SETTLE_K: 3}
SIZE = {TICK_QUOTE: 104, TICK_FULL: 384, INDEX_K: 80, BAR_K: 88, CLOCK: 24, GAP_K: 32, SETTLE_K: 40}
# <block name="gap-reasons" same-as="docs/plan/A1_schemas.md:gap-reasons" same-as-pattern='"(?P<value>[a-z_]+)": (?P<key>\d+)'>
GAP_REASONS = {"disconnect": 1, "process_stop": 2, "token_expired": 3, "stall": 4, "not_subscribed": 5}
# </block>
# <block name="refdata-subtypes" same-as="docs/plan/A1_schemas.md:refdata-subtypes" same-as-pattern='"(?P<value>[a-z_]+)": (?P<key>\d+)'>
REFDATA_SUBTYPES = {"instrument_add": 1, "price_band": 2, "margin_requirement": 3, "spec_change": 4}
# </block>
BASIS = {"raw": 0, "adjusted": 1}
FLAG_CLOCK = 2
MAX_JSON = 65_535 - 32
# <block name="bar-intervals" same-as="stockify/data/history.py#/INTERVALS" same-as-mode="sequence" same-as-pattern='"(?P<value>[0-9a-z]+)"'>
INTERVAL_S = {"minute": 60, "3minute": 180, "5minute": 300, "10minute": 600, "15minute": 900,
              "30minute": 1800, "60minute": 3600, "day": 86400}
# </block>
SEC = 1_000_000_000


class PackError(ValueError):
    pass


# ── Encoding ─────────────────────────────────────────────────────────────────

def _i(v) -> int:  # noqa: ANN001
    return 0 if v is None else int(v)


def _prefix(kind: int, size: int, iid: int, t: int, seq: int = 0) -> bytes:
    return PREFIX.pack(kind, size, iid, t, seq)


def enc_tick(p: dict, iid: int, t: int) -> tuple[int, bytes]:
    """A recorded packet (parsed from its raw bytes) → TICK_QUOTE / TICK_FULL / INDEX payload."""
    from stockify.daemon import kite_packets as kp
    raw = p["raw"]
    n = len(raw)
    f = kp.parse_packet(raw)
    if n in (28, 32):
        body = INDEX.pack(_i(f["ltp"]), _i(f["high"]), _i(f["low"]), _i(f["open"]), _i(f["close"]),
                          _i(kp.index_change(raw)), _i(f["exch_ts_s"]))
        return INDEX_K, _prefix(INDEX_K, 80, iid, t) + body
    if n not in (44, 184):
        raise PackError(f"packet of {n} bytes (LTP mode) has no pack record kind")
    quote = QUOTE.pack(*(_i(f[k]) for k in ("ltp", "last_qty", "atp", "volume", "buy_qty", "sell_qty",
                                             "open", "high", "low", "close")))
    if n == 44:
        return TICK_QUOTE, _prefix(TICK_QUOTE, 104, iid, t) + quote
    extra = FULL_EXTRA.pack(*(_i(f[k]) for k in ("last_trade_ts_s", "oi", "oi_high", "oi_low", "exch_ts_s")))
    depth = b"".join(LEVEL.pack(price, qty, orders, 0) for qty, price, orders in kp.parse_depth(raw))
    return TICK_FULL, _prefix(TICK_FULL, 384, iid, t) + quote + extra + depth


def enc_bar(iid: int, t: int, interval_s: int, start_ns: int, o, h, lo, c, vol, oi) -> bytes:  # noqa: ANN001
    return _prefix(BAR_K, 88, iid, t) + BAR.pack(interval_s, 0, start_ns, _i(o), _i(h), _i(lo), _i(c), _i(vol), _i(oi))


def enc_clock(t: int) -> bytes:
    return _prefix(CLOCK, 24, 0, t)


def enc_gap(iid: int, t: int, phase: int, reason: str) -> bytes:
    if reason not in GAP_REASONS:
        raise PackError(f"unknown gap reason {reason!r}")
    return _prefix(GAP_K, 32, iid, t) + GAP.pack(phase, 0, GAP_REASONS[reason], 1 if iid else 0)


def enc_refdata(iid: int, t: int, subtype: str, payload: dict) -> bytes:
    data = numerics.dumps_canonical(payload).encode("utf-8")
    if len(data) > MAX_JSON:
        raise PackError(f"REFDATA {subtype} for {iid} is {len(data)} bytes (max {MAX_JSON}); split it")
    return _prefix(REFDATA_K, 32 + len(data), iid, t) + REFDATA.pack(REFDATA_SUBTYPES[subtype], 0, len(data)) + data


def enc_settle(iid: int, t: int, subtype: int, day: str, price_paise: int) -> bytes:
    return _prefix(SETTLE_K, 40, iid, t) + SETTLE.pack(subtype, int(day.replace("-", "")), price_paise)


def _with_seq(rec: bytes, seq: int) -> bytes:
    return rec[:16] + struct.pack("<Q", seq) + rec[24:]


# ── Reading ──────────────────────────────────────────────────────────────────

def read_header(data: bytes) -> dict:
    magic, version, flags, hsize, count, tmin, tmax, basis = HEADER.unpack_from(data, 0)
    if magic != MAGIC or version != VERSION or hsize != 64:
        raise PackError(f"not an .evt v1 file (magic {magic!r}, version {version}, header {hsize})")
    return {"version": version, "flags": flags, "count": count, "t_min": tmin, "t_max": tmax,
            "price_basis": {v: k for k, v in BASIS.items()}[basis]}


def decode(data: bytes, offset: int) -> tuple[dict, int]:
    kind, size, iid, t, seq = PREFIX.unpack_from(data, offset)
    if kind not in KIND_NAMES:
        raise PackError(f"unknown record kind {kind} at byte {offset}")
    if kind in SIZE and size != SIZE[kind]:
        raise PackError(f"{KIND_NAMES[kind]} of size {size} at byte {offset}")
    rec = {"kind": KIND_NAMES[kind], "instrument_id": iid, "t_event": t, "seq": seq}
    body = offset + 24
    if kind in (TICK_QUOTE, TICK_FULL):
        rec.update(zip(("ltp", "last_qty", "atp", "volume", "buy_qty", "sell_qty", "open", "high", "low", "close"),
                       QUOTE.unpack_from(data, body)))
        if kind == TICK_FULL:
            rec.update(zip(("last_trade_ts_s", "oi", "oi_high", "oi_low", "exch_ts_s"),
                           FULL_EXTRA.unpack_from(data, body + 80)))
            rec["depth"] = [LEVEL.unpack_from(data, body + 120 + 24 * i)[:3] for i in range(10)]
    elif kind == INDEX_K:
        rec.update(zip(("value", "high", "low", "open", "close", "change", "exch_ts_s"), INDEX.unpack_from(data, body)))
    elif kind == BAR_K:
        isec, _, start, o, h, lo, c, vol, oi = BAR.unpack_from(data, body)
        rec.update(interval_s=isec, start_ns=start, open=o, high=h, low=lo, close=c, volume=vol, oi=oi)
    elif kind == GAP_K:
        phase, _, reason, scope = GAP.unpack_from(data, body)
        rec.update(phase=phase, reason={v: k for k, v in GAP_REASONS.items()}[reason], scope=scope)
    elif kind == REFDATA_K:
        sub, _, n = REFDATA.unpack_from(data, body)
        rec.update(subtype={v: k for k, v in REFDATA_SUBTYPES.items()}[sub],
                   payload=json.loads(data[body + 8: body + 8 + n].decode("utf-8")))
        if size != 32 + n:
            raise PackError(f"REFDATA size {size} != 32 + {n}")
    elif kind == SETTLE_K:
        sub, d, price = SETTLE.unpack_from(data, body)
        rec.update(subtype=sub, date=f"{d // 10000:04d}-{d // 100 % 100:02d}-{d % 100:02d}", price_paise=price)
    return rec, offset + size


def read(path) -> tuple[dict, list[dict]]:  # noqa: ANN001
    """(header, records) of a pack; checks size, order and the record count."""
    data = read_bytes(path)
    header = read_header(data)
    out, off, last = [], 64, None
    while off < len(data):
        rec, off = decode(data, off)
        key = (rec["t_event"], RANK[next(k for k, v in KIND_NAMES.items() if v == rec["kind"])], rec["seq"])
        if last is not None and key < last:
            raise PackError(f"records out of order at seq {rec['seq']}")
        last = key
        out.append(rec)
    if off != len(data) or len(out) != header["count"]:
        raise PackError(f"pack has {len(out)} records, header says {header['count']}")
    return header, out


def read_numpy(path, kind: str = "TICK_FULL"):  # noqa: ANN001, ANN201
    """Fixed-size records of one kind as a numpy structured array (prefix fields + payload)."""
    import numpy as np
    header, records = read(path)
    rows = [r for r in records if r["kind"] == kind]
    if not rows:
        return np.zeros(0)
    names = [k for k in rows[0] if k not in ("kind", "depth", "payload", "reason", "subtype", "date")]
    return np.array([tuple(r[k] for k in names) for r in rows], dtype=[(k, "<i8") for k in names])


# ── Selection ────────────────────────────────────────────────────────────────

def _prev_close(underlying_id: int, day: date) -> int | None:
    from stockify.data import calendar, refdata
    prev = calendar.previous_trading_day("FO", day).isoformat()
    conn = refdata._connect()
    for table in ("index_eod", "cm_eod"):
        row = conn.execute(f"SELECT close_paise FROM {table} WHERE trade_date=? AND instrument_id=? "
                           f"{'AND series=' + repr('EQ') if table == 'cm_eod' else ''}",
                           (prev, underlying_id)).fetchone()
        if row and row[0]:
            return row[0]
    return None


def _chain(sel: dict, day: date) -> list[int]:
    from stockify.data import instruments
    und = sel["underlying"].upper()
    kinds = [k.upper() for k in sel.get("kinds", ["CE", "PE"])]
    exp = sel.get("expiry", "nearest")
    if exp in ("nearest", "next"):
        listed = instruments.expiries(und, "OPT", day)
        k = 0 if exp == "nearest" else 1
        if len(listed) <= k:
            raise PackError(f"no {exp} {und} option expiry listed on {day}")
        exp = listed[k]
    chain = [r for r in instruments.option_chain(und, exp, day) if r["kind"] in kinds]
    if not chain:
        raise PackError(f"no {und} {exp} options listed on {day}")
    out = []
    if "strikes" in sel:
        uid = instruments.underlying_of(chain[0]["instrument_id"])
        ref = _prev_close(uid, day) if uid else None
        if ref is None:
            raise PackError(f"{und}: no previous-day close to centre the chain on {day} (load NSE files first)")
        strikes = sorted({r["strike_paise"] for r in chain})
        atm = min(range(len(strikes)), key=lambda i: (abs(strikes[i] - ref), strikes[i]))
        n = int(sel["strikes"])
        keep = set(strikes[max(0, atm - n): atm + n + 1])
        chain = [r for r in chain if r["strike_paise"] in keep]
    out += [r["instrument_id"] for r in chain]
    if sel.get("futures"):
        for e in instruments.expiries(und, "FUT", day)[:1]:
            fut = instruments.derivative(und, "FUT", e)
            if fut:
                out.append(fut)
    return out


def resolve(selectors: list[dict] | None, day: date, recorded: set[int] | None = None) -> set[int]:
    """Instrument ids the selectors name on `day` (None = all recorded)."""
    from stockify.data import instruments
    if selectors is None:
        if recorded is None:
            raise PackError("this dataset needs an `instruments` list")
        return set(recorded)
    out: set[int] = set()
    for sel in selectors:
        if isinstance(sel, str):                     # "NSE:RELIANCE" (A1 §6.1 shorthand)
            sel = {"symbols": [sel]}
        if "ids" in sel:
            out |= {int(i) for i in sel["ids"]}
        elif "symbols" in sel:
            for s in sel["symbols"]:
                exchange, _, name = s.rpartition(":")
                iid = instruments.find(exchange or "NSE", name)
                if iid is None:
                    raise PackError(f"unknown symbol {s!r}")
                out.add(iid)
        elif "chain" in sel:
            out |= set(_chain(sel["chain"], day))
        else:
            raise PackError(f"unknown selector {sel}")
    return out


# ── Building ─────────────────────────────────────────────────────────────────

@dataclass
class _Build:
    spec: dict
    basis: str
    events: list = field(default_factory=list)       # (key, bytes) for non-streamed records
    sources: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    added: set = field(default_factory=set)
    first_day: dict = field(default_factory=dict)
    counter: int = 0

    def add(self, t: int, rank: int, iid: int, rec: bytes, src_seq: int | None = None) -> None:
        if src_seq is None:
            self.counter += 1
            src_seq = self.counter
        self.events.append(((t, rank, src_seq, iid), rec))


def _pricing() -> dict:
    from stockify.config import loader
    return loader.load_json("pricing.json")


def _ist(day: date | str, hhmm: str) -> int:
    d = date.fromisoformat(day) if isinstance(day, str) else day
    return timeutil.ist_ns(d, hhmm)


def _refdata_events(b: _Build, ids: set[int], day: date, learned: dict[int, int] | None = None) -> None:
    """instrument_add on first appearance (at 00:00, or at `learned[iid]` for an instrument subscribed mid-day),
    spec changes, price bands."""
    from stockify.data import instruments, refdata
    conn = refdata._connect()
    t0 = _ist(day, "00:00")
    learned = learned or {}
    for iid in sorted(ids - b.added):
        info = instruments.get(iid)
        if info is None:
            raise PackError(f"instrument {iid} is not in refdata")
        spec = instruments.spec_at(iid, day) or {}
        t_add = learned.get(iid, t0)
        b.add(t_add, 2, iid, enc_refdata(iid, t_add, "instrument_add", {
            "instrument_id": iid, "exchange": info["exchange"], "kind": info["kind"],
            "tradingsymbol": info["tradingsymbol"], "underlying": info["underlying"],
            "underlying_id": info["underlying_id"], "expiry": info["expiry"],
            "actual_expiry": info.get("actual_expiry"), "strike_paise": info["strike_paise"],
            "lot_size": spec.get("lot_size"), "tick_paise": spec.get("tick_paise")}))
        b.added.add(iid)
        b.first_day[iid] = day.isoformat()
    d = day.isoformat()
    for iid in sorted(ids):
        row = conn.execute("SELECT effective_from, lot_size, tick_paise, source FROM contract_specs "
                           "WHERE instrument_id=? AND effective_from=?", (iid, d)).fetchone()
        if row and b.first_day.get(iid) != d:
            b.add(t0, 2, iid, enc_refdata(iid, t0, "spec_change", dict(row) | {"instrument_id": iid}))
        for lim in conn.execute("SELECT snapshot_ns, upper_paise, lower_paise, source FROM daily_limits "
                                "WHERE trade_date=? AND instrument_id=? ORDER BY snapshot_ns", (d, iid)):
            b.add(lim["snapshot_ns"], 2, iid, enc_refdata(iid, lim["snapshot_ns"], "price_band", {
                "instrument_id": iid, "upper_paise": lim["upper_paise"], "lower_paise": lim["lower_paise"],
                "source": lim["source"]}))


def _settle_events(b: _Build, ids: set[int], day: date) -> None:
    from stockify.data import refdata
    conn = refdata._connect()
    pricing = _pricing()
    d = day.isoformat()
    t_eod = _ist(day, pricing["eod_publication_ist"])
    t_fsp = _ist(day, pricing["fsp_publication_ist"])
    marks = ",".join("?" * len(ids)) or "NULL"
    for r in conn.execute(f"SELECT instrument_id, settle_paise FROM fo_eod WHERE trade_date=? AND "
                          f"instrument_id IN ({marks}) AND settle_paise IS NOT NULL ORDER BY instrument_id",
                          (d, *sorted(ids))):
        b.add(t_eod, 3, r[0], enc_settle(r[0], t_eod, 1, d, r[1]))
    unds = sorted({r[0] for r in conn.execute(
        f"SELECT DISTINCT underlying_id FROM instruments WHERE instrument_id IN ({marks}) "
        "AND IFNULL(actual_expiry, expiry)=? "
        "AND underlying_id IS NOT NULL", (*sorted(ids), d))})
    for uid in unds:
        row = conn.execute("SELECT fsp_paise FROM settlement_prices WHERE underlying_id=? AND expiry=?",
                           (uid, d)).fetchone()
        if row:
            b.add(t_fsp, 3, uid, enc_settle(uid, t_fsp, 0, d, row[0]))


def _clock_events(b: _Build, t_lo: int, t_hi: int) -> list:
    cad = int(b.spec.get("clock_cadence_ms", 0)) * 1_000_000
    if cad <= 0 or t_lo is None:
        return []
    start = -(-t_lo // cad) * cad
    return [((t, 5, i, 0), enc_clock(t)) for i, t in enumerate(range(start, t_hi + 1, cad), 1)]


def _tier_ids(subs: list[dict], tier: str, t: int) -> set[int]:
    state: dict[int, str | None] = {}
    for row in sorted(subs, key=lambda r: r["recv_ns"] or 0):
        if row["recv_ns"] is not None and row["recv_ns"] > t:
            break
        if row["instrument_id"] is None:
            continue
        state[row["instrument_id"]] = row["mode"] if row["action"] == "subscribe" else None
    return {i for i, m in state.items() if m == tier}


def _gap_events(b: _Build, tick_day, ids: set[int]) -> None:  # noqa: ANN001
    subs = tick_day.subscriptions()
    for g in sorted(tick_day.gaps(), key=lambda g: (g["start_ns"] or 0, g["end_ns"] or 0, g["reason"])):
        scope = g.get("scope") or "all"
        if scope == "all":
            targets = [0]
        elif scope.startswith("tier:"):
            targets = sorted(_tier_ids(subs, scope[5:], g["start_ns"]) & ids)
        elif scope.startswith("instrument:"):
            targets = [int(scope[11:])] if int(scope[11:]) in ids else []
        else:
            raise PackError(f"unknown gap scope {scope!r}")
        for iid in targets:
            for phase, t in ((0, g["start_ns"]), (1, g["end_ns"])):
                if t is not None:
                    b.add(t, 1, iid, enc_gap(iid, t, phase, g["reason"]))


def _ticks_day(b: _Build, day: date):  # noqa: ANN202
    """(sorted key/bytes iterator, t_min, t_max) for one recorded day."""
    from stockify.data import ticks
    tick_day = ticks.open_day(day)
    if tick_day is None:
        if b.spec.get("skip_missing_days"):
            return None
        raise PackError(f"{day} was not recorded (set skip_missing_days to leave it out)")
    with tick_day:
        return _ticks_day_read(b, day, tick_day)


def _learned_mid_day(subs: list[dict]) -> dict[int, int]:
    """instrument → receive time of its first subscription, for instruments whose first subscription that day was
    not part of the session's start batch (reason `start…`), i.e. strikes a chain re-centre added (M4.12)."""
    first: dict[int, dict] = {}
    for s in sorted((s for s in subs if s["action"] == "subscribe" and s["instrument_id"] is not None),
                    key=lambda s: (s["recv_ns"], s["instrument_id"])):
        first.setdefault(s["instrument_id"], s)
    return {iid: s["recv_ns"] for iid, s in first.items() if not str(s.get("reason") or "").startswith("start")}


def _ticks_day_read(b: _Build, day: date, tick_day):  # noqa: ANN001, ANN202
    from stockify.data import ticks
    subs = tick_day.subscriptions()
    recorded = {s["instrument_id"] for s in subs if s["instrument_id"] is not None}
    learned = _learned_mid_day(subs)
    ids = resolve(b.spec.get("instruments"), day, recorded or None)
    tiers = tuple(b.spec.get("tiers", ticks.KINDS))
    digest = hashlib.sha256()
    t_lo = t_hi = None
    unknown = 0
    out = []
    monotonic = True
    last_t = None
    for p in tick_day.packets(["seq", "recv_ns", "instrument_id", "raw"], instrument_ids=ids or {-1}, kinds=tiers):
        if p["instrument_id"] is None:
            unknown += 1
            continue
        digest.update(struct.pack("<qq", p["seq"], p["instrument_id"]) + p["raw"])
        kind, rec = enc_tick(p, p["instrument_id"], p["recv_ns"])
        t = p["recv_ns"]
        if last_t is not None and t < last_t:
            monotonic = False
        last_t = t
        t_lo = t if t_lo is None else min(t_lo, t)
        t_hi = t if t_hi is None else max(t_hi, t)
        out.append(((t, 4, p["seq"], p["instrument_id"]), rec))
    b.events = []
    _refdata_events(b, ids, day, learned)
    _settle_events(b, ids, day)
    if b.spec.get("include_gaps", True):
        _gap_events(b, tick_day, ids)
    side = sorted(b.events + _clock_events(b, t_lo, t_hi))
    b.sources.append({"day": day.isoformat(), "source": tick_day.source, "instruments": len(ids),
                      "packets_digest": digest.hexdigest(), "unmapped_packets": unknown})
    ticks_sorted = out if monotonic else sorted(out)
    return heapq.merge(ticks_sorted, side), t_lo, t_hi


def _fo_units(day: date, iid: int, contracts, row_lot):  # noqa: ANN001, ANN202
    """An `fo_eod` row's traded volume in units (D-79).

    NSE's F&O daily file gives volume in contracts (lots) and `fo_eod` keeps it as published; a bar's volume is in
    units for every source, so it is converted here, once: contracts × lot. The lot is the row's own `lot_size`
    or, where that is NULL (old-format files), the dated contract spec in force on the trade date. No lot from
    either fails the build: never a guessed lot, never contracts passed off as units. The product is taken in
    Python (SQLite would turn an overflowing one into a float) and must fit an int64."""
    from stockify.data import instruments

    def fail(why: str) -> PackError:
        info = instruments.get(iid) or {}
        return PackError(f"fo_eod {day.isoformat()} instrument {iid} "
                         f"({info.get('exchange')}:{info.get('tradingsymbol')}): {why}")

    lot, source = row_lot, "fo_eod.lot_size"
    if lot is None:
        lot, source = (instruments.spec_at(iid, day) or {}).get("lot_size"), "contract_specs"
        if lot is None:
            raise fail("no lot size in the row and no contract spec in force on that date, so its volume in "
                       "contracts cannot be turned into units (D-79)")
    if type(lot) is not int or lot < 1:
        raise fail(f"lot size {lot!r} ({source}) is not a positive whole number")
    if contracts is None:
        return None
    if type(contracts) is not int:
        raise fail(f"volume {contracts!r} is not a whole number of contracts")
    units = contracts * lot
    if not numerics.INT64_MIN <= units <= numerics.INT64_MAX:
        raise fail(f"volume {contracts} contracts × lot {lot} does not fit an int64")
    return units


def _table_events(b: _Build, start: date, end: date):  # noqa: ANN202
    """bars / bhav_eod: every record in memory (one row per instrument per bar)."""
    from stockify.data import calendar, history, refdata
    spec = b.spec
    pricing = _pricing()
    rows_digest = hashlib.sha256()
    all_events = []
    for day in calendar.trading_days("FO", start, end):
        ids = resolve(spec.get("instruments"), day)
        b.events = []
        _refdata_events(b, ids, day)
        _settle_events(b, ids, day)
        all_events += b.events
        d = day.isoformat()
        if spec["dataset"] == "bhav_eod":
            t = _ist(day, pricing["eod_publication_ist"])
            conn = refdata._connect()
            marks = ",".join("?" * len(ids)) or "NULL"
            # fo_eod.volume is in contracts and is turned into units below (D-79); oi is units already
            q = {"fo_eod": "SELECT instrument_id, open_paise, high_paise, low_paise, close_paise, volume, oi, lot_size "
                           "FROM fo_eod",
                 "cm_eod": "SELECT instrument_id, open_paise, high_paise, low_paise, close_paise, volume, NULL FROM cm_eod",
                 "index_eod": "SELECT instrument_id, open_paise, high_paise, low_paise, close_paise, NULL, NULL FROM index_eod"}
            for table, sql in q.items():
                extra = " AND series='EQ'" if table == "cm_eod" else ""
                for r in conn.execute(f"{sql} WHERE trade_date=? AND instrument_id IN ({marks}){extra} "
                                      "ORDER BY instrument_id", (d, *sorted(ids))):
                    if table == "fo_eod":
                        r = (*r[:5], _fo_units(day, r[0], r[5], r[7]), r[6])
                    rows_digest.update(numerics.dumps_canonical([table, d, *r]).encode())
                    all_events.append(((t, 4, len(all_events), r[0]),
                                       enc_bar(r[0], t, 86400, _ist(day, "00:00"), *r[1:])))
    if spec["dataset"] == "bars":
        interval = spec.get("interval")
        if interval not in INTERVAL_S:
            raise PackError(f"bars pack needs an interval from {', '.join(INTERVAL_S)}")
        isec = INTERVAL_S[interval]
        ids = set()
        for day in calendar.trading_days("FO", start, end):
            ids |= resolve(spec.get("instruments"), day)
        lo, hi = _ist(start, "00:00"), _ist(end, "23:59") + 59 * SEC
        rows = [(iid, r) for iid in sorted(ids) for r in history.bars(iid, interval, lo, hi)]
        for n, (iid, r) in enumerate(rows):     # source seq = row order by (instrument_id, start)
            rows_digest.update(numerics.dumps_canonical([iid, r["ts_ns"], r["close_paise"], r["fetched_on"]]).encode())
            t = r["ts_ns"] + isec * SEC
            all_events.append(((t, 4, n, iid), enc_bar(iid, t, isec, r["ts_ns"], r["open_paise"], r["high_paise"],
                                                     r["low_paise"], r["close_paise"], r["volume"], r["oi"])))
    b.sources.append({"dataset": spec["dataset"], "rows_digest": rows_digest.hexdigest()})
    ts = [k[0] for k, _ in all_events]
    t_lo, t_hi = (min(ts), max(ts)) if ts else (None, None)
    return sorted(all_events + _clock_events(b, t_lo, t_hi)), t_lo, t_hi


def pack_path(sha: str) -> Path:
    return paths.packs_dir() / f"{sha}.evt"


def manifest_path(sha: str) -> Path:
    return paths.packs_dir() / f"{sha}.json"


def _normalise(spec: dict) -> dict:
    spec = json.loads(json.dumps(spec))
    if spec.get("dataset") not in ("ticks", "bars", "bhav_eod"):
        raise PackError("dataset must be ticks, bars or bhav_eod")
    for k in ("from", "to"):
        date.fromisoformat(spec[k])
    if spec["from"] > spec["to"]:
        raise PackError("from is after to")
    return spec


# Temporary files a builder writes (pid and a random part in the name): a builder that died leaves them.
_TMP_NAMES = (re.compile(r"\.building-(\d+)-[0-9a-f]{12}\.evt"),
              re.compile(r"\.[0-9a-f]{64}\.json\.(\d+)-[0-9a-f]{12}\.tmp"))
STALE_AFTER_S = 86_400


def clean_stale_tmp(max_age_s: int = STALE_AFTER_S) -> list[str]:
    """Remove builder temp files older than `max_age_s` whose process no longer exists; returns their names.
    A temp file whose pid is alive is kept (even if the pid was reused): it may belong to a running build."""
    from stockify import proc, timeutil
    now_s = timeutil.now_ns() / 1e9
    removed = []
    for f in paths.packs_dir().iterdir():
        m = next((r.fullmatch(f.name) for r in _TMP_NAMES if r.fullmatch(f.name)), None)
        if m is None:
            continue
        try:
            if now_s - f.stat().st_mtime < max_age_s or proc.is_running(int(m.group(1))):
                continue
            f.unlink()
            removed.append(f.name)
        except OSError:
            continue                     # gone already, or in use: leave it
    return removed


def build(spec: dict) -> str:
    """Write the pack and its manifest; return the pack's SHA-256."""
    try:
        clean_stale_tmp()
    except OSError:
        pass
    spec = _normalise(spec)
    start, end = date.fromisoformat(spec["from"]), date.fromisoformat(spec["to"])
    basis = "adjusted" if spec["dataset"] == "bars" else "raw"
    b = _Build(spec, basis)
    flags = FLAG_CLOCK if int(spec.get("clock_cadence_ms", 0)) > 0 else 0
    folder = paths.packs_dir()
    tmp = folder / f".building-{os.getpid()}-{secrets.token_hex(6)}.evt"   # unique per builder, threads too
    count, t_min, t_max = 0, None, None
    sha = hashlib.sha256()
    try:
        with tmp.open("wb") as f:
            f.write(b"\0" * 64)
            if spec["dataset"] == "ticks":
                from stockify.data import calendar
                days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

                def day_streams():                               # one day in memory at a time
                    for day in days:
                        if not calendar.is_trading_day("FO", day) and _no_recording(day):
                            continue
                        res = _ticks_day(b, day)
                        if res is not None:
                            yield from res[0]                    # days do not overlap in time
                stream = day_streams()
            else:
                stream, _, _ = _table_events(b, start, end)
                stream = iter(stream)
            last = None
            for key, rec in stream:
                if last is not None and key[:2] < last[:2]:
                    raise PackError(f"internal ordering error at {key}")
                last = key
                count += 1
                f.write(_with_seq(rec, count))
                name = KIND_NAMES[struct.unpack_from("<H", rec)[0]]
                b.counts[name] = b.counts.get(name, 0) + 1
                t_min = key[0] if t_min is None else min(t_min, key[0])
                t_max = key[0] if t_max is None else max(t_max, key[0])
            f.seek(0)
            f.write(HEADER.pack(MAGIC, VERSION, flags, 64, count, t_min or 0, t_max or 0, BASIS[basis]))
        with tmp.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                sha.update(chunk)
        digest = sha.hexdigest()
        target = pack_path(digest)
        _publish(tmp, target, digest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    counts = dict(sorted(b.counts.items()))
    manifest = {"sha256": digest, "format": "evt/1", "spec": spec, "price_basis": basis, "records": count,
                "counts_by_kind": counts, "t_min": t_min, "t_max": t_max, "sources": b.sources,
                "bytes": _refused_retry(lambda p: p.stat().st_size, target)}
    _publish_text(manifest_path(digest), json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return digest


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Windows refuses a replace, or an open, while another process has the file open or is replacing it at that
# moment. Those refusals pass in milliseconds, so publishing retries a few times before it decides.
_RETRIES = 8
_BACKOFF_S = 0.02            # doubled each try, capped at 0.5 s: about 3 s in all


def _pause(attempt: int) -> None:
    import time
    time.sleep(min(_BACKOFF_S * 2 ** attempt, 0.5))


def _publish(tmp: Path, target: Path, digest: str) -> None:
    """Move a finished pack into place. Packs are content-addressed, so a target that already exists holds the
    same bytes: keep it. The move is os.rename, which refuses an existing target (CB 4.6): a published pack is
    never replaced, so no reader ever meets a replace window. Two builders of one spec can race (both saw no
    target); the loser's rename is refused, and the existing target is accepted only if its hash matches; a
    different file re-raises at once."""
    if target.exists():
        tmp.unlink()
        return
    error: OSError | None = None
    for attempt in range(_RETRIES):
        try:
            os.rename(tmp, target)
            return
        except (PermissionError, FileExistsError) as e:
            error = e
        try:
            existing = _sha_file(target) if target.exists() else None
        except (PermissionError, FileNotFoundError):
            existing = None                              # mid-replace by another builder: look again
        if existing == digest:
            tmp.unlink(missing_ok=True)
            return
        if existing is not None:
            raise error                                  # someone else's bytes under this name: never replace
        _pause(attempt)
    raise error  # type: ignore[misc]


# Manifest fields that depend on when a pack was built rather than on its content. There are none today; the
# comparison still ignores such fields so that adding one later cannot turn a harmless race into a failure.
_WALL_CLOCK_FIELDS = frozenset({"created_ns", "built_ns", "duration_ms", "pid", "host"})
# Provenance: where the records were read from (landing or archive, P-05). The same pack can be rebuilt from
# another source (after retention moves a day to the archive); the manifest then follows its latest build.
_PROVENANCE_FIELDS = frozenset({"sources"})


def _same_manifest(existing: str, ours: str, ignore: frozenset[str] = _WALL_CLOCK_FIELDS) -> bool | None:
    """True: same content; False: different content; None: not readable as a manifest yet (look again)."""
    try:
        a, b = json.loads(existing), json.loads(ours)
    except json.JSONDecodeError:
        return None
    strip = lambda m: {k: v for k, v in m.items() if k not in ignore}  # noqa: E731
    return isinstance(a, dict) and strip(a) == strip(b)


def _publish_text(target: Path, text: str) -> None:
    """Write a manifest atomically: a unique tmp, then os.rename, which refuses an existing target (CB 4.6).
    When it is refused, an existing manifest with the same content is kept as it is (a racing builder, or a
    rebuild), so a reader meets no replace window. One that differs only in provenance (`sources`) is refreshed
    by a replace, retried while refused. Any other difference re-raises at once: it is never overwritten."""
    tmp = target.with_name(f".{target.name}.{os.getpid()}-{secrets.token_hex(6)}.tmp")
    _refused_retry(lambda p: p.write_text(text, encoding="utf-8"), tmp)
    error: OSError | None = None
    try:
        for attempt in range(_RETRIES):
            try:
                os.rename(tmp, target)
                return
            except (PermissionError, FileExistsError) as e:
                error = e
            try:
                existing = target.read_text(encoding="utf-8")
            except (PermissionError, FileNotFoundError):
                existing = None                          # mid-replace by another builder: look again
            if existing is not None:
                verdict = _same_manifest(existing, text)
                if verdict:
                    return
                if verdict is False:
                    if not _same_manifest(existing, text, _WALL_CLOCK_FIELDS | _PROVENANCE_FIELDS):
                        raise error                      # a complete manifest with other content: a conflict
                    try:
                        tmp.replace(target)              # same pack read from elsewhere: follow the latest build
                        return
                    except PermissionError as e:
                        error = e
            _pause(attempt)
        raise error  # type: ignore[misc]
    finally:
        tmp.unlink(missing_ok=True)


def _no_recording(day: date) -> bool:
    from stockify.data import ticks
    return not ticks.has_day(day)


def _refused_retry(op, path: Path):  # noqa: ANN001, ANN202
    """Run `op(path)`, retrying a refused open (PermissionError) with the publishing back-off. A pack or manifest
    can be refused for a moment while another process replaces it (pack-race form 3); a refusal that outlasts
    the retries re-raises. A missing file is not retried: it is simply not built."""
    for attempt in range(_RETRIES):
        try:
            return op(path)
        except PermissionError:
            if attempt == _RETRIES - 1:
                raise
            _pause(attempt)
    raise AssertionError("unreachable")


def read_bytes(path) -> bytes:  # noqa: ANN001
    """A pack's bytes, through the refused-open retry."""
    return _refused_retry(lambda p: p.read_bytes(), Path(path))


def sha_file(path) -> str:  # noqa: ANN001
    """A pack's SHA-256, through the refused-open retry."""
    return _refused_retry(_sha_file, Path(path))


def manifest(sha: str) -> dict:
    """A pack's manifest, through the refused-open retry."""
    return json.loads(_refused_retry(lambda p: p.read_text(encoding="utf-8"), manifest_path(sha)))
