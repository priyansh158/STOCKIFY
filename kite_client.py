"""
Kite Connect client for STOCKIFY.

Owns:
- Auth (API key/secret from .env, daily access_token cached to disk)
- Daily NSE/BSE instruments dump cached in SQLite
- Symbol resolution (the Phase-0 unlock that fixes Screener rename-lag)
- Thin REST wrappers (ltp, historical_data, holdings, positions)

Track A of the two-track future architecture. The fundamentals leg
(Screener.in, NSE Corporate Announcements) is independent of this module.

Static creds live in .env (KITE_API_KEY, KITE_API_SECRET).
Dynamic access_token lives in %LOCALAPPDATA%\\STOCKIFY\\kite\\kite_token.json
(not in .env, since .env is hook-protected and meant for static keys).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import stockify_paths

try:
    from kiteconnect import KiteConnect
    KITECONNECT_AVAILABLE = True
except ImportError:
    KiteConnect = None  # type: ignore
    KITECONNECT_AVAILABLE = False


# ── Cache paths ───────────────────────────────────────────────────────────────

def _cache_dir() -> Path:
    """Compatibility wrapper for the canonical local state directory."""
    return stockify_paths.state_dir()


def _token_file() -> Path:
    return _cache_dir() / "kite_token.json"


def _instruments_db() -> Path:
    return _cache_dir() / "instruments.sqlite"


# ── Exceptions ────────────────────────────────────────────────────────────────

class KiteAuthError(Exception):
    """Base for Kite auth-related errors."""


class KiteNotAuthenticated(KiteAuthError):
    """No valid token cached; user must complete the login flow."""


class KiteTokenExpired(KiteAuthError):
    """Token was valid but has crossed the daily 06:00 IST expiry boundary."""


# ── Env / config ──────────────────────────────────────────────────────────────

def _api_key() -> str:
    return (os.environ.get("KITE_API_KEY") or "").strip()


def _api_secret() -> str:
    return (os.environ.get("KITE_API_SECRET") or "").strip()


def is_enabled() -> bool:
    """True iff both API key + secret + library are available."""
    return KITECONNECT_AVAILABLE and bool(_api_key()) and bool(_api_secret())


def _api_key_fingerprint() -> str:
    key = _api_key()
    if not key:
        return ""
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# ── Login flow ────────────────────────────────────────────────────────────────

def login_url() -> str:
    """Kite Connect v3 login URL (user opens this in a browser)."""
    if not _api_key():
        return ""
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={_api_key()}"


def exchange_request_token(request_token: str) -> dict:
    """Exchange a one-time request_token (from the redirect) for an access_token.

    Saves the token to disk and returns the full payload.
    """
    if not KITECONNECT_AVAILABLE:
        raise KiteAuthError("kiteconnect library not installed")
    if not _api_key() or not _api_secret():
        raise KiteAuthError("KITE_API_KEY / KITE_API_SECRET not set in .env")
    if not request_token:
        raise KiteAuthError("missing request_token")

    kc = KiteConnect(api_key=_api_key())
    data = kc.generate_session(request_token, api_secret=_api_secret())
    # Augment with our own metadata
    data["obtained_at"] = int(time.time())
    data["api_key_fingerprint"] = _api_key_fingerprint()
    save_token(data)
    return data


def save_token(payload: dict) -> None:
    """Persist token payload to disk."""
    path = _token_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_cached_token() -> dict | None:
    """Load cached token payload, or None if missing / corrupt / wrong api_key."""
    path = _token_file()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    # Discard if the api_key changed in .env since the token was issued
    if payload.get("api_key_fingerprint") and payload["api_key_fingerprint"] != _api_key_fingerprint():
        return None
    return payload


def clear_token() -> None:
    """Logout — remove the cached token."""
    try:
        _token_file().unlink(missing_ok=True)
    except Exception:
        pass


# ── Token freshness ───────────────────────────────────────────────────────────

# Kite access tokens expire daily at ~06:00 IST.
_IST = timezone(timedelta(hours=5, minutes=30))


def token_is_fresh(payload: dict | None) -> bool:
    """True if the token was obtained after the most recent 06:00 IST boundary."""
    if not payload or not payload.get("access_token"):
        return False
    obtained_at = payload.get("obtained_at")
    if not obtained_at:
        return False
    obtained = datetime.fromtimestamp(int(obtained_at), tz=timezone.utc).astimezone(_IST)
    now_ist = datetime.now(_IST)
    # Most recent 06:00 IST boundary <= now
    todays_boundary = now_ist.replace(hour=6, minute=0, second=0, microsecond=0)
    if now_ist < todays_boundary:
        boundary = todays_boundary - timedelta(days=1)
    else:
        boundary = todays_boundary
    return obtained >= boundary


def is_authenticated() -> bool:
    """True if we have a fresh cached token (no network call)."""
    return is_enabled() and token_is_fresh(load_cached_token())


# ── KiteConnect instance ──────────────────────────────────────────────────────

_client_lock = threading.Lock()
_client: Optional["KiteConnect"] = None
_client_token_fp: str = ""


def get_client() -> "KiteConnect":
    """Return a KiteConnect instance with access_token set.

    Raises KiteNotAuthenticated / KiteTokenExpired if no fresh token cached.
    """
    global _client, _client_token_fp
    if not is_enabled():
        raise KiteNotAuthenticated("Kite is not configured (missing API key/secret)")

    payload = load_cached_token()
    if not payload:
        raise KiteNotAuthenticated("no cached Kite token — login required")
    if not token_is_fresh(payload):
        raise KiteTokenExpired("cached Kite token is past daily 06:00 IST expiry")

    token = payload["access_token"]
    fp = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

    with _client_lock:
        if _client is None or _client_token_fp != fp:
            _client = KiteConnect(api_key=_api_key())
            _client.set_access_token(token)
            _client_token_fp = fp
        return _client


# ── Instruments cache ─────────────────────────────────────────────────────────

_INSTRUMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    instrument_token INTEGER PRIMARY KEY,
    exchange_token INTEGER,
    tradingsymbol TEXT NOT NULL,
    name TEXT,
    instrument_type TEXT,
    segment TEXT,
    exchange TEXT,
    tick_size REAL,
    lot_size INTEGER,
    fetched_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_inst_symbol ON instruments(tradingsymbol);
CREATE INDEX IF NOT EXISTS idx_inst_name ON instruments(name);
CREATE INDEX IF NOT EXISTS idx_inst_segment ON instruments(segment, instrument_type);
"""

