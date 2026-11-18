"""FS 2: NSE's results filings per company, and the resumable download of their XBRL files.

Two NSE listings hold a company's results (both read, both kept raw through `nse_api.Client`):

- `/api/corporates-financial-results?index=equities&symbol=&period=Quarterly|Half-Yearly|Annual` ("results"):
  every filing up to the quarter ended 31 Dec 2024. `period` maps to Q / H / A. The year's last quarter and the year
  are two entries (two rows here) that usually share one XBRL link; that file is fetched once.
- `/api/integrated-filing-results?index=equities&symbol=&type=Integrated Filing- Financials` ("integrated"): SEBI's
  integrated filing, every quarter from the one ended 31 Mar 2025 (RELIANCE, read 2026-11-17). It names only the
  quarter end (`qe_Date`), so its rows are Q; a March filing also carries the year's figures (the parser's concern).

Each filing keeps NSE's broadcast time (IST in the listing, UTC ns here): `broadCastDate`, else `exchdisstime`, else
`filingDate` (results), `broadcast_Date` (integrated); `broadcast_source` says which field it came from. Basis is
"consolidated" when NSE says "Consolidated", else "standalone". A filing without an XBRL link or a broadcast time
is listed as `held` with the reason. A later filing for the same period is a new row (its own filing_id): a
restatement is a version, never an overwrite.

`download_pending` fetches each `listed` filing's XBRL from nsearchives through the project's `nse_files.Http`
(1 s between requests, one at a time), keeps it at `nse/raw/fundamentals/xbrl/<YYYY>/<symbol>_<period_end>_<basis>_
<id12>.xml` (atomic write, `.sha256` sidecar) and marks it `downloaded`. A file already on disk with a matching sha
is taken as it is, so an interrupted run resumes without refetching. NSE refusing (401 / 403 / an HTML page) or the
deadline stops the run cleanly: what was saved stays saved, the rest stays `listed`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from stockify import dbconn, paths, timeutil
from stockify.data import nse_files
from stockify.fundamentals import nse_api, store

log = logging.getLogger(__name__)

LEGACY_PATH = "/api/corporates-financial-results"
LEGACY_WARMUP = "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
INTEGRATED_PATH = "/api/integrated-filing-results"
INTEGRATED_WARMUP = "https://www.nseindia.com/companies-listing/corporate-integrated-filing"
INTEGRATED_TYPE = "Integrated Filing- Financials"
PERIODS = {"Quarterly": "Q", "Half-Yearly": "H", "Annual": "A"}
XBRL_HOSTS = ("nsearchives.nseindia.in", "nsearchives.nseindia.com", "www.nseindia.com")
STATUSES = ("listed", "downloaded", "parsed", "held")

COLUMNS = ("filing_id", "symbol", "period_start", "period_end", "period_kind", "basis", "broadcast_ns",
           "broadcast_source", "source", "seq_number", "xbrl_url", "xbrl_sha256", "xbrl_file", "status", "reason")

_MONTHS = {m: n for n, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                       "DEC"), start=1)}
_DT = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})(?: (\d{1,2}):(\d{2})(?::(\d{2}))?)?$")


# ── Parsing NSE's fields ─────────────────────────────────────────────────────

def _parse(text: str) -> tuple[date, tuple[int, int, int] | None]:
    m = _DT.match(text.strip())
    if not m or m.group(2).upper() not in _MONTHS:
        raise ValueError(f"not an NSE date: {text!r}")
    d = date(int(m.group(3)), _MONTHS[m.group(2).upper()], int(m.group(1)))
    hms = None if m.group(4) is None else (int(m.group(4)), int(m.group(5)), int(m.group(6) or 0))
    return d, hms


def parse_day(text: str) -> date:
    """'31-Dec-2024' or '30-SEP-2026' (month names in English whatever the locale)."""
    d, hms = _parse(text)
    if hms is not None:
        raise ValueError(f"not an NSE date without time: {text!r}")
    return d


def parse_ist(text: str) -> int:
    """'16-Jan-2025 20:20:21' or '16-Jan-2025 20:20', IST, to UTC epoch ns."""
    d, hms = _parse(text)
    if hms is None:
        raise ValueError(f"not an NSE date and time: {text!r}")
    return timeutil.ist_to_ns(datetime(d.year, d.month, d.day, *hms, tzinfo=timeutil.IST))


def _has(v: Any) -> bool:
    return isinstance(v, str) and v.strip() not in ("", "-")


def broadcast_of(entry: dict, fields: tuple[str, ...] = ("broadCastDate", "exchdisstime", "filingDate")
                 ) -> tuple[int | None, str | None]:
    """(UTC ns, the field it came from) from the first of `fields` that holds a time; (None, None) when none does."""
    for f in fields:
        if _has(entry.get(f)):
            return parse_ist(entry[f]), f
    return None, None


def _xbrl_url(v: Any) -> tuple[str | None, str | None]:
    """(url, None) for an XBRL link on NSE's archive, else (None, the reason)."""
    if not _has(v) or str(v).rstrip().endswith("/-"):
        return None, "NSE lists no XBRL file for this filing"
    url = str(v).strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in XBRL_HOSTS or not parts.path.lower().endswith(".xml"):
        return None, f"the XBRL link is not an NSE archive XML file: {url}"
    return url, None


