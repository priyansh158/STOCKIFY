"""Instrument identity with history (M1.1; A1_schemas.md §1.1, D-06, K-9).

Kite instrument tokens are unique only for the current day and are reused
after a contract expires, so STOCKIFY never keys anything on a token. Each
instrument gets a permanent surrogate `instrument_id`, found by its natural
key:

* derivatives: (exchange, underlying, kind, expiry, strike_paise)
* equities and indices: (exchange, tradingsymbol)

`sync(day, dumps)` folds one day's Kite instrument dumps into
`refdata.sqlite`: it keeps the raw dump, upserts instruments
(`first_seen` / `last_seen`), opens and closes dated token intervals, and
appends a `contract_specs` row only when lot size or tick size changes.
Dumps must be synced in date order per exchange.

Strikes and tick sizes are decimal strings in the dump and are converted to
paise exactly through `stockify.numerics` (never `round(x * 100)`).
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
from dataclasses import dataclass, field
from datetime import date

from stockify import numerics, paths
from stockify.data import refdata

DUMP_EXCHANGES = ("NSE", "NFO")
DUMP_COLUMNS = ("instrument_token", "exchange_token", "tradingsymbol", "name", "last_price",
                "expiry", "strike", "tick_size", "lot_size", "instrument_type", "segment", "exchange")
DERIVATIVE_KINDS = ("FUT", "CE", "PE")
UNDERLYING_FLOOR = "2015-01-01"


class SyncError(RuntimeError):
    """A dump cannot be applied (bad format, or out of date order)."""


@dataclass
class SyncStats:
    day: str
    rows: int = 0
    skipped: int = 0
    new_instruments: int = 0
    token_opened: int = 0
    token_closed: int = 0
    spec_rows: int = 0
    raw_files: list[str] = field(default_factory=list)


# ── Parsing ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DumpRow:
    exchange: str
    kind: str
    tradingsymbol: str
    underlying: str | None
    expiry: str | None
    strike_paise: int | None
    instrument_token: int
    exchange_token: int | None
    lot_size: int
    tick_paise: int

    @property
    def key(self) -> tuple:
        if self.kind in DERIVATIVE_KINDS:
            return (self.exchange, self.underlying, self.kind, self.expiry, self.strike_paise)
        return (self.exchange, self.tradingsymbol)


def _classify(rec: dict) -> str | None:
    segment, itype, exchange = rec["segment"], rec["instrument_type"], rec["exchange"]
    if segment == "INDICES":
        return "INDEX"
    if exchange == "NSE" and segment == "NSE" and itype == "EQ":
        return "EQ"
    if exchange == "NFO" and itype in DERIVATIVE_KINDS:
        return itype
    return None


def parse_dump(text: str) -> tuple[list[DumpRow], int]:
    """Parse one Kite instruments CSV. Returns (rows kept, rows skipped)."""
    reader = csv.DictReader(io.StringIO(text))
    missing = set(DUMP_COLUMNS) - set(reader.fieldnames or ())
    if missing:
        raise SyncError(f"instrument dump is missing columns: {sorted(missing)}")
    rows, skipped = [], 0
    for rec in reader:
        kind = _classify(rec)
        if kind is None:
            skipped += 1
            continue
        try:
            derivative = kind in DERIVATIVE_KINDS
            rows.append(DumpRow(
                exchange=rec["exchange"],
                kind=kind,
                tradingsymbol=rec["tradingsymbol"].strip().upper(),
                underlying=rec["name"].strip().upper() if derivative else None,
                expiry=(rec["expiry"].strip() or None) if derivative else None,
                strike_paise=numerics.to_paise(rec["strike"]) if kind in ("CE", "PE") else None,
                instrument_token=int(rec["instrument_token"]),
                exchange_token=int(rec["exchange_token"]) if rec["exchange_token"] else None,
                lot_size=int(rec["lot_size"] or 0),
                tick_paise=numerics.to_paise(rec["tick_size"] or "0"),
            ))
        except (ValueError, numerics.NumericError) as e:
            raise SyncError(f"bad dump row {rec.get('tradingsymbol')!r}: {e}") from e
    return rows, skipped


# ── Raw dumps (immutable, A1 §1.2) ───────────────────────────────────────────

def save_raw(day: date, exchange: str, text: str) -> str:
    """Keep the dump exactly as downloaded: gzip (mtime 0) plus a .sha256 sidecar.

    A file for the same day is never overwritten; a different download of the
    same day is kept next to it as `_r2`, `_r3`, ….
    """
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    directory = paths.nse_raw_dir("kite_instruments")
    stem = f"{day:%Y%m%d}_{exchange}"
    n = 1
    while True:
        name = f"{stem}.csv.gz" if n == 1 else f"{stem}_r{n}.csv.gz"
        target = directory / name
        sidecar = directory / (name + ".sha256")
        if not target.exists():
            target.write_bytes(gzip.compress(data, mtime=0))
            sidecar.write_text(f"{digest}  {name}\n", encoding="utf-8")
            return name
        if sidecar.exists() and sidecar.read_text(encoding="utf-8").split()[0] == digest:
            return name
        n += 1


# ── Sync ─────────────────────────────────────────────────────────────────────

def _sync_underlyings(conn, underlyings: dict[str, str]) -> None:
    for key, index_symbol in sorted(underlyings.items()):
        conn.execute(
            "INSERT OR IGNORE INTO index_underlyings (key, index_symbol, effective_from) VALUES (?, ?, ?)",
            (key.upper(), index_symbol, UNDERLYING_FLOOR),
        )


def _find_id(conn, row: DumpRow) -> int | None:
    if row.kind in DERIVATIVE_KINDS:
        hit = conn.execute(
            "SELECT instrument_id FROM instruments WHERE exchange=? AND underlying=? AND kind=? "
            "AND expiry=? AND IFNULL(strike_paise, -1)=IFNULL(?, -1)",
            (row.exchange, row.underlying, row.kind, row.expiry, row.strike_paise),
        ).fetchone()
    else:
        hit = conn.execute(
            "SELECT instrument_id FROM instruments WHERE exchange=? AND tradingsymbol=? "
            "AND kind IN ('EQ','INDEX')",
            (row.exchange, row.tradingsymbol),
        ).fetchone()
    return hit["instrument_id"] if hit else None


def _upsert_instrument(conn, row: DumpRow, day: str, stats: SyncStats) -> int:
    iid = _find_id(conn, row)
    if iid is None:
        cur = conn.execute(
            "INSERT INTO instruments (exchange, kind, tradingsymbol, underlying, expiry, strike_paise, "
            "first_seen, last_seen, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'kite_dump')",
            (row.exchange, row.kind, row.tradingsymbol, row.underlying, row.expiry,
             row.strike_paise, day, day),
        )
        stats.new_instruments += 1
        return cur.lastrowid
    conn.execute(
        "UPDATE instruments SET last_seen = MAX(last_seen, ?), first_seen = MIN(first_seen, ?), "
        "tradingsymbol = IFNULL(tradingsymbol, ?) WHERE instrument_id = ?",
        (day, day, row.tradingsymbol, iid),
    )
    return iid


def _open_interval(conn, iid: int):
    return conn.execute(
        "SELECT instrument_token, valid_from FROM instrument_tokens "
        "WHERE instrument_id=? AND valid_to IS NULL", (iid,),
    ).fetchone()


def _apply_token(conn, iid: int, row: DumpRow, day: str, stats: SyncStats) -> None:
    current = _open_interval(conn, iid)
    if current is not None and current["instrument_token"] == row.instrument_token:
        return
    # The token is being reused: close it on whichever instrument still holds it.
    for other in conn.execute(
        "SELECT instrument_id, valid_from FROM instrument_tokens "
        "WHERE instrument_token=? AND valid_to IS NULL AND instrument_id<>?",
        (row.instrument_token, iid),
    ).fetchall():
        _close(conn, other["instrument_id"], other["valid_from"], day, stats)
    if current is not None:
        _close(conn, iid, current["valid_from"], day, stats)
    conn.execute(
        "INSERT INTO instrument_tokens (instrument_id, instrument_token, exchange_token, valid_from, valid_to) "
        "VALUES (?, ?, ?, ?, NULL) ON CONFLICT (instrument_id, valid_from) DO UPDATE SET "
        "instrument_token=excluded.instrument_token, exchange_token=excluded.exchange_token, valid_to=NULL",
        (iid, row.instrument_token, row.exchange_token, day),
    )
    stats.token_opened += 1


def _close(conn, iid: int, valid_from: str, day: str, stats: SyncStats) -> None:
    if valid_from == day:   # opened and superseded on the same day: drop the empty interval
        conn.execute("DELETE FROM instrument_tokens WHERE instrument_id=? AND valid_from=?", (iid, valid_from))
    else:
        conn.execute("UPDATE instrument_tokens SET valid_to=? WHERE instrument_id=? AND valid_from=?",
                     (day, iid, valid_from))
    stats.token_closed += 1


def _apply_spec(conn, iid: int, row: DumpRow, day: str, stats: SyncStats) -> None:
    if row.kind == "INDEX" or row.lot_size <= 0 or row.tick_paise <= 0:
        return
    latest = conn.execute(
        "SELECT effective_from, lot_size, tick_paise FROM contract_specs WHERE instrument_id=? "
        "ORDER BY effective_from DESC LIMIT 1", (iid,),
    ).fetchone()
    if latest is not None and (latest["lot_size"], latest["tick_paise"]) == (row.lot_size, row.tick_paise):
        return
    conn.execute(
        "INSERT INTO contract_specs (instrument_id, effective_from, lot_size, tick_paise, source) "
        "VALUES (?, ?, ?, ?, 'kite_dump') ON CONFLICT (instrument_id, effective_from) DO UPDATE SET "
        "lot_size=excluded.lot_size, tick_paise=excluded.tick_paise, source=excluded.source",
        (iid, day, row.lot_size, row.tick_paise),
    )
    stats.spec_rows += 1


def _link_underlyings(conn) -> None:
    """Set underlying_id for derivatives whose underlying instrument is known.

    One lookup per distinct underlying, then one UPDATE each (the first real
    dump, ~54k rows, made a correlated subquery per row far too slow).
    """
    pending = [r["underlying"] for r in conn.execute(
        "SELECT DISTINCT underlying FROM instruments "
        "WHERE kind IN ('FUT','CE','PE','FUT_CONT') AND underlying_id IS NULL AND underlying IS NOT NULL")]
    if not pending:
        return
    index_of = {r["key"]: r["index_symbol"] for r in conn.execute("SELECT key, index_symbol FROM index_underlyings")}
    for und in pending:
        if und in index_of:
            exchange, symbol = index_of[und].split(":", 1)
            hit = conn.execute("SELECT instrument_id FROM instruments WHERE kind='INDEX' AND exchange=? "
                               "AND tradingsymbol=? ORDER BY instrument_id LIMIT 1", (exchange, symbol)).fetchone()
        else:
            hit = conn.execute("SELECT instrument_id FROM instruments WHERE kind='EQ' AND exchange='NSE' "
                               "AND tradingsymbol=? ORDER BY instrument_id LIMIT 1", (und,)).fetchone()
        if hit:
            conn.execute("UPDATE instruments SET underlying_id=? WHERE underlying=? AND underlying_id IS NULL "
                         "AND kind IN ('FUT','CE','PE','FUT_CONT')", (hit["instrument_id"], und))


def sync(day: date, dumps: dict[str, str], underlyings: dict[str, str] | None = None) -> SyncStats:
    """Apply one day's dumps ({exchange: csv text}) to refdata.sqlite."""
    if underlyings is None:
        from stockify.config import loader
        underlyings = loader.load_json("contract_rules.json").get("underlyings", {})
    d = day.isoformat()
    stats = SyncStats(day=d)
    parsed: dict[str, list[DumpRow]] = {}
    for exchange, text in sorted(dumps.items()):
        rows, skipped = parse_dump(text)
        if any(r.exchange != exchange and not (exchange == "NSE" and r.kind == "INDEX") for r in rows):
            raise SyncError(f"dump for {exchange} contains rows for another exchange")
        parsed[exchange] = rows
        stats.rows += len(rows)
        stats.skipped += skipped
        stats.raw_files.append(save_raw(day, exchange, text))

    with refdata.transaction() as conn:
        for exchange in parsed:
            newest = conn.execute(
                "SELECT MAX(last_seen) AS d FROM instruments WHERE exchange=? AND source='kite_dump'",
                (exchange,),
            ).fetchone()["d"]
            if newest is not None and d < newest:
                raise SyncError(f"{exchange} dump for {d} is older than the last synced day {newest}")
        _sync_underlyings(conn, underlyings)
        for exchange, rows in parsed.items():
            seen: set[int] = set()
            for row in rows:
                iid = _upsert_instrument(conn, row, d, stats)
                seen.add(iid)
                _apply_token(conn, iid, row, d, stats)
                _apply_spec(conn, iid, row, d, stats)
            # Instruments of this exchange missing from today's dump lose their token.
            for gone in conn.execute(
                "SELECT t.instrument_id, t.valid_from FROM instrument_tokens t "
                "JOIN instruments i USING (instrument_id) "
                "WHERE t.valid_to IS NULL AND i.exchange=?", (exchange,),
            ).fetchall():
                if gone["instrument_id"] not in seen:
                    _close(conn, gone["instrument_id"], gone["valid_from"], d, stats)
        _link_underlyings(conn)
    return stats


