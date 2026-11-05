"""Data-quality report for a recorded day (M1.9).

Reads the day through `stockify.data.ticks` (archive first) and computes, per
instrument, over the seconds the instrument was **subscribed** during its
group's **CONTINUOUS** phase:

| metric                 | meaning                                                      |
|------------------------|--------------------------------------------------------------|
| packets                | packets in that window                                       |
| coverage_pct           | % of seconds with at least one packet                        |
| longest_gap_s          | longest run of seconds without a packet                      |
| crossed_books          | FULL packets whose best bid >= best ask (both sides present) |
| volume_decreases       | packets whose day volume is below the previous packet's      |
| exch_ts_out_of_order   | packets whose exchange timestamp went backwards              |
| latency_p50/p95/p99_ms | receive time minus exchange timestamp (includes clock offset)|

Day-level rows use instrument_id 0: instruments, packets, total gap seconds per
reason, and silent_minutes (minutes of the F&O continuous phase with neither a
packet nor a heartbeat). Rows go to refdata.sqlite → data_quality (replaced on
each run) and a Markdown summary to $LAB/reports/quality_YYYYMMDD.md.
"""
from __future__ import annotations

import logging
from array import array
from dataclasses import dataclass, field
from datetime import date

from stockify import paths
from stockify.daemon import kite_packets as kp
from stockify.data import calendar, instruments, refdata, ticks

log = logging.getLogger("stockify.data.quality")
SEC = 1_000_000_000
MIN = 60 * SEC
FOREIGN_METRICS = ("fsp_vs_close_paise", "fsp_settle_spread_paise")    # written by stockify.data.nse_files.settle
GROUP_OF_KIND = {"INDEX": "CM", "EQ": "CM", "FUT": "FO_FUT", "CE": "FO_OPT", "PE": "FO_OPT"}


class QualityError(RuntimeError):
    pass


@dataclass
class _Inst:
    start_s: int                  # first second of the continuous window
    seconds: int                  # window length
    subscribed: bytearray         # 1 per second while subscribed
    seen: bytearray               # 1 per second with a packet
    packets: int = 0
    crossed: int = 0
    vol_down: int = 0
    ts_back: int = 0
    last_vol: int | None = None
    last_exch: int | None = None
    latency_ms: array = field(default_factory=lambda: array("d"))