_instruments_lock = threading.Lock()
_refresh_in_progress = False


def _connect_db() -> sqlite3.Connection:
    db = _instruments_db()
    conn = sqlite3.connect(str(db), timeout=10.0)
    conn.executescript(_INSTRUMENTS_SCHEMA)
    return conn


def instruments_age_seconds() -> int:
    """Seconds since the instruments cache was last refreshed. Large if missing."""
    db = _instruments_db()
    if not db.exists():
        return 10**9
    try:
        with _connect_db() as conn:
            row = conn.execute("SELECT MAX(fetched_at) FROM instruments").fetchone()
        if not row or row[0] is None:
            return 10**9
        return max(0, int(time.time()) - int(row[0]))
    except Exception:
        return 10**9


def instruments_row_count() -> int:
    db = _instruments_db()
    if not db.exists():
        return 0
    try:
        with _connect_db() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0])
    except Exception:
        return 0


def needs_instruments_refresh(max_age_seconds: int = 24 * 3600) -> bool:
    return (instruments_row_count() == 0 or
            instruments_age_seconds() > max_age_seconds)


def refresh_instruments(force: bool = False, max_age_seconds: int = 24 * 3600) -> int:
    """Download the full instruments dump and replace the SQLite cache.

    Returns the row count after refresh. No-op if cache is fresh and force=False.
    Raises if Kite is not enabled / authenticated.
    """
    global _refresh_in_progress
    if not force and not needs_instruments_refresh(max_age_seconds):
        return instruments_row_count()

    with _instruments_lock:
        if _refresh_in_progress and not force:
            return instruments_row_count()
        _refresh_in_progress = True

    try:
        kite = get_client()
        # KiteConnect.instruments() returns a list of dicts already.
        rows = kite.instruments()  # default = all exchanges
        now = int(time.time())

        # Filter to NSE/BSE equities + indices (we use indices for macro tokens).
        keep_segments = {"NSE", "BSE", "INDICES"}
        filtered = []
        for r in rows:
            seg = r.get("segment") or ""
            if seg not in keep_segments:
                continue
            filtered.append((
                int(r.get("instrument_token") or 0),
                int(r.get("exchange_token") or 0) if r.get("exchange_token") else None,
                str(r.get("tradingsymbol") or "").upper(),
                str(r.get("name") or "")[:200],
                str(r.get("instrument_type") or ""),
                str(r.get("segment") or ""),
                str(r.get("exchange") or ""),
                float(r.get("tick_size") or 0) or None,
                int(r.get("lot_size") or 0) or None,
                now,
            ))

        with _connect_db() as conn:
            conn.execute("DELETE FROM instruments")
            conn.executemany(
                "INSERT OR REPLACE INTO instruments "
                "(instrument_token, exchange_token, tradingsymbol, name, "
                " instrument_type, segment, exchange, tick_size, lot_size, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                filtered,
            )
            conn.commit()
        return len(filtered)
    finally:
        with _instruments_lock:
            _refresh_in_progress = False


