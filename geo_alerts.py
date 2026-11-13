"""SQLite store for geo news monitor: seen articles + disruption alerts.

Lives beside other STOCKIFY local state:
  %LOCALAPPDATA%/STOCKIFY/kite/geo_alerts.sqlite
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import stockify_paths
from stockify import dbconn

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_articles (
    guid TEXT PRIMARY KEY,
    title TEXT,
    url TEXT,
    source TEXT,
    published_at TEXT,
    first_seen_at INTEGER NOT NULL,
    query TEXT
);

CREATE TABLE IF NOT EXISTS geo_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    severity TEXT NOT NULL,
    disruption_score INTEGER NOT NULL,
    scope TEXT,
    title TEXT NOT NULL,
    source TEXT,
    url TEXT,
    published_at TEXT,
    guid TEXT,
    why_disruptive TEXT,
    investor_action TEXT,
    confidence REAL,
    affected_tickers_json TEXT,
    article_json TEXT,
    emailed_at INTEGER,
    acknowledged_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_geo_alerts_created ON geo_alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_geo_alerts_severity ON geo_alerts(severity);
CREATE UNIQUE INDEX IF NOT EXISTS idx_geo_alerts_guid ON geo_alerts(guid);
"""


def _db_path():
    return stockify_paths.state_dir() / "geo_alerts.sqlite"


def _open() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


_pool = dbconn.Pool(__name__, _open, _init)


def _connect() -> sqlite3.Connection:
    """This thread's connection (D-143); writers still take `_lock`."""
    return _pool.get()


def init_db() -> None:
    _connect()


def mark_seen(
    *,
    guid: str,
    title: str = "",
    url: str = "",
    source: str = "",
    published_at: str = "",
    query: str = "",
) -> bool:
    """Insert seen row. Returns True if newly seen, False if already known."""
    if not guid:
        return False
    conn = _connect()
    with dbconn.locked_write(_lock, conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO seen_articles "
            "(guid, title, url, source, published_at, first_seen_at, query) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guid, title, url, source, published_at, int(time.time()), query),
        )
        conn.commit()
        return cur.rowcount > 0


def is_seen(guid: str) -> bool:
    if not guid:
        return False
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM seen_articles WHERE guid = ? LIMIT 1", (guid,)
    ).fetchone()
    return row is not None


@dataclass
class GeoAlert:
    id: int
    created_at: int
    severity: str
    disruption_score: int
    scope: str
    title: str
    source: str
    url: str
    published_at: str
    guid: str
    why_disruptive: str
    investor_action: str
    confidence: float
    affected_tickers: list[str]
    article: dict[str, Any]
    emailed_at: Optional[int]
    acknowledged_at: Optional[int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "severity": self.severity,
            "disruption_score": self.disruption_score,
            "scope": self.scope,
            "title": self.title,
            "source": self.source,
            "url": self.url,
            "published_at": self.published_at,
            "guid": self.guid,
            "why_disruptive": self.why_disruptive,
            "investor_action": self.investor_action,
            "confidence": self.confidence,
            "affected_tickers": self.affected_tickers,
            "article": self.article,
            "emailed_at": self.emailed_at,
            "acknowledged_at": self.acknowledged_at,
        }


def _row_to_alert(row: sqlite3.Row) -> GeoAlert:
    tickers: list[str] = []
    try:
        tickers = json.loads(row["affected_tickers_json"] or "[]")
    except json.JSONDecodeError:
        tickers = []
    article: dict[str, Any] = {}
    try:
        article = json.loads(row["article_json"] or "{}")
    except json.JSONDecodeError:
        article = {}
    return GeoAlert(
        id=int(row["id"]),
        created_at=int(row["created_at"]),
        severity=row["severity"] or "",
        disruption_score=int(row["disruption_score"] or 0),
        scope=row["scope"] or "",
        title=row["title"] or "",
        source=row["source"] or "",
        url=row["url"] or "",
        published_at=row["published_at"] or "",
        guid=row["guid"] or "",
        why_disruptive=row["why_disruptive"] or "",
        investor_action=row["investor_action"] or "",
        confidence=float(row["confidence"] or 0),
        affected_tickers=tickers if isinstance(tickers, list) else [],
        article=article if isinstance(article, dict) else {},
        emailed_at=row["emailed_at"],
        acknowledged_at=row["acknowledged_at"],
    )


def save_alert(
    *,
    severity: str,
    disruption_score: int,
    scope: str,
    title: str,
    source: str,
    url: str,
    published_at: str,
    guid: str,
    why_disruptive: str,
    investor_action: str,
    confidence: float,
    affected_tickers: list[str],
    article: dict[str, Any],
) -> Optional[int]:
    """Insert alert. Returns id, or None if guid already alerted."""
    if not guid:
        return None
    conn = _connect()
    with dbconn.locked_write(_lock, conn):
        try:
            cur = conn.execute(
                "INSERT INTO geo_alerts ("
                "created_at, severity, disruption_score, scope, title, source, url, "
                "published_at, guid, why_disruptive, investor_action, confidence, "
                "affected_tickers_json, article_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    severity,
                    int(disruption_score),
                    scope,
                    title,
                    source,
                    url,
                    published_at,
                    guid,
                    why_disruptive,
                    investor_action,
                    float(confidence),
                    json.dumps(affected_tickers or []),
                    json.dumps(article or {}),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            conn.rollback()          # the failed INSERT left this thread's transaction open, holding the write lock
            return None


def mark_emailed(alert_id: int) -> None:
    conn = _connect()
    with dbconn.locked_write(_lock, conn):
        conn.execute(
            "UPDATE geo_alerts SET emailed_at = ? WHERE id = ?",
            (int(time.time()), alert_id),
        )
        conn.commit()


def acknowledge(alert_id: int) -> bool:
    conn = _connect()
    with dbconn.locked_write(_lock, conn):
        cur = conn.execute(
            "UPDATE geo_alerts SET acknowledged_at = ? WHERE id = ?",
            (int(time.time()), alert_id),
        )
        conn.commit()
        return cur.rowcount > 0


def list_alerts(
    *,
    limit: int = 50,
    offset: int = 0,
    min_severity: Optional[str] = None,
) -> list[GeoAlert]:
    conn = _connect()
    order = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    sql = "SELECT * FROM geo_alerts"
    params: list[Any] = []
    if min_severity and min_severity.upper() in order:
        allowed = order[: order.index(min_severity.upper()) + 1]
        placeholders = ",".join("?" * len(allowed))
        sql += f" WHERE severity IN ({placeholders})"
        params.extend(allowed)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_alert(r) for r in rows]


def get_alert(alert_id: int) -> Optional[GeoAlert]:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM geo_alerts WHERE id = ? LIMIT 1", (alert_id,)
    ).fetchone()
    return _row_to_alert(row) if row else None