def _percentile(values: array, q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _continuous(group: str, day: date) -> tuple[int, int] | None:
    for ph in calendar.phases(group, day):
        if ph.phase == "CONTINUOUS":
            return ph.start_ns, ph.end_ns
    return None


def _sub_intervals(subs: list[dict]) -> dict[int, list[tuple[int, int]]]:
    """instrument_id -> [(from_ns, to_ns)] while subscribed (open end = None)."""
    out: dict[int, list[list]] = {}
    for row in sorted(subs, key=lambda r: r["recv_ns"] or 0):
        iid = row["instrument_id"]
        if iid is None:
            continue
        spans = out.setdefault(iid, [])
        if row["action"] == "subscribe":
            if not spans or spans[-1][1] is not None:
                spans.append([row["recv_ns"], None])
        elif row["action"] == "unsubscribe" and spans and spans[-1][1] is None:
            spans[-1][1] = row["recv_ns"]
    return {k: [(a, b) for a, b in v] for k, v in out.items()}


def report(day: date, *, write: bool = True) -> dict:
    tick_day = ticks.open_day(day)
    if tick_day is None:
        raise QualityError(f"no recording for {day}")
    spans = _sub_intervals(tick_day.subscriptions())
    insts: dict[int, _Inst] = {}

    def state(iid: int) -> _Inst | None:
        if iid in insts:
            return insts[iid]
        info = instruments.get(iid) or {}
        window = _continuous(GROUP_OF_KIND.get(info.get("kind"), "CM"), day)
        if window is None:
            return None
        start_s, end_s = window[0] // SEC, window[1] // SEC
        n = end_s - start_s
        sub = bytearray(n)
        if iid not in spans:                 # no subscription log (older recordings): whole window
            sub = bytearray([1]) * n
        for a, b in spans.get(iid, []):
            lo = max(0, a // SEC - start_s)
            hi = min(n, (b // SEC if b else end_s) - start_s)
            for i in range(lo, hi):
                sub[i] = 1
        insts[iid] = _Inst(start_s, n, sub, bytearray(n))
        return insts[iid]

    active_minutes: set[int] = set()
    cols = ["instrument_id", "recv_ns", "pkt_len", "volume", "exch_ts_s", "raw"]
    for p in tick_day.packets(cols):
        active_minutes.add(p["recv_ns"] // MIN)
        iid = p["instrument_id"]
        if iid is None:
            continue
        st = state(iid)
        if st is None:
            continue
        i = p["recv_ns"] // SEC - st.start_s
        if not (0 <= i < st.seconds and st.subscribed[i]):
            continue
        st.packets += 1
        st.seen[i] = 1
        if p["pkt_len"] == 184:
            depth = kp.parse_depth(p["raw"])
            bid, ask = depth[0][1], depth[5][1]
            if bid and ask and bid >= ask:
                st.crossed += 1
        if p["volume"] is not None:
            if st.last_vol is not None and p["volume"] < st.last_vol:
                st.vol_down += 1
            st.last_vol = p["volume"]
        if p["exch_ts_s"]:
            if st.last_exch is not None and p["exch_ts_s"] < st.last_exch:
                st.ts_back += 1
            st.last_exch = p["exch_ts_s"]
            st.latency_ms.append((p["recv_ns"] - p["exch_ts_s"] * SEC) / 1e6)

    for minute_ns, count in tick_day.heartbeats():
        if count:
            active_minutes.add(minute_ns // MIN)

    rows: list[tuple] = []
    per_inst = []
    for iid, st in sorted(insts.items()):
        sub_s = sum(st.subscribed)
        covered = sum(1 for i in range(st.seconds) if st.subscribed[i] and st.seen[i])
        longest = run = 0
        for i in range(st.seconds):
            if st.subscribed[i] and not st.seen[i]:
                run += 1
                longest = max(longest, run)
            else:
                run = 0
        m = {"packets": st.packets,
             "coverage_pct": round(100.0 * covered / sub_s, 3) if sub_s else None,
             "longest_gap_s": longest, "crossed_books": st.crossed, "volume_decreases": st.vol_down,
             "exch_ts_out_of_order": st.ts_back,
             "latency_p50_ms": _percentile(st.latency_ms, 0.50),
             "latency_p95_ms": _percentile(st.latency_ms, 0.95),
             "latency_p99_ms": _percentile(st.latency_ms, 0.99)}
        per_inst.append((iid, m))
        rows += [(day.isoformat(), iid, k, v, None) for k, v in m.items() if v is not None]

    gap_s: dict[str, float] = {}
    for g in tick_day.gaps():
        if g["start_ns"] is not None and g["end_ns"] is not None:
            gap_s[g["reason"]] = gap_s.get(g["reason"], 0) + (g["end_ns"] - g["start_ns"]) / SEC
    fo = _continuous("FO_OPT", day)
    silent = 0
    if fo and any(insts.values()):
        first_sub = min((a for v in spans.values() for a, _ in v), default=fo[0])
        last_sub = max(((b or fo[1]) for v in spans.values() for _, b in v), default=fo[1])
        lo, hi = max(fo[0], first_sub) // MIN, min(fo[1], last_sub) // MIN
        silent = sum(1 for m in range(lo, hi) if m not in active_minutes)
    day_metrics = {"instruments": len(insts), "packets": sum(st.packets for st in insts.values()),
                   "silent_minutes": silent, **{f"gap_s_{k}": round(v, 3) for k, v in gap_s.items()}}
    rows += [(day.isoformat(), 0, k, v, None) for k, v in day_metrics.items()]

    result = {"date": day.isoformat(), "source": tick_day.source, "day": day_metrics,
              "instruments": {iid: m for iid, m in per_inst}}
    if write:
        with refdata.transaction() as conn:
            # Only this report's own rows: other checks (e.g. the FSP cross-check, M1.3) share the table.
            conn.execute("DELETE FROM data_quality WHERE trade_date=? AND metric NOT IN (%s)"
                         % ",".join("?" * len(FOREIGN_METRICS)), (day.isoformat(), *FOREIGN_METRICS))
            conn.executemany("INSERT INTO data_quality (trade_date, instrument_id, metric, value, detail) "
                             "VALUES (?, ?, ?, ?, ?)", rows)
        result["summary_path"] = str(write_summary(result))
    return result


def write_summary(result: dict):
    folder = paths.lab_dir() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"quality_{result['date'].replace('-', '')}.md"
    path.write_text(summary_markdown(result), encoding="utf-8")
    return path


def _name(iid: int) -> str:
    info = instruments.get(iid) or {}
    return info.get("tradingsymbol") or f"{info.get('underlying')} {info.get('kind')}" or str(iid)


def summary_markdown(result: dict) -> str:
    d, insts = result["day"], result["instruments"]
    lat = [m["latency_p50_ms"] for m in insts.values() if m.get("latency_p50_ms") is not None]
    lines = [f"# Data quality {result['date']} ({result['source']})", "",
             f"- Instruments: {d['instruments']}, packets in the continuous phase: {d['packets']:,}",
             f"- Silent minutes (no packet, no heartbeat): {d['silent_minutes']}"]
    for k, v in sorted(d.items()):
        if k.startswith("gap_s_"):
            lines.append(f"- Gap time, {k[6:]}: {v:,.0f} s")
    if lat:
        lines.append(f"- Median receive-minus-exchange latency across instruments: {sorted(lat)[len(lat) // 2]:,.0f} ms")
    totals = {k: sum(m.get(k) or 0 for m in insts.values())
              for k in ("crossed_books", "volume_decreases", "exch_ts_out_of_order")}
    lines.append(f"- Crossed books: {totals['crossed_books']}, volume decreases: {totals['volume_decreases']}, "
                 f"out-of-order exchange timestamps: {totals['exch_ts_out_of_order']}")
    lines += ["", "## Lowest coverage", "", "| instrument | coverage % | longest gap s | packets |", "|---|---|---|---|"]
    ranked = sorted(((m.get("coverage_pct") if m.get("coverage_pct") is not None else 101.0, iid, m)
                     for iid, m in insts.items()))[:10]
    for cov, iid, m in ranked:
        lines.append(f"| {_name(iid)} | {cov:.1f} | {m['longest_gap_s']} | {m['packets']:,} |")
    return "\n".join(lines) + "\n"


def stored(day: date) -> dict:
    """data_quality rows for a day, as {instrument_id: {metric: value}}."""
    out: dict[int, dict] = {}
    for r in refdata._connect().execute(
            "SELECT instrument_id, metric, value FROM data_quality WHERE trade_date=?", (day.isoformat(),)):
        out.setdefault(r["instrument_id"], {})[r["metric"]] = r["value"]
    return out