def filing_id(symbol: str, period_end: str, period_kind: str, basis: str, broadcast_ns: int | None,
              xbrl_url: str | None) -> str:
    """sha256 of the fields that make a filing (the period kind too: NSE lists the year's last quarter and the year
    as two entries, possibly with the same time and link)."""
    text = "\x1f".join((symbol, period_end, period_kind, basis, "" if broadcast_ns is None else str(broadcast_ns),
                        xbrl_url or ""))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row(symbol: str, *, period_start: str | None, period_end: str, period_kind: str, consolidated: Any,
         broadcast: tuple[int | None, str | None], source: str, seq: Any, xbrl: Any) -> dict:
    basis = "consolidated" if consolidated == "Consolidated" else "standalone"
    url, why = _xbrl_url(xbrl)
    if broadcast[0] is None:
        why = "; ".join(w for w in (why, "NSE lists no broadcast time for this filing") if w)
    return {"filing_id": filing_id(symbol, period_end, period_kind, basis, broadcast[0], url),
            "symbol": symbol, "period_start": period_start, "period_end": period_end, "period_kind": period_kind,
            "basis": basis, "broadcast_ns": broadcast[0], "broadcast_source": broadcast[1], "source": source,
            "seq_number": None if seq is None else str(seq), "xbrl_url": url, "xbrl_sha256": None, "xbrl_file": None,
            "status": "held" if why else "listed", "reason": why}


def _check_symbol(symbol: str, entry: dict) -> None:
    if entry.get("symbol") != symbol:
        raise ValueError(f"NSE answered for {entry.get('symbol')!r} when asked for {symbol!r}")


def parse_results(symbol: str, period: str, doc: Any, since: date) -> list[dict]:
    """The rows of one corporates-financial-results answer (a JSON list) with period end on/after `since`."""
    if not isinstance(doc, list):
        raise ValueError(f"corporates-financial-results for {symbol} is not a list")
    out = []
    for e in doc:
        _check_symbol(symbol, e)
        if e.get("period") != period:
            log.warning("fundamentals: %s %s listing holds a %r entry; skipped", symbol, period, e.get("period"))
            continue
        end = parse_day(e["toDate"])
        if end < since:
            continue
        start = parse_day(e["fromDate"]) if _has(e.get("fromDate")) else None
        out.append(_row(symbol, period_start=None if start is None else start.isoformat(), period_end=end.isoformat(),
                        period_kind=PERIODS[period], consolidated=e.get("consolidated"), broadcast=broadcast_of(e),
                        source="results", seq=e.get("seqNumber"), xbrl=e.get("xbrl")))
    return out


def parse_integrated(symbol: str, doc: Any, since: date) -> list[dict]:
    """The financial-results rows of one integrated-filing-results answer with quarter end on/after `since`."""
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise ValueError(f"integrated-filing-results for {symbol} has no data list")
    data = doc["data"]
    total = doc.get("totalCount")
    if isinstance(total, int) and total > len(data):
        log.warning("fundamentals: %s integrated filings: NSE says %d, sent %d (the rest are not listed)",
                    symbol, total, len(data))
    out = []
    for e in data:
        _check_symbol(symbol, e)
        if e.get("type") != INTEGRATED_TYPE:
            continue
        end = parse_day(e["qe_Date"])
        if end < since:
            continue
        out.append(_row(symbol, period_start=None, period_end=end.isoformat(), period_kind="Q",
                        consolidated=e.get("consolidated"), broadcast=broadcast_of(e, ("broadcast_Date",)),
                        source="integrated", seq=e.get("seq_Id"), xbrl=e.get("xbrl")))
    return out


# ── Store ────────────────────────────────────────────────────────────────────