# ── Lookups (the public API everything else uses) ────────────────────────────

def _iso(d: date | str) -> str:
    return d if isinstance(d, str) else d.isoformat()


def resolve_token(token: int, on_date: date | str) -> int | None:
    row = refdata._connect().execute(
        "SELECT instrument_id FROM instrument_tokens WHERE instrument_token=? AND valid_from<=? "
        "AND (valid_to IS NULL OR valid_to>?) ORDER BY valid_from DESC LIMIT 1",
        (int(token), _iso(on_date), _iso(on_date)),
    ).fetchone()
    return row["instrument_id"] if row else None


def token_at(instrument_id: int, on_date: date | str) -> int | None:
    row = refdata._connect().execute(
        "SELECT instrument_token FROM instrument_tokens WHERE instrument_id=? AND valid_from<=? "
        "AND (valid_to IS NULL OR valid_to>?) ORDER BY valid_from DESC LIMIT 1",
        (instrument_id, _iso(on_date), _iso(on_date)),
    ).fetchone()
    return row["instrument_token"] if row else None


def get(instrument_id: int) -> dict | None:
    row = refdata._connect().execute(
        "SELECT * FROM instruments WHERE instrument_id=?", (instrument_id,)).fetchone()
    return dict(row) if row else None


def is_government_security(tradingsymbol: str | None, isin: str | None = None) -> bool:
    """Sovereign gold bonds and other government securities: NSE lists them, but they are not stocks. By the SGB
    symbol, or an IN00 ISIN (the Government of India's) when one is known."""
    return (tradingsymbol or "").upper().startswith("SGB") or (isin or "").upper().startswith("IN00")