def refresh_instruments_async(max_age_seconds: int = 24 * 3600) -> None:
    """Kick off refresh on a daemon thread if needed. Returns immediately."""
    if not needs_instruments_refresh(max_age_seconds):
        return
    if not is_authenticated():
        return
    def _run():
        try:
            refresh_instruments(force=False, max_age_seconds=max_age_seconds)
        except Exception:
            pass
    t = threading.Thread(target=_run, name="kite-instruments-refresh", daemon=True)
    t.start()


def is_refresh_in_progress() -> bool:
    return _refresh_in_progress


# ── Symbol resolution ─────────────────────────────────────────────────────────

@dataclass
class InstrumentRow:
    instrument_token: int
    tradingsymbol: str
    name: str
    exchange: str        # "NSE" | "BSE"
    segment: str         # "NSE" | "BSE" | "INDICES"
    instrument_type: str # "EQ" for equities
    lot_size: Optional[int] = None


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "instrument_token": int(row[0]),
        "tradingsymbol":    row[1],
        "name":             row[2] or "",
        "exchange":         row[3] or "",
        "segment":          row[4] or "",
        "instrument_type":  row[5] or "",
    }


def resolve_symbol(query: str, limit: int = 12) -> list[dict]:
    """Look up a query (symbol or company name) against the instruments cache.

    Returns up to `limit` rows, NSE preferred when both NSE+BSE listings exist.
    Equities only (instrument_type='EQ').

    Empty list when Kite not enabled or no match.
    """
    if not is_enabled():
        return []
    q = (query or "").strip().upper()
    if len(q) < 2:
        return []
    if instruments_row_count() == 0:
        return []

    select = ("SELECT instrument_token, tradingsymbol, name, exchange, segment, "
              "instrument_type FROM instruments WHERE segment IN ('NSE','BSE') "
              "AND instrument_type = 'EQ' ")

    results: list[dict] = []
    seen: set[str] = set()

    def _push(rows: list[sqlite3.Row]):
        for r in rows:
            d = _row_to_dict(r)
            key = d["tradingsymbol"]
            if key in seen:
                continue
            seen.add(key)
            results.append(d)
            if len(results) >= limit:
                return

    try:
        with _connect_db() as conn:
            # 1. Exact tradingsymbol match (NSE first, then BSE)
            rows = conn.execute(
                select + "AND tradingsymbol = ? ORDER BY (exchange='NSE') DESC, tradingsymbol",
                (q,)
            ).fetchall()
            _push(rows)
            if len(results) >= limit:
                return results

            # 2. Tradingsymbol prefix match
            rows = conn.execute(
                select + "AND tradingsymbol LIKE ? ORDER BY (exchange='NSE') DESC, tradingsymbol LIMIT ?",
                (q + "%", limit)
            ).fetchall()
            _push(rows)
            if len(results) >= limit:
                return results

            # 3. Name substring match (case-insensitive)
            rows = conn.execute(
                select + "AND UPPER(name) LIKE ? ORDER BY (exchange='NSE') DESC, name LIMIT ?",
                ("%" + q + "%", limit)
            ).fetchall()
            _push(rows)
    except Exception:
        return results

    return results


