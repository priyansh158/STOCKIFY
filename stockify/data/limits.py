"""Price bands (circuit limits) snapshot into `refdata.sqlite` → `daily_limits` (M1.5).

The recorder snapshots the FULL-tier tradable instruments through Kite's quote
endpoint (`upper_circuit_limit`, `lower_circuit_limit`), at most
`quote_rps` requests per second and `quote_max_instruments` per call (K-8).
Options are refreshed hourly (their bands move during the day); other
instruments once a day, plus any FULL instrument added later (re-centring).
Rows are append-only snapshots keyed by `snapshot_ns`; packs turn them into
`REFDATA price_band` records (M1.8).
"""
from __future__ import annotations

import logging
from datetime import date

from stockify.data import history, instruments, refdata

log = logging.getLogger("stockify.data.limits")
SEC = 1_000_000_000
HOUR = 3600 * SEC


class LimitsError(RuntimeError):
    pass


def _key(info: dict) -> str | None:
    if not info.get("tradingsymbol") or info.get("kind") in ("INDEX", "FUT_CONT"):
        return None
    return f"{info['exchange']}:{info['tradingsymbol']}"


def snapshot(day: date, instrument_ids: list[int], *, client, clock, bucket=None) -> int:  # noqa: ANN001
    """Fetch and store one snapshot for the instruments; returns rows written."""
    cfg = history.limits()
    bucket = bucket or history.TokenBucket(cfg["quote_rps"], clock)
    keys: dict[str, int] = {}
    for iid in sorted(set(instrument_ids)):
        k = _key(instruments.get(iid) or {})
        if k:
            keys[k] = iid
    names = sorted(keys)
    per_call = int(cfg["quote_max_instruments"])
    rows = []
    for i in range(0, len(names), per_call):
        batch = names[i:i + per_call]
        bucket.acquire()
        quotes = client.quote(batch) or {}
        now = clock.now_ns()
        for name in batch:
            q = quotes.get(name)
            if not q or q.get("upper_circuit_limit") is None or q.get("lower_circuit_limit") is None:
                continue
            upper, lower = history._paise(q["upper_circuit_limit"]), history._paise(q["lower_circuit_limit"])
            if upper <= 0 and lower <= 0:
                continue                                    # no band published
            rows.append((day.isoformat(), keys[name], now, upper, lower, "kite_quote"))
    with refdata.transaction() as conn:
        conn.executemany("INSERT OR REPLACE INTO daily_limits (trade_date, instrument_id, snapshot_ns, "
                         "upper_paise, lower_paise, source) VALUES (?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


class Snapshotter:
    """Decides what to snapshot when; the recorder loop calls `tick()` once a second."""

    RETRY_NS = 5 * 60 * SEC

    def __init__(self, day: date, manager, *, client=None, clock):  # noqa: ANN001
        if client is None:                      # Kite's read-only view: quotes only, no order route (LCB 5.1)
            from stockify import kite_readonly
            client = kite_readonly.client()
        self.day, self.manager, self.client, self.clock = day, manager, client, clock
        self.bucket = history.TokenBucket(history.limits()["quote_rps"], clock)
        self.done: dict[int, int] = {}        # instrument_id -> last snapshot ns
        self.next_try_ns = 0

    def due(self, now: int) -> list[int]:
        out = []
        for iid, sub in self.manager.current.items():
            if sub.mode != "full":
                continue
            last = self.done.get(iid)
            if last is None:
                out.append(iid)
                continue
            info = instruments.get(iid) or {}
            if info.get("kind") in ("CE", "PE") and now - last >= HOUR:
                out.append(iid)
        return out

    def tick(self) -> int:
        now = self.clock.now_ns()
        if now < self.next_try_ns:
            return 0
        ids = self.due(now)
        if not ids:
            return 0
        try:
            n = snapshot(self.day, ids, client=self.client, clock=self.clock, bucket=self.bucket)
        except Exception as e:                    # token trouble, network: try again in 5 minutes
            log.warning("price-band snapshot failed (%s); retrying in 5 min", e)
            self.next_try_ns = now + self.RETRY_NS
            return 0
        for iid in ids:
            self.done[iid] = now
        log.info("price bands: %d of %d instruments snapshotted", n, len(ids))
        return n
