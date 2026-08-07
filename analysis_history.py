"""
SQLite-backed history of completed analyses.

Replaces the deprecated PDF report as the artifact of record. Each completed
4-box analysis is persisted with the full per-box JSON results, verdict,
macro snapshot, and any anonymized position context. The History page reads
this store to show a reverse-chronological card list with filters; clicking
a card re-renders the full analysis from stored JSON (no LLM re-call).

Store lives at %LOCALAPPDATA%\\STOCKIFY\\kite\\history.sqlite — same dir as
the Kite caches because they're conceptually local-only state owned by this
machine's STOCKIFY install.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import kite_client  # for the shared cache dir


_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    company_name TEXT,
    sector TEXT,
    verdict TEXT,
    verdict_reason TEXT,
    bottom_line TEXT,
    financial_score INTEGER,
    geo_risk_score INTEGER,
    technical_classification TEXT,
    profile_json TEXT,
    financials_json TEXT,
    geo_json TEXT,
    technical_json TEXT,
    summary_json TEXT,
    macro_snapshot_json TEXT,
    position_context_json TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_analyses_ticker ON analyses(ticker);
CREATE INDEX IF NOT EXISTS idx_analyses_created ON analyses(created_at DESC);
"""


def _db_path() -> Path:
    return kite_client._cache_dir() / "history.sqlite"


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        conn = sqlite3.connect(str(_db_path()), timeout=10.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _conn = conn
        return _conn


def init_db() -> None:
    _connect()


@dataclass
class AnalysisRecord:
    id: int = 0
    created_at: int = 0
    ticker: str = ""
    company_name: str = ""
    sector: str = ""
    verdict: str = ""
    verdict_reason: str = ""
    bottom_line: str = ""
    financial_score: int = 0
    geo_risk_score: int = 0
    technical_classification: str = ""
    profile: dict = field(default_factory=dict)
    financials: dict = field(default_factory=dict)
    geo: dict = field(default_factory=dict)
    technical: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    macro_snapshot: dict | None = None
    position_context: dict | None = None
    notes: str = ""


def _record_from_row(row: sqlite3.Row) -> AnalysisRecord:
    def _load(s):
        if not s:
            return {}
        try:
            return json.loads(s)
        except Exception:
            return {}
    return AnalysisRecord(
        id=int(row[0]),
        created_at=int(row[1] or 0),
        ticker=row[2] or "",
        company_name=row[3] or "",
        sector=row[4] or "",
        verdict=row[5] or "",
        verdict_reason=row[6] or "",
        bottom_line=row[7] or "",
        financial_score=int(row[8] or 0),
        geo_risk_score=int(row[9] or 0),
        technical_classification=row[10] or "",
        profile=_load(row[11]),
        financials=_load(row[12]),
        geo=_load(row[13]),
        technical=_load(row[14]),
        summary=_load(row[15]),
        macro_snapshot=_load(row[16]) or None,
        position_context=_load(row[17]) or None,
        notes=row[18] or "",
    )


# ── Writer ────────────────────────────────────────────────────────────────────

def save_analysis(
    *,
    ticker: str,
    company_name: str,
    sector: str,
    verdict: str,
    verdict_reason: str,
    bottom_line: str,
    profile: dict,
    financials: dict,
    geo: dict,
    technical: dict,
    summary: dict,
    macro_snapshot: dict | None = None,
    position_context: dict | None = None,
) -> int:
    """Insert one completed analysis. Returns the new row id."""
    conn = _connect()
    fin_score = int(financials.get("score") or 0)
    geo_score = int(geo.get("risk_score") or 0)
    tech_cls  = (technical.get("recommendation") or "")[:32]
    payload = (
        int(time.time()),
        ticker.strip().upper(),
        company_name or "",
        sector or "",
        verdict or "",
        verdict_reason or "",
        bottom_line or "",
        fin_score,
        geo_score,
        tech_cls,
        json.dumps(profile or {}, default=str),
        json.dumps(financials or {}, default=str),
        json.dumps(geo or {}, default=str),
        json.dumps(technical or {}, default=str),
        json.dumps(summary or {}, default=str),
        json.dumps(macro_snapshot, default=str) if macro_snapshot else None,
        json.dumps(position_context, default=str) if position_context else None,
        "",
    )
    try:
        with _lock:
            cur = conn.execute(
                "INSERT INTO analyses "
                "(created_at, ticker, company_name, sector, verdict, verdict_reason, "
                " bottom_line, financial_score, geo_risk_score, technical_classification, "
                " profile_json, financials_json, geo_json, technical_json, summary_json, "
                " macro_snapshot_json, position_context_json, notes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
            conn.commit()
            return int(cur.lastrowid)
    except Exception:
        return 0


# ── Readers ───────────────────────────────────────────────────────────────────

_BASE_COLS = (
    "id, created_at, ticker, company_name, sector, verdict, verdict_reason, "
    "bottom_line, financial_score, geo_risk_score, technical_classification, "
    "profile_json, financials_json, geo_json, technical_json, summary_json, "
    "macro_snapshot_json, position_context_json, notes"
)


def list_analyses(
    *,
    limit: int = 100,
    offset: int = 0,
    ticker: str | None = None,
    verdict_contains: str | None = None,
    since_ts: int | None = None,
) -> list[AnalysisRecord]:
    """Return matching records, newest first. Pulls full rows — light enough
    for the history page since records are ~5-10 KB each."""
    conn = _connect()
    clauses, params = [], []
    if ticker:
        clauses.append("ticker = ?")
        params.append(ticker.strip().upper())
    if verdict_contains:
        clauses.append("UPPER(verdict) LIKE ?")
        params.append(f"%{verdict_contains.strip().upper()}%")
    if since_ts:
        clauses.append("created_at >= ?")
        params.append(int(since_ts))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (f"SELECT {_BASE_COLS} FROM analyses{where} "
           f"ORDER BY created_at DESC LIMIT ? OFFSET ?")
    params.extend([int(limit), int(offset)])
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        return []
    return [_record_from_row(r) for r in rows]


def get_analysis(analysis_id: int) -> AnalysisRecord | None:
    conn = _connect()
    try:
        row = conn.execute(
            f"SELECT {_BASE_COLS} FROM analyses WHERE id = ?",
            (int(analysis_id),)
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return _record_from_row(row)


def delete_analysis(analysis_id: int) -> bool:
    conn = _connect()
    try:
        with _lock:
            cur = conn.execute("DELETE FROM analyses WHERE id = ?", (int(analysis_id),))
            conn.commit()
            return (cur.rowcount or 0) > 0
    except Exception:
        return False


def count_analyses(ticker: str | None = None) -> int:
    conn = _connect()
    sql = "SELECT COUNT(*) FROM analyses"
    params: tuple = ()
    if ticker:
        sql += " WHERE ticker = ?"
        params = (ticker.strip().upper(),)
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    except Exception:
        return 0