def get_instrument_token(tradingsymbol: str, exchange: str = "NSE") -> int | None:
    """Return the instrument_token for a bare NSE/BSE symbol, or None."""
    if not is_enabled() or not tradingsymbol:
        return None
    try:
        with _connect_db() as conn:
            row = conn.execute(
                "SELECT instrument_token FROM instruments "
                "WHERE tradingsymbol = ? AND exchange = ? AND instrument_type = 'EQ' "
                "LIMIT 1",
                (tradingsymbol.strip().upper(), exchange.upper())
            ).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def get_instrument_token_for_index(index_name: str) -> int | None:
    """Return instrument_token for an index (NIFTY 50, BANK NIFTY, etc.).

    Indices live in segment='INDICES' and have different name conventions.
    """
    if not is_enabled() or not index_name:
        return None
    try:
        with _connect_db() as conn:
            row = conn.execute(
                "SELECT instrument_token FROM instruments "
                "WHERE segment = 'INDICES' AND "
                "(UPPER(tradingsymbol) = ? OR UPPER(name) = ?) LIMIT 1",
                (index_name.strip().upper(), index_name.strip().upper())
            ).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


# ── Thin REST wrappers ────────────────────────────────────────────────────────

def ltp(symbols: list[str]) -> dict[str, dict]:
    """Last traded prices for ["NSE:RELIANCE", "NSE:TCS", ...].

    Returns {} on auth/network failure (callers must handle empty).
    """
    if not symbols:
        return {}
    try:
        kite = get_client()
        return kite.ltp(symbols) or {}
    except KiteAuthError:
        raise
    except Exception:
        return {}


def quote(symbols: list[str]) -> dict[str, dict]:
    """Full quote (OHLC, depth, OI) for one or many symbols."""
    if not symbols:
        return {}
    try:
        kite = get_client()
        return kite.quote(symbols) or {}
    except KiteAuthError:
        raise
    except Exception:
        return {}


def historical(instrument_token: int, from_dt: datetime, to_dt: datetime,
               interval: str, continuous: bool = False) -> list[dict]:
    """OHLCV candles from Kite.

    interval ∈ {"minute", "3minute", "5minute", "10minute", "15minute",
                "30minute", "60minute", "day", "week", "month"}

    Returns a list of dicts: [{date, open, high, low, close, volume}, ...].
    Empty list on failure.
    """
    if not instrument_token:
        return []
    try:
        kite = get_client()
        return kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_dt,
            to_date=to_dt,
            interval=interval,
            continuous=continuous,
        ) or []
    except KiteAuthError:
        raise
    except Exception:
        return []


def holdings() -> list[dict]:
    """Long-term holdings from the user's Zerodha account."""
    try:
        kite = get_client()
        return kite.holdings() or []
    except KiteAuthError:
        raise
    except Exception:
        return []


def positions() -> dict:
    """Day + net positions."""
    try:
        kite = get_client()
        return kite.positions() or {"day": [], "net": []}
    except KiteAuthError:
        raise
    except Exception:
        return {"day": [], "net": []}


# ── Status snapshot (for sidebar widget) ──────────────────────────────────────

def status_snapshot() -> dict:
    """Single dict the UI uses to render the sidebar Kite badge."""
    payload = load_cached_token() if is_enabled() else None
    return {
        "enabled":           is_enabled(),
        "authenticated":     bool(payload and token_is_fresh(payload)),
        "user_name":         (payload or {}).get("user_name", "") if payload else "",
        "user_id":           (payload or {}).get("user_id", "") if payload else "",
        "instruments_rows":  instruments_row_count(),
        "instruments_age_s": instruments_age_seconds(),
        "refresh_pending":   is_refresh_in_progress(),
    }