def find(exchange: str, tradingsymbol: str) -> int | None:
    """Instrument id by exchange + tradingsymbol (latest seen if a symbol was reused)."""
    row = refdata._connect().execute(
        "SELECT instrument_id FROM instruments WHERE exchange=? AND tradingsymbol=? "
        "ORDER BY last_seen DESC, instrument_id DESC LIMIT 1",
        (exchange.upper(), tradingsymbol.upper()),
    ).fetchone()
    return row["instrument_id"] if row else None


def derivative(underlying: str, kind: str, expiry: str, strike_paise: int | None = None,
               exchange: str = "NFO") -> int | None:
    """Instrument id of a derivative by its natural key (D-06)."""
    row = refdata._connect().execute(
        "SELECT instrument_id FROM instruments WHERE exchange=? AND underlying=? AND kind=? "
        "AND expiry=? AND IFNULL(strike_paise, -1)=IFNULL(?, -1)",
        (exchange.upper(), underlying.upper(), kind.upper(), expiry, strike_paise),
    ).fetchone()
    return row["instrument_id"] if row else None


def spec_at(instrument_id: int, on_date: date | str) -> dict | None:
    row = refdata._connect().execute(
        "SELECT effective_from, lot_size, tick_paise, source FROM contract_specs "
        "WHERE instrument_id=? AND effective_from<=? ORDER BY effective_from DESC LIMIT 1",
        (instrument_id, _iso(on_date)),
    ).fetchone()
    return dict(row) if row else None