def _insert(rows: list[dict]) -> int:
    """New filings only (a filing already known keeps its status); returns how many were new."""
    if not rows:
        return 0
    conn = store._connect()
    sql = f"INSERT OR IGNORE INTO filings ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})"
    with dbconn.locked_write(store._lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        before = conn.total_changes
        conn.executemany(sql, [tuple(r[c] for c in COLUMNS) for r in rows])
        new = conn.total_changes - before
        conn.execute("COMMIT")
    return new


def _update(fid: str, **fields) -> None:
    conn = store._connect()
    with dbconn.locked_write(store._lock, conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"UPDATE filings SET {', '.join(f'{k} = ?' for k in fields)} WHERE filing_id = ?",
                     (*fields.values(), fid))
        conn.execute("COMMIT")


def mark(fid: str, status: str, reason: str | None = None) -> None:
    """Set a filing's status (listed | downloaded | parsed | held) and its plain-words reason."""
    if status not in STATUSES:
        raise ValueError(f"not a filing status: {status!r}")
    _update(fid, status=status, reason=reason)


def get(fid: str) -> dict | None:
    r = store._connect().execute("SELECT * FROM filings WHERE filing_id = ?", (fid,)).fetchone()
    return None if r is None else dict(r)


def for_symbol(symbol: str) -> list[dict]:
    """A company's filings, oldest period first, then by broadcast time."""
    return [dict(r) for r in store._connect().execute(
        "SELECT * FROM filings WHERE symbol = ? ORDER BY period_end, broadcast_ns, filing_id", (symbol,)).fetchall()]


def by_status(status: str) -> list[dict]:
    """Filings in one status, newest broadcast first."""
    return [dict(r) for r in store._connect().execute(
        "SELECT * FROM filings WHERE status = ? ORDER BY broadcast_ns DESC, filing_id", (status,)).fetchall()]


def xbrl_path(row: dict) -> Path:
    """Where a downloaded filing's XBRL file is kept."""
    rel = row["xbrl_file"]
    parts = (rel or "").split("/")
    if len(parts) != 2 or not all(paths.safe_segment(p, 160) for p in parts):
        raise ValueError(f"not a kept XBRL file: {rel!r}")
    return paths.fundamentals_raw_dir("xbrl") / parts[0] / parts[1]


# ── Listing ──────────────────────────────────────────────────────────────────

def list_for(symbol: str, *, client: nse_api.Client, since: date) -> list[dict]:
    """Read both NSE listings for `symbol` (quarterly, half-yearly and annual results; integrated filings), keep the
    answers raw, record the filings with period end on/after `since` that are new, and return them all (as listed
    now). Nothing is recorded unless every answer was read: Blocked / NseError / ValueError propagate."""
    rows: list[dict] = []
    for period in PERIODS:
        doc = client.json(LEGACY_PATH, {"index": "equities", "symbol": symbol, "period": period},
                          warmup=LEGACY_WARMUP)
        rows += parse_results(symbol, period, doc, since)
    doc = client.json(INTEGRATED_PATH, {"index": "equities", "symbol": symbol, "type": INTEGRATED_TYPE},
                      warmup=INTEGRATED_WARMUP)
    rows += parse_integrated(symbol, doc, since)
    _insert(rows)
    return rows


# ── Download ─────────────────────────────────────────────────────────────────

def _file_name(row: dict) -> tuple[str, str]:
    sym = re.sub(r"[^A-Za-z0-9.-]+", "_", row["symbol"]).strip("_.") or "X"
    name = f"{sym}_{row['period_end']}_{row['basis']}_{row['filing_id'][:12]}.xml"
    year = row["period_end"][:4]
    if not paths.safe_segment(name, 160) or not paths.safe_segment(year, 8):
        raise ValueError(f"no safe file name for filing {row['filing_id']}")
    return year, name


def _xbrl_problem(body: bytes) -> str | None:
    """None for a well-formed XBRL instance, else what is wrong."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        return f"not a well-formed XBRL file ({e})"
    if not root.tag.endswith("}xbrl"):
        return f"not a well-formed XBRL file (root element {root.tag!r})"
    return None


def _looks_html(body: bytes) -> bool:
    head = body.lstrip()[:64].lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _sidecar(file: Path) -> Path:
    return file.with_name(file.name + ".sha256")


def _kept_sha(file: Path) -> str | None:
    """The sha of a file already kept whole (its sidecar matches, or it has none yet and is a good XBRL file)."""
    if not file.is_file():
        return None
    body = file.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    side = _sidecar(file)
    if side.is_file():
        parts = side.read_text(encoding="utf-8").split()
        return digest if parts and parts[0] == digest else None
    if _xbrl_problem(body) is None:                      # the run died between the file and its sidecar
        _write_atomic(side, f"{digest}  {file.name}\n".encode("utf-8"))
        return digest
    return None


def _write_atomic(target: Path, body: bytes) -> None:
    part = target.with_name(target.name + ".part")
    part.write_bytes(body)
    os.replace(part, target)


def _keep(row: dict, body: bytes) -> tuple[str, str]:
    year, name = _file_name(row)
    folder = paths.fundamentals_raw_dir("xbrl") / year
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(body).hexdigest()
    target = folder / name
    _write_atomic(target, body)
    _write_atomic(_sidecar(target), f"{digest}  {name}\n".encode("utf-8"))
    return digest, f"{year}/{name}"


def _same_file(row: dict) -> tuple[str, str] | None:
    """Another filing with the same XBRL link whose file is kept: (sha, xbrl_file)."""
    for other in store._connect().execute(
            "SELECT * FROM filings WHERE xbrl_url = ? AND filing_id <> ? AND xbrl_file IS NOT NULL"
            " AND status IN ('downloaded', 'parsed') ORDER BY filing_id", (row["xbrl_url"], row["filing_id"])):
        other = dict(other)
        try:
            sha = _kept_sha(xbrl_path(other))
        except ValueError:
            continue
        if sha is not None and sha == other["xbrl_sha256"]:
            return sha, other["xbrl_file"]
    return None


def download_pending(*, client: nse_api.Client, limit: int | None = None, deadline_ns: int | None = None
                     ) -> dict:
    """Fetch the XBRL file of every `listed` filing, newest broadcast first. Returns {"downloaded", "skipped" (kept
    already, no request), "failed", "stopped": None | "blocked" | "deadline" | "limit"}. `limit` caps the requests
    made; `deadline_ns` (UTC ns, the Http's clock) is checked before each one. A 404 holds the filing (NSE has no
    file at its link); another failure leaves it listed with the reason, tried again next run."""
    http = client.http
    out: dict[str, Any] = {"downloaded": 0, "skipped": 0, "failed": 0, "stopped": None}
    fetches = 0
    for row in by_status("listed"):
        if row["xbrl_url"] is None:
            continue
        year, name = _file_name(row)
        target = paths.fundamentals_raw_dir("xbrl") / year / name
        sha = _kept_sha(target)
        if sha is not None:
            _update(row["filing_id"], xbrl_sha256=sha, xbrl_file=f"{year}/{name}", status="downloaded", reason=None)
            out["skipped"] += 1
            continue
        same = _same_file(row)
        if same is not None:
            _update(row["filing_id"], xbrl_sha256=same[0], xbrl_file=same[1], status="downloaded", reason=None)
            out["skipped"] += 1
            continue
        if deadline_ns is not None and http.clock.now_ns() >= deadline_ns:
            out["stopped"] = "deadline"
            break
        if limit is not None and fetches >= limit:
            out["stopped"] = "limit"
            break
        fetches += 1
        url = row["xbrl_url"]
        try:
            status, body = http.get(url)
        except nse_files.NseError as e:
            log.warning("fundamentals: XBRL %s for %s: %s", url, row["symbol"], e)
            _update(row["filing_id"], reason=f"the XBRL file could not be fetched: {e}")
            out["failed"] += 1
            continue
        if status in (401, 403) or (status == 200 and _looks_html(body)):
            log.warning("fundamentals: NSE refused the XBRL file %s (HTTP %s); stopping, the rest stays listed",
                        url, status)
            out["stopped"] = "blocked"
            break
        if status == 404:
            mark(row["filing_id"], "held", f"NSE has no file at the filing's XBRL link (HTTP 404): {url}")
            out["failed"] += 1
            continue
        problem = f"HTTP {status}" if status != 200 else _xbrl_problem(body)
        if problem is not None:
            log.warning("fundamentals: XBRL %s for %s: %s", url, row["symbol"], problem)
            _update(row["filing_id"], reason=f"the XBRL file NSE sent is {problem}" if status == 200
                    else f"the XBRL file could not be fetched ({problem})")
            out["failed"] += 1
            continue
        sha, rel = _keep(row, body)
        _update(row["filing_id"], xbrl_sha256=sha, xbrl_file=rel, status="downloaded", reason=None)
        out["downloaded"] += 1
    return out
