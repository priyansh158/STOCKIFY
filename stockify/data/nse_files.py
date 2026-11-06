"""NSE end-of-day files: fetch, keep raw, parse, load (M1.3, A1 §1.2).

Datasets (URL templates and date ranges live in `stockify/config/sources.json`):

| dataset   | parser(s)                 | loads into                                  |
|-----------|---------------------------|---------------------------------------------|
| fo        | fo_udiff / fo_legacy      | fo_eod, instruments (bhavcopy), contract_specs |
| cm        | cm_udiff / cm_legacy      | cm_eod (series from `cm_series`), instruments |
| idx       | ind_close_all             | index_eod (names mapped to Kite indices)    |
| qtyfreeze | qtyfreeze (current only)  | freeze_limits (a row when a value changes)  |

Fetching: browser-like User-Agent and Referer, one cookie warm-up per
session, at most one request per `min_interval_ms`, `retries` attempts with
exponential backoff on errors and 5xx. A 404 means "not published / holiday"
(outcome `missing`), not an error.

Every download is kept unchanged under `$LAB/nse/raw/<dataset>/<YYYYMMDD>.<ext>`
with a `.sha256` sidecar; a different download for the same date is kept
next to it (`_r2`, …). Raw files are never modified or deleted. Each attempt
leaves one `nse_fetch_log` row per (dataset, date).

After the files of a day are loaded, `settle(day)` writes final settlement
prices for contracts expiring that day (D-13, D-39): NSE's own FSP from the
F&O bhavcopy, cross-checked against the index close or the official close;
a difference above one tick goes to `data_quality`.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date

from stockify import paths, timeutil
from stockify.config import loader
from stockify.data import instruments, nse_parsers, refdata

log = logging.getLogger("stockify.data.nse")

DATASETS = ("fo", "cm", "idx", "qtyfreeze")
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
# quality.FOREIGN_METRICS must list these two (checked by blockwatch).
# <block name="fsp-metrics" same-as="stockify/data/quality.py#/FOREIGN_METRICS" same-as-mode="sequence" same-as-pattern='"(?P<value>[a-z_]+)"'>
FSP_METRIC = "fsp_vs_close_paise"                   # FSP differs from the index / official close
FSP_SPREAD_METRIC = "fsp_settle_spread_paise"       # expiring contracts disagree on the FSP
# </block>


class NseError(RuntimeError):
    pass


# ── Sources ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Source:
    dataset: str
    parser: str
    url: str
    ext: str
    verified: bool
    effective_from: str
    valid_to: str | None
    current_only: bool


def config() -> dict:
    return loader.load_json("sources.json")


def source_for(dataset: str, day: date, cfg: dict | None = None) -> Source | None:
    """The format row in force on `day` (latest effective_from ≤ day, within valid_to)."""
    cfg = cfg or config()
    rows = cfg["datasets"].get(dataset)
    if rows is None:
        raise NseError(f"unknown dataset {dataset!r}; known: {', '.join(cfg['datasets'])}")
    d = day.isoformat()
    best = None
    for row in rows:
        if row["effective_from"] <= d and (row.get("valid_to") is None or d <= row["valid_to"]):
            if best is None or row["effective_from"] > best["effective_from"]:
                best = row
    if best is None:
        return None
    return Source(dataset, best["parser"], best["url"], best["ext"], best["verified"],
                  best["effective_from"], best.get("valid_to"), bool(best.get("current_only")))


def url_for(src: Source, day: date, cfg: dict | None = None) -> str:
    cfg = cfg or config()
    path = (src.url.replace("{YYYYMMDD}", f"{day:%Y%m%d}").replace("{DDMMYYYY}", f"{day:%d%m%Y}")
            .replace("{YYYY}", f"{day:%Y}").replace("{MON}", MONTHS[day.month - 1])
            .replace("{DD}", f"{day:%d}"))
    if "{" in path:
        raise NseError(f"unfilled placeholder in {src.url}")
    return cfg["http"]["host"].rstrip("/") + path


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Http:
    """Throttled GET with retries. `session` needs `.get(url, timeout=)` and
    `.headers`; `clock` is a daemon Clock (tests pass a ManualClock)."""

    def __init__(self, session=None, clock=None, cfg: dict | None = None):  # noqa: ANN001
        self.cfg = (cfg or config())["http"]
        if session is None:
            import requests
            session = requests.Session()
        if clock is None:
            from stockify.daemon.clock import WallClock
            clock = WallClock()
        session.headers.update({"User-Agent": self.cfg["user_agent"], "Referer": self.cfg["referer"],
                                "Accept": "*/*"})
        self.session, self.clock = session, clock
        self._last_mono: int | None = None
        self._warmed = False
        self.requests = 0

    def _throttle(self) -> None:
        gap = int(self.cfg["min_interval_ms"]) * 1_000_000
        if self._last_mono is not None:
            wait = self._last_mono + gap - self.clock.mono_ns()
            if wait > 0:
                self.clock.sleep_until(self.clock.now_ns() + wait)
        self._last_mono = self.clock.mono_ns()
        self.requests += 1

    def _warm_up(self) -> None:
        if self._warmed or not self.cfg.get("warmup_url"):
            return
        self._warmed = True
        self._throttle()
        try:
            self.session.get(self.cfg["warmup_url"], timeout=float(self.cfg["timeout_s"]))
        except Exception as e:                               # cookies are a courtesy, not a requirement
            log.debug("warm-up request failed: %s", e)

    def get(self, url: str) -> tuple[int, bytes]:
        """(http status, body). 404 returns at once; errors and 5xx are retried."""
        self._warm_up()
        tries = int(self.cfg["retries"])
        backoff = int(self.cfg["backoff_ms"]) * 1_000_000
        last_error = "no attempt"
        for attempt in range(tries):
            if attempt:
                self.clock.sleep_until(self.clock.now_ns() + backoff * (2 ** (attempt - 1)))
            self._throttle()
            try:
                r = self.session.get(url, timeout=float(self.cfg["timeout_s"]))
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                continue
            if r.status_code < 500:
                return r.status_code, r.content
            last_error = f"HTTP {r.status_code}"
        raise NseError(f"{url}: {last_error} after {tries} attempts")


# ── Raw files ────────────────────────────────────────────────────────────────

def save_raw(dataset: str, day: date, ext: str, data: bytes) -> tuple[str, str]:
    """Keep the bytes exactly as downloaded; returns (file name, sha256)."""
    digest = hashlib.sha256(data).hexdigest()
    directory = paths.nse_raw_dir(dataset)
    n = 1
    while True:
        name = f"{day:%Y%m%d}.{ext}" if n == 1 else f"{day:%Y%m%d}_r{n}.{ext}"
        target, sidecar = directory / name, directory / (name + ".sha256")
        if not target.exists():
            target.write_bytes(data)
            sidecar.write_text(f"{digest}  {name}\n", encoding="utf-8")
            return name, digest
        if sidecar.exists() and sidecar.read_text(encoding="utf-8").split()[0] == digest:
            return name, digest
        n += 1


def _raw_matches(dataset: str, sha: str | None) -> bool:
    if not sha:
        return False
    for sidecar in paths.nse_raw_dir(dataset).glob("*.sha256"):
        if sidecar.read_text(encoding="utf-8").split()[0] == sha:
            return True
    return False


# ── Loading ──────────────────────────────────────────────────────────────────

@dataclass
class LoadStats:
    rows: int = 0
    new_instruments: int = 0
    spec_rows: int = 0
    specs_without_tick: int = 0
    unmapped: list[str] = field(default_factory=list)
    freeze_changes: int = 0


class _TickHints:
    """Tick size for a contract seen only in a bhavcopy: the contract's own
    nearest spec, else the nearest spec of the same underlying and kind. None
    when nothing is known (the spec row is then skipped, never invented).
    The per-(underlying, kind) answer is computed once per load."""

    def __init__(self, conn, d: str):  # noqa: ANN001
        self.conn, self.d = conn, d
        self.by_key: dict[tuple[str, str], int | None] = {}

    def get(self, underlying: str, kind: str, iid: int | None = None) -> int | None:
        if iid is not None:
            row = self.conn.execute(
                "SELECT tick_paise FROM contract_specs WHERE instrument_id=? "
                "ORDER BY ABS(julianday(effective_from) - julianday(?)) LIMIT 1", (iid, self.d)).fetchone()
            if row:
                return row["tick_paise"]
        key = (underlying, kind)
        if key not in self.by_key:
            row = self.conn.execute(
                "SELECT s.tick_paise FROM instruments i JOIN contract_specs s USING (instrument_id) "
                "WHERE i.exchange='NFO' AND i.underlying=? AND i.kind=? "
                "ORDER BY ABS(julianday(s.effective_from) - julianday(?)) LIMIT 1",
                (underlying, kind, self.d)).fetchone()
            self.by_key[key] = row["tick_paise"] if row else None
        return self.by_key[key]


def _derivative_id(conn, r: dict, d: str, stats: LoadStats) -> int:  # noqa: ANN001
    hit = conn.execute(
        "SELECT instrument_id, source FROM instruments WHERE exchange='NFO' AND underlying=? AND kind=? "
        "AND expiry=? AND IFNULL(strike_paise, -1)=IFNULL(?, -1)",
        (r["underlying"], r["kind"], r["expiry"], r["strike_paise"])).fetchone()
    if hit is None:
        stats.new_instruments += 1
        return conn.execute(
            "INSERT INTO instruments (exchange, kind, tradingsymbol, underlying, expiry, strike_paise, "
            "first_seen, last_seen, source, actual_expiry) VALUES ('NFO', ?, NULL, ?, ?, ?, ?, ?, 'bhavcopy', ?)",
            (r["kind"], r["underlying"], r["expiry"], r["strike_paise"], d, d, r.get("actual_expiry"))).lastrowid
    if r.get("actual_expiry"):
        conn.execute("UPDATE instruments SET actual_expiry=? WHERE instrument_id=? "
                     "AND IFNULL(actual_expiry, '') <> ?", (r["actual_expiry"], hit["instrument_id"], r["actual_expiry"]))
    # A bhavcopy row proves the contract was listed that day. last_seen of Kite
    # instruments stays theirs: `instruments.sync` uses it to order dumps.
    if hit["source"] == "bhavcopy":
        conn.execute("UPDATE instruments SET first_seen=MIN(first_seen, ?), last_seen=MAX(last_seen, ?) "
                     "WHERE instrument_id=?", (d, d, hit["instrument_id"]))
    else:
        conn.execute("UPDATE instruments SET first_seen=MIN(first_seen, ?) WHERE instrument_id=?",
                     (d, hit["instrument_id"]))
    return hit["instrument_id"]


def _spec(conn, iid: int, r: dict, d: str, stats: LoadStats, hints: _TickHints) -> None:  # noqa: ANN001
    lot = r["lot_size"]
    if not lot:
        return
    current = conn.execute("SELECT lot_size FROM contract_specs WHERE instrument_id=? AND effective_from<=? "
                           "ORDER BY effective_from DESC LIMIT 1", (iid, d)).fetchone()
    if current is not None and current["lot_size"] == lot:
        return
    tick = hints.get(r["underlying"], r["kind"], iid)
    if tick is None:
        stats.specs_without_tick += 1
        return
    cur = conn.execute("INSERT OR IGNORE INTO contract_specs (instrument_id, effective_from, lot_size, "
                       "tick_paise, source) VALUES (?, ?, ?, ?, 'bhavcopy')", (iid, d, lot, tick))
    stats.spec_rows += cur.rowcount


def _load_fo(conn, d: str, rows: list[dict], stats: LoadStats) -> None:  # noqa: ANN001
    out = []
    hints = _TickHints(conn, d)
    for r in rows:
        iid = _derivative_id(conn, r, d, stats)
        _spec(conn, iid, r, d, stats, hints)
        out.append((d, iid, r["open_paise"], r["high_paise"], r["low_paise"], r["close_paise"],
                    r["last_paise"], r["prev_close_paise"], r["settle_paise"], r["underlying_paise"],
                    r["oi"], r["oi_change"], r["volume"], r["turnover_paise"], r["lot_size"]))
    conn.execute("DELETE FROM fo_eod WHERE trade_date=?", (d,))
    conn.executemany("INSERT INTO fo_eod VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", out)
    stats.rows = len(out)


def _equity_id(conn, symbol: str, d: str, stats: LoadStats) -> int:  # noqa: ANN001
    # `kind IN ('EQ','INDEX')` is the partial index's own predicate: with `kind='EQ'`
    # alone SQLite cannot use ux_instr_symbol and scans every instrument.
    hit = conn.execute("SELECT instrument_id, source, kind FROM instruments WHERE exchange='NSE' "
                       "AND tradingsymbol=? AND kind IN ('EQ','INDEX')", (symbol,)).fetchone()
    if hit is not None and hit["kind"] != "EQ":
        raise NseError(f"cash-market symbol {symbol!r} is an index in refdata")
    if hit is None:
        stats.new_instruments += 1
        return conn.execute(
            "INSERT INTO instruments (exchange, kind, tradingsymbol, first_seen, last_seen, source) "
            "VALUES ('NSE', 'EQ', ?, ?, ?, 'bhavcopy')", (symbol, d, d)).lastrowid
    extend_last = ", last_seen=MAX(last_seen, ?)" if hit["source"] == "bhavcopy" else ""
    conn.execute(f"UPDATE instruments SET first_seen=MIN(first_seen, ?){extend_last} WHERE instrument_id=?",
                 (d, d, hit["instrument_id"]) if extend_last else (d, hit["instrument_id"]))
    return hit["instrument_id"]


def _load_cm(conn, d: str, rows: list[dict], stats: LoadStats, series: list[str]) -> None:  # noqa: ANN001
    out = []
    for r in rows:
        if r["series"] not in series:
            continue
        iid = _equity_id(conn, r["symbol"], d, stats)
        out.append((d, iid, r["series"], r["open_paise"], r["high_paise"], r["low_paise"], r["close_paise"],
                    r["last_paise"], r["prev_close_paise"], r["volume"], r["turnover_paise"], r["trades"]))
    conn.execute("DELETE FROM cm_eod WHERE trade_date=?", (d,))
    conn.executemany("INSERT INTO cm_eod VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", out)
    stats.rows = len(out)


def _load_idx(conn, d: str, rows: list[dict], stats: LoadStats, aliases: dict) -> None:  # noqa: ANN001
    known = {r["tradingsymbol"].upper(): r["instrument_id"] for r in conn.execute(
        "SELECT instrument_id, tradingsymbol FROM instruments WHERE exchange='NSE' AND kind='INDEX' "
        "ORDER BY instrument_id DESC")}
    out = []
    for r in rows:
        name = r["name"].upper()
        iid = known.get(aliases.get(name, name))
        if iid is None:
            stats.unmapped.append(r["name"])
            continue
        out.append((d, iid, r["open_paise"], r["high_paise"], r["low_paise"], r["close_paise"]))
    conn.execute("DELETE FROM index_eod WHERE trade_date=?", (d,))
    conn.executemany("INSERT OR REPLACE INTO index_eod VALUES (?, ?, ?, ?, ?, ?)", out)
    stats.rows = len(out)


def _load_qtyfreeze(conn, d: str, rows: list[dict], stats: LoadStats) -> None:  # noqa: ANN001
    for r in rows:
        prev = conn.execute("SELECT freeze_qty FROM freeze_limits WHERE underlying=? AND effective_from<=? "
                            "ORDER BY effective_from DESC LIMIT 1", (r["symbol"], d)).fetchone()
        if prev is not None and prev["freeze_qty"] == r["freeze_qty"]:
            continue
        conn.execute("INSERT OR REPLACE INTO freeze_limits (underlying, effective_from, freeze_qty, source) "
                     "VALUES (?, ?, ?, 'qtyfreeze.csv')", (r["symbol"], d, r["freeze_qty"]))
        stats.freeze_changes += 1
    stats.rows = len(rows)


def load(dataset: str, day: date, parser: str, raw: bytes, cfg: dict | None = None) -> LoadStats:
    """Parse one raw file and load it in one transaction (re-loading a day replaces it)."""
    cfg = cfg or config()
    rows = nse_parsers.PARSERS[parser](raw, day)
    d, stats = day.isoformat(), LoadStats()
    with refdata.transaction() as conn:
        if dataset == "fo":
            _load_fo(conn, d, rows, stats)
        elif dataset == "cm":
            _load_cm(conn, d, rows, stats, cfg.get("cm_series", ["EQ"]))
        elif dataset == "idx":
            _load_idx(conn, d, rows, stats, {k.upper(): v.upper() for k, v in cfg.get("index_aliases", {}).items()})
        elif dataset == "qtyfreeze":
            _load_qtyfreeze(conn, d, rows, stats)
        else:
            raise NseError(f"no loader for {dataset!r}")
        instruments._link_underlyings(conn)
    return stats


# ── Final settlement prices (D-13, D-39) ─────────────────────────────────────

def settle(day: date) -> list[dict]:
    """FSP for every underlying with contracts expiring on `day` (D-39).

    A contract expires on its `actual_expiry` when NSE expired it early
    (D-41), otherwise on its `expiry`. On that day NSE's F&O bhavcopy gives
    every expiring contract of an
    underlying (future, calls, puts) one settlement price: the FSP. That is
    the source. It is cross-checked against an independent close: the index
    close (`index_eod`) for indices, the official close (`cm_eod`, EQ) for
    stocks. Stocks closed by the closing auction can legitimately differ from
    the FSP (observed 2026-10-27: 18 of 213, up to Rs 50), so a difference
    above one tick is recorded in `data_quality` and marked `mismatch`, but
    the FSP stays NSE's own. Missing inputs are reported, never guessed.
    """
    d = day.isoformat()
    out = []
    with refdata.transaction() as conn:
        index_keys = {r["key"] for r in conn.execute("SELECT key FROM index_underlyings")}
        hints = _TickHints(conn, d)
        expiring = conn.execute(
            "SELECT i.underlying, MAX(i.underlying_id) AS uid, COUNT(DISTINCT f.settle_paise) AS n, "
            "MIN(f.settle_paise) AS lo, MAX(f.settle_paise) AS hi FROM fo_eod f "
            "JOIN instruments i USING (instrument_id) "
            "WHERE f.trade_date=? AND i.exchange='NFO' AND IFNULL(i.actual_expiry, i.expiry)=? "
            "AND i.kind IN ('FUT','CE','PE') AND f.settle_paise IS NOT NULL "
            "GROUP BY i.underlying ORDER BY i.underlying", (d, d)).fetchall()
        for row in expiring:
            und, uid = row["underlying"], row["uid"]
            entry = {"underlying": und, "underlying_id": uid, "expiry": d}
            if uid is None:
                out.append({**entry, "status": "no underlying instrument"})
                continue
            conn.execute("DELETE FROM data_quality WHERE trade_date=? AND instrument_id=? AND metric IN (?, ?)",
                         (d, uid, FSP_METRIC, FSP_SPREAD_METRIC))
            if row["n"] != 1:
                conn.execute("INSERT INTO data_quality (trade_date, instrument_id, metric, value, detail) "
                             "VALUES (?, ?, ?, ?, ?)", (d, uid, FSP_SPREAD_METRIC, row["hi"] - row["lo"],
                                                         json.dumps({"distinct_settle_prices": row["n"]})))
                out.append({**entry, "status": f"{row['n']} different settle prices for expiring contracts"})
                continue
            fsp = row["lo"]
            if und in index_keys:
                close_src = "index_eod"
                hit = conn.execute("SELECT close_paise FROM index_eod WHERE trade_date=? AND instrument_id=?",
                                   (d, uid)).fetchone()
            else:
                close_src = "cm_eod"
                hit = conn.execute("SELECT close_paise FROM cm_eod WHERE trade_date=? AND instrument_id=? "
                                   "AND series='EQ'", (d, uid)).fetchone()
            check = "n/a"
            if hit is not None and hit["close_paise"] is not None:
                tick = next((t for t in (hints.get(und, k) for k in ("FUT", "CE", "PE")) if t is not None), 0)
                diff = abs(hit["close_paise"] - fsp)
                check = "ok" if diff <= tick else "mismatch"
                if check == "mismatch":
                    conn.execute("INSERT INTO data_quality (trade_date, instrument_id, metric, value, detail) "
                                 "VALUES (?, ?, ?, ?, ?)",
                                 (d, uid, FSP_METRIC, diff, json.dumps(
                                     {"fsp_paise": fsp, "close_paise": hit["close_paise"],
                                      "close_source": close_src, "tick_paise": tick}, sort_keys=True)))
            conn.execute("INSERT OR REPLACE INTO settlement_prices (underlying_id, expiry, fsp_paise, source, "
                         "cross_check) VALUES (?, ?, ?, 'fo_eod', ?)", (uid, d, fsp, check))
            out.append({**entry, "status": "ok", "fsp_paise": fsp, "source": "fo_eod",
                        "close_source": close_src, "cross_check": check})
    return out


# ── Fetch ────────────────────────────────────────────────────────────────────

@dataclass
class FetchResult:
    dataset: str
    trade_date: str
    outcome: str                      # ok | missing | error | skipped | no_source
    url: str | None = None
    http_status: int | None = None
    sha256: str | None = None
    raw_file: str | None = None
    parser: str | None = None
    rows: int = 0
    parsed_ok: bool | None = None
    detail: str = ""


def _log(conn, res: FetchResult, nbytes: int | None, parsed_ok: bool | None, now_ns: int) -> None:  # noqa: ANN001
    conn.execute(
        "INSERT OR REPLACE INTO nse_fetch_log (dataset, trade_date, url, http_status, outcome, sha256, bytes, "
        "fetched_at_ns, parser, parsed_ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (res.dataset, res.trade_date, res.url, res.http_status, res.outcome, res.sha256, nbytes, now_ns,
         res.parser, None if parsed_ok is None else int(parsed_ok)))


def logged(dataset: str, day: date) -> dict | None:
    row = refdata._connect().execute("SELECT * FROM nse_fetch_log WHERE dataset=? AND trade_date=?",
                                     (dataset, day.isoformat())).fetchone()
    return dict(row) if row else None


def fetch_one(dataset: str, day: date, http: Http, *, allow_unverified: bool = False, force: bool = False,
              cfg: dict | None = None, today: date | None = None) -> FetchResult:
    cfg = cfg or config()
    src = source_for(dataset, day, cfg)
    if src is None:
        return FetchResult(dataset, day.isoformat(), "no_source", detail=f"no {dataset} format covers {day}")
    if src.current_only:
        # The file only describes "now": it is stored under the day it was fetched.
        day = today or timeutil.ist_today()
    if not src.verified and not allow_unverified:
        raise NseError(f"{dataset} ({src.parser}) is not verified in sources.json; pass --allow-unverified")
    d = day.isoformat()
    prev = logged(dataset, day)
    if (not force and prev and prev["outcome"] == "ok" and prev["parsed_ok"] == 1
            and _raw_matches(dataset, prev["sha256"])):
        return FetchResult(dataset, d, "skipped", url=prev["url"], http_status=prev["http_status"],
                           sha256=prev["sha256"], parser=prev["parser"], parsed_ok=True, detail="already fetched")
    url = url_for(src, day, cfg)
    res = FetchResult(dataset, d, "error", url=url, parser=src.parser)
    nbytes = None
    try:
        status, body = http.get(url)
        res.http_status = status
        if status == 404:
            res.outcome, res.detail = "missing", "not published (holiday, or not yet)"
        elif status != 200:
            res.detail = f"HTTP {status}"
        else:
            nbytes = len(body)
            res.raw_file, res.sha256 = save_raw(dataset, day, src.ext, body)
            res.outcome = "ok"
            try:
                stats = load(dataset, day, src.parser, body, cfg)
                res.parsed_ok, res.rows = True, stats.rows
                bits = [f"{stats.rows} rows"]
                if stats.new_instruments:
                    bits.append(f"{stats.new_instruments} new instruments")
                if stats.spec_rows:
                    bits.append(f"{stats.spec_rows} lot-size rows")
                if stats.specs_without_tick:
                    bits.append(f"{stats.specs_without_tick} lot sizes skipped (no tick size known)")
                if stats.unmapped:
                    bits.append(f"{len(stats.unmapped)} index names not in refdata")
                if stats.freeze_changes:
                    bits.append(f"{stats.freeze_changes} freeze-limit changes")
                res.detail = ", ".join(bits)
            except nse_parsers.ParseError as e:
                res.parsed_ok, res.detail = False, f"parse failed: {e}"
    except NseError as e:
        res.detail = str(e)
    with refdata.transaction() as conn:
        _log(conn, res, nbytes, res.parsed_ok, timeutil.now_ns())
    if res.outcome == "ok" and res.parsed_ok is False:
        log.error("%s %s: %s (raw kept as %s)", dataset, d, res.detail, res.raw_file)
    return res


def fetch(day: date, datasets: tuple[str, ...] = DATASETS, *, http: Http | None = None,
          allow_unverified: bool = False, force: bool = False, today: date | None = None) -> list[FetchResult]:
    """Fetch and load one trade date, then compute that day's settlement prices."""
    cfg = config()
    http = http or Http(cfg=cfg)
    results = [fetch_one(ds, day, http, allow_unverified=allow_unverified, force=force, cfg=cfg, today=today)
               for ds in datasets]
    settle(day)
    return results


def complete(results: list[FetchResult]) -> bool:
    """True when every dataset is loaded (fresh or earlier) and parsed."""
    return all(r.outcome == "skipped" or (r.outcome == "ok" and r.parsed_ok) for r in results)


def backfill(start: date, end: date, datasets: tuple[str, ...], *, http: Http | None = None,
             allow_unverified: bool = False, progress=None) -> dict:  # noqa: ANN001
    """Every NSE trading day in [start, end]; resumable (fetched days are skipped)."""
    from stockify.data import calendar
    datasets = tuple(ds for ds in datasets if ds != "qtyfreeze")          # current only (S-6)
    cfg = config()
    http = http or Http(cfg=cfg)
    totals: dict[str, int] = {}
    for day in calendar.trading_days("FO", start, end):
        for ds in datasets:
            res = fetch_one(ds, day, http, allow_unverified=allow_unverified, cfg=cfg)
            totals[res.outcome] = totals.get(res.outcome, 0) + 1
            if progress:
                progress(res)
        settle(day)
    return totals