def expiries(underlying: str, kind: str, on_date: date | str) -> list[str]:
    """Expiries on or after `on_date` among contracts listed by then and not
    expired early before it (D-41).

    `kind` is FUT, CE, PE or OPT (calls and puts).
    """
    kinds = ("CE", "PE") if kind.upper() == "OPT" else (kind.upper(),)
    marks = ",".join("?" * len(kinds))
    rows = refdata._connect().execute(
        f"SELECT DISTINCT expiry FROM instruments WHERE underlying=? AND kind IN ({marks}) "
        "AND first_seen<=? AND IFNULL(actual_expiry, expiry)>=? ORDER BY expiry",
        (underlying.upper(), *kinds, _iso(on_date), _iso(on_date)),
    ).fetchall()
    return [r["expiry"] for r in rows]


def option_chain(underlying: str, expiry: str, on_date: date | str) -> list[dict]:
    """Calls and puts of one expiry listed by `on_date`, by strike then kind."""
    rows = refdata._connect().execute(
        "SELECT instrument_id, kind, strike_paise, tradingsymbol FROM instruments "
        "WHERE underlying=? AND expiry=? AND kind IN ('CE','PE') AND first_seen<=? "
        "AND IFNULL(actual_expiry, expiry)>=? ORDER BY strike_paise, kind",
        (underlying.upper(), expiry, _iso(on_date), _iso(on_date)),
    ).fetchall()
    return [dict(r) for r in rows]


