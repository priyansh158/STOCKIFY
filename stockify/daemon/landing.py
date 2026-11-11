"""One day's landing file: `ticks/landing/ticks_YYYYMMDD.sqlite` (A1 §3.1).

Written only by the recorder's writer thread; read by compaction, the replay
ticker and the quality report. WAL with `synchronous=NORMAL`, schema from the
`landing` migrations. Recorded days are irreplaceable (I-13): nothing here
deletes or rewrites packet rows.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import date
from pathlib import Path

from stockify import paths
from stockify.data import migrate

PACKET_COLUMNS = ("seq", "frame_no", "recv_ns", "mono_ns", "instrument_id", "instrument_token",
                  "mode", "pkt_len", "ltp", "last_qty", "atp", "volume", "buy_qty", "sell_qty",
                  "open", "high", "low", "close", "last_trade_ts_s", "oi", "oi_high", "oi_low",
                  "exch_ts_s", "raw")
_INSERT = (f"INSERT INTO packets ({', '.join(PACKET_COLUMNS)}) "
           f"VALUES ({', '.join('?' * len(PACKET_COLUMNS))})")


class LandingDB:
    def __init__(self, day: date, path: Path | None = None, *, readonly: bool = False):
        self.day = day
        self.path = path or paths.ticks_landing_file(day)
        if readonly:
            self.conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True,
                                        check_same_thread=False, isolation_level=None)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), timeout=10.0, check_same_thread=False,
                                        isolation_level=None)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            migrate.apply(self.conn, "landing")
        self.conn.row_factory = sqlite3.Row
        # The writer thread and the session's health/ticker threads share this connection.
        self.lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    # ── counters for restart continuation ────────────────────────────────────
    def max_seq(self) -> int:
        return int(self.conn.execute("SELECT IFNULL(MAX(seq), 0) FROM packets").fetchone()[0])

    def max_frame_no(self) -> int:
        return int(self.conn.execute("SELECT IFNULL(MAX(frame_no), 0) FROM packets").fetchone()[0])

    # ── writes (one transaction per batch) ───────────────────────────────────
    def write_batch(self, rows: list[tuple], heartbeats: dict[int, int], meta: dict[str, str]) -> None:
        with self.lock:
            c = self.conn
            c.execute("BEGIN")
            try:
                if rows:
                    c.executemany(_INSERT, rows)
                for minute_ns, n in heartbeats.items():
                    c.execute("INSERT INTO heartbeats (minute_ns, count) VALUES (?, ?) "
                              "ON CONFLICT (minute_ns) DO UPDATE SET count = count + excluded.count",
                              (minute_ns, n))
                for k, v in meta.items():
                    c.execute("INSERT INTO recorder_meta (key, value) VALUES (?, ?) "
                              "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (k, str(v)))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def set_meta(self, **items) -> None:
        self.write_batch([], {}, {k: str(v) for k, v in items.items()})

    def meta(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM recorder_meta")}

    def open_gap(self, start_ns: int, reason: str, scope: str = "all") -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO gaps (start_ns, end_ns, reason, scope) VALUES (?, NULL, ?, ?)",
                                    (start_ns, reason, scope))
            return int(cur.lastrowid)

    def close_gap(self, gap_id: int, end_ns: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE gaps SET end_ns = ? WHERE gap_id = ? AND end_ns IS NULL", (end_ns, gap_id))

    def add_gap(self, start_ns: int, end_ns: int, reason: str, scope: str = "all") -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO gaps (start_ns, end_ns, reason, scope) VALUES (?, ?, ?, ?)",
                                    (start_ns, end_ns, reason, scope))
            return int(cur.lastrowid)

    def add_dropped_frames(self, runs: list[tuple[int, int, int, int]]) -> None:
        """Durable record of frames the writer queue refused (RM 5.3): one `queue_full` gap per run of consecutive
        frame numbers, `(first_frame_no, last_frame_no, first_recv_ns, last_recv_ns)` each. The gap spans the receive
        times of the first and last dropped frame, and names the frames so a parity report knows what is missing."""
        with self.lock:
            self.conn.executemany(
                "INSERT INTO gaps (start_ns, end_ns, reason, scope, first_frame_no, last_frame_no, frames) "
                "VALUES (?, ?, 'queue_full', 'all', ?, ?, ?)",
                [(t0, t1, a, b, b - a + 1) for a, b, t0, t1 in runs])

    def log_subscriptions(self, rows: list[tuple]) -> None:
        """rows: (recv_ns, action, instrument_id, instrument_token, mode, reason)."""
        with self.lock:
            self.conn.executemany(
                "INSERT INTO subscription_log (recv_ns, action, instrument_id, instrument_token, mode, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows)

    # ── reads ────────────────────────────────────────────────────────────────
    def gaps(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM gaps ORDER BY gap_id")]

    def packet_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0])