def underlying_of(instrument_id: int) -> int | None:
    row = refdata._connect().execute(
        "SELECT underlying_id FROM instruments WHERE instrument_id=?", (instrument_id,)).fetchone()
    return row["underlying_id"] if row else None


# ── Download (live use; tests pass dumps in directly) ────────────────────────

def download_dumps(exchanges: tuple[str, ...] = DUMP_EXCHANGES) -> dict[str, str]:
    """Fetch today's raw instrument CSVs from Kite (needs a fresh login).

    Uses the SDK's request path for `/instruments/<exchange>`, which returns
    the CSV bytes unparsed, so decimals stay exact.
    """
    import kite_client
    kite = kite_client.get_client()
    return {ex: kite._get("market.instruments", url_args={"exchange": ex}).decode("utf-8")
            for ex in exchanges}


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def display_name(info: dict | None) -> str | None:
    """The name to show a person for an instrument row (refdata row or a bundle.json instrument row).

    The Kite tradingsymbol when there is one. A contract first seen in a bhavcopy has none (A1 §1: tradingsymbol
    is NULL for bhavcopy-only contracts, which is every historical F&O contract), so it is named from what the
    row does hold: `NIFTY 24500 CE (9 Nov 2026)`, `NIFTY FUT (27 Nov 2026)`. Nothing is invented: no Kite-style
    symbol is built. None only when the row says nothing usable (the caller then falls back to the id)."""
    if not info:
        return None
    if info.get("tradingsymbol"):
        return str(info["tradingsymbol"])
    und = info.get("underlying")
    if not und:
        return None
    parts = [str(und)]
    strike = info.get("strike_paise")
    if strike is not None:
        rupees, paise = divmod(int(strike), 100)
        parts.append(f"{rupees}" if paise == 0 else f"{rupees}.{paise:02d}")
    if info.get("kind") and info["kind"] not in ("EQ", "ETF", "INDEX"):
        parts.append(str(info["kind"]))
    name = " ".join(parts)
    exp = info.get("expiry")
    if exp:
        try:
            y, m, d = (int(x) for x in str(exp).split("-"))
            name += f" ({d} {_MONTHS[m - 1]} {y})"
        except (ValueError, IndexError):
            name += f" ({exp})"
    return name
