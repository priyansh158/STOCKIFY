"""NSE's SPAN risk-parameter files and the contract-wise ELM file, archived byte for byte (CAM 4.2, S-7).

SPAN: one zip per file, `nsccl.<YYYYMMDD>.<suffix>.zip` on the NSE archive host. Suffixes `i1` (start of day,
created the evening before the trade date), `i2`..`i4` (intraday) and `s` (end of day). Each zip holds one CME
SPAN 4.00 XML whose header carries `<created>YYYYMMDDHHMM</created>` (IST). There is no `.b` file.

ELM: the reports API, "F&O-Contract wise ELM file", a gzip of `F_AEL_OPT_CONTRACTS_<DDMMYYYY>.csv`. It needs one
cookie warm-up on the derivatives reports page and only a rolling window of recent dates exists.

NSE sometimes answers 200 with an HTML page: a body that is not a zip (SPAN) or gzip (ELM) is rejected, never kept.
Every file is checked whole before it is archived (M-1): a SPAN zip must pass every CRC (`ZipFile.testzip`) and hold
exactly one `.spn`; an ELM gzip must decompress to the end as UTF-8 CSV rows of one width (at least 7 columns, an
optional header line, at least one data row). A SPAN file's `<created>` time must also be plausible for the business
day D in its name: between 12:00 IST on the day before D's previous trading day and D+1 06:00 IST (L-4); an archived file whose created time is not is never used.

Layout under `$LAB/nse/margin` (through `stockify.paths`):

    span/<YYYY>/nsccl.<YYYYMMDD>.<suffix>.zip
    elm/<YYYY>/F_AEL_OPT_CONTRACTS_<DDMMYYYY>.csv.gz
    manifest/<YYYYMMDD>.json     {file: {url, fetched_ns, sha256, bytes, created}}

Files are never modified or deleted: a download that differs from the archived file is reported as rejected and the
archived one stays. Fetching reuses `nse_files.Http` (browser headers, throttle, retries).
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import quote

from stockify import paths, timeutil
from stockify.data import calendar, nse_files

log = logging.getLogger("stockify.data.nse_margin")

SPAN_SUFFIXES = ("i1", "i2", "i3", "i4", "s")
SPAN_HOST = "https://nsearchives.nseindia.com/archives/nsccl/span"
ELM_WARMUP_URL = "https://www.nseindia.com/all-reports-derivatives"
ELM_API = "https://www.nseindia.com/api/reports"
ELM_ARCHIVES = [{"name": "F&O-Contract wise ELM file", "type": "archives", "category": "derivatives",
                 "section": "equity"}]
SEGMENT = "FO"
ELM_WINDOW_DAYS = 120
_CREATED = re.compile(rb"<created>(\d{12})</created>")
_HEADER_BYTES = 1 << 16
ELM_MAX_BYTES = 256 * 1024 * 1024        # an ELM CSV larger than this once unpacked is refused (6 Nov 2026: ~8 MB)
ELM_MIN_COLUMNS = 7                      # instrument, symbol, expiry, strike, CE | PE | XX, 0, ELM % (S-7)
# An ELM % as NSE writes it (M-3): a plain bounded decimal, no sign, no exponent. Anything else is unusable, in the
# bundle (read_elm) and in both margin models (staircase._elm_ppb, staircase.cpp elm_ppb).
ELM_PCT = re.compile(r"[0-9]{1,3}(\.[0-9]{1,8})?")        # ASCII digits only: \d takes any Unicode digit
# The plausible created time of a SPAN file of business day D (L-4 ruling, CAM 4.5): from 12:00 IST on the calendar
# day before D's previous trading day to D+1 06:00 IST. NSE makes a Monday's i1 on the Friday night or early Saturday
# (22:47 Fri .. 01:59 Sat seen); the extra day covers a weekday holiday missing from the calendar (it holds the
# holidays from 2009 on, D-75).
CREATED_FROM = (-1, "12:00")           # days relative to the previous trading day of D
CREATED_UNTIL = (1, "06:00")


def span_name(day: date, suffix: str) -> str:
    return f"nsccl.{day:%Y%m%d}.{suffix}.zip"


def elm_name(day: date) -> str:
    return f"F_AEL_OPT_CONTRACTS_{day:%d%m%Y}.csv.gz"


def span_url(day: date, suffix: str) -> str:
    return f"{SPAN_HOST}/{span_name(day, suffix)}"


def elm_request(day: date) -> str:
    archives = quote(json.dumps(ELM_ARCHIVES, separators=(",", ":")), safe="")
    stamp = f"{day:%d}-{nse_files.MONTHS[day.month - 1].title()}-{day:%Y}"
    return f"{ELM_API}?archives={archives}&date={stamp}&type=equity&mode=single"


def _span_path(day: date, suffix: str):  # noqa: ANN202
    return _year_dir("span", day) / span_name(day, suffix)


def _elm_path(day: date):  # noqa: ANN202
    return _year_dir("elm", day) / elm_name(day)


def _year_dir(kind: str, day: date):  # noqa: ANN202
    """The year folder, not created here (reading the archive creates nothing): `_atomic_write` creates it."""
    return paths.nse_margin_dir(kind) / f"{day:%Y}"


def _manifest_path(day: date):  # noqa: ANN202
    return paths.nse_margin_dir("manifest") / f"{day:%Y%m%d}.json"


def read_manifest(day: date) -> dict:
    try:
        return json.loads(_manifest_path(day).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _atomic_write(path, data: bytes) -> None:  # noqa: ANN001
    """Write next to the target and rename over it: a crash leaves no half-written archive file.
    A leftover `<name>.tmp` is never read as an archive file (only exact names are) and is overwritten next time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _write_manifest(day: date, data: dict) -> None:
    _atomic_write(_manifest_path(day), json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))


# ── Validation ───────────────────────────────────────────────────────────────

def span_created(body: bytes) -> str | None:
    """The header `<created>` of the single `.spn` in a SPAN zip, or None when the body is not such a zip."""
    if not body.startswith(b"PK"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".spn")]
            if len(names) != 1:
                return None
            with z.open(names[0]) as f:
                head = f.read(_HEADER_BYTES)
    except (zipfile.BadZipFile, NotImplementedError, OSError):
        return None
    m = _CREATED.search(head)
    return m.group(1).decode() if m else None


def span_check(body: bytes) -> str | None:
    """`span_created` of a SPAN zip that is whole: one `.spn` member, not oversized, every CRC right (M-1). None
    for anything else: such a body is never archived."""
    from stockify.fno.span_file import MAX_MEMBER_BYTES
    created = span_created(body)
    if created is None:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            spn = [i for i in z.infolist() if i.filename.lower().endswith(".spn")]
            if len(spn) != 1 or spn[0].file_size > MAX_MEMBER_BYTES or z.testzip() is not None:
                return None
    except (zipfile.BadZipFile, NotImplementedError, OSError, EOFError, zlib.error, ValueError):
        return None
    return created


class ElmFileError(ValueError):
    """An ELM gzip that does not unpack to the end as UTF-8 text, or is too large."""


def elm_text(body: bytes) -> str:
    """The whole CSV text of an ELM gzip; ElmFileError for a truncated, corrupt, oversized or non-UTF-8 one."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as g:
            raw = g.read(ELM_MAX_BYTES + 1)
    except (OSError, EOFError, zlib.error) as e:
        raise ElmFileError(f"not a whole gzip: {type(e).__name__}: {e}") from e
    if len(raw) > ELM_MAX_BYTES:
        raise ElmFileError(f"unpacks to more than {ELM_MAX_BYTES} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ElmFileError(f"not UTF-8: {e}") from e


def elm_problem(body: bytes) -> str | None:
    """Why an ELM body cannot be archived, or None when it can (M-1): it must unpack (`elm_text`) to CSV rows of
    one width, at least ELM_MIN_COLUMNS, with at least one data row (a row whose ELM % column is a plain decimal;
    other rows, such as a header line, are allowed)."""
    try:
        text = elm_text(body)
    except ElmFileError as e:
        return str(e)
    rows = [line.split(",") for line in text.splitlines() if line.strip()]
    if not rows:
        return "no rows"
    widths = {len(r) for r in rows}
    if len(widths) != 1 or min(widths) < ELM_MIN_COLUMNS:
        return f"rows of {sorted(widths)} columns, not one width of at least {ELM_MIN_COLUMNS}"
    if not any(re.fullmatch(r"\d+(\.\d+)?", r[ELM_MIN_COLUMNS - 1].strip()) for r in rows):
        return "no data row"
    return None


def plausible(day: date, created_ns: int) -> bool:
    """Whether a SPAN file of business day `day` may have been created at `created_ns` (L-4)."""
    prev = calendar.previous_trading_day(SEGMENT, day)
    lo = timeutil.ist_ns(prev + timedelta(days=CREATED_FROM[0]), CREATED_FROM[1])
    hi = timeutil.ist_ns(day + timedelta(days=CREATED_UNTIL[0]), CREATED_UNTIL[1])
    return lo <= created_ns <= hi


def _created_ns(created: str) -> int:
    """`YYYYMMDDHHMM` (IST) to UTC epoch ns."""
    return timeutil.ist_ns(date(int(created[:4]), int(created[4:6]), int(created[6:8])),
                           f"{created[8:10]}:{created[10:12]}")


# ── Fetch ────────────────────────────────────────────────────────────────────

@dataclass
class DayResult:
    day: date
    stored: list[str] = field(default_factory=list)          # newly archived
    missing: list[str] = field(default_factory=list)         # 404: not published (or outside the ELM window)
    rejected: list[str] = field(default_factory=list)        # not a zip/gzip, no <created>, or differs from the archive
    unchanged: list[str] = field(default_factory=list)       # already archived with the same SHA-256
    errors: list[str] = field(default_factory=list)          # network trouble after retries


def have(res: DayResult, name: str) -> bool:
    return name in res.stored or name in res.unchanged


def complete(res: DayResult) -> bool:
    """The end-of-day SPAN file and the ELM file are both archived."""
    return have(res, span_name(res.day, "s")) and have(res, elm_name(res.day))


def _keep(res: DayResult, manifest: dict, name: str, target, url: str, body: bytes,  # noqa: ANN001
          created: str | None) -> None:
    """Archive one file, then record its manifest row at once (a crash later in the day keeps both)."""
    digest = hashlib.sha256(body).hexdigest()
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            res.unchanged.append(name)
            if name not in manifest:                           # archived earlier without a manifest row
                manifest[name] = {"url": url, "fetched_ns": target.stat().st_mtime_ns, "sha256": digest,
                                  "bytes": len(body), "created": created}
                _write_manifest(res.day, manifest)
        else:
            log.warning("%s differs from the archived file; the archived one is kept", name)
            res.rejected.append(name)
        return
    _atomic_write(target, body)
    manifest[name] = {"url": url, "fetched_ns": timeutil.now_ns(), "sha256": digest, "bytes": len(body),
                      "created": created}
    _write_manifest(res.day, manifest)
    res.stored.append(name)


def _fetch_span(day: date, suffix: str, http, res: DayResult, manifest: dict) -> None:  # noqa: ANN001
    name, url = span_name(day, suffix), span_url(day, suffix)
    try:
        status, body = http.get(url)
    except nse_files.NseError as e:
        log.warning("%s: %s", name, e)
        res.errors.append(name)
        return
    if status == 404:
        res.missing.append(name)
        return
    created = span_check(body) if status == 200 else None
    if created is None:
        log.warning("%s: HTTP %s, not a whole SPAN zip (%d bytes); not archived", name, status, len(body))
        res.rejected.append(name)
        return
    created_ns = _checked_created_ns(created)
    if created_ns is None or not plausible(day, created_ns):
        log.warning("%s: created %s is not plausible for business day %s; not archived", name, created, day)
        res.rejected.append(name)
        return
    _keep(res, manifest, name, _span_path(day, suffix), url, body, created)


def _fetch_elm(day: date, http, res: DayResult, manifest: dict) -> None:  # noqa: ANN001
    name, url = elm_name(day), elm_request(day)
    try:
        if not getattr(http, "_elm_warmed", False):
            http.get(ELM_WARMUP_URL)                           # cookies; the answer itself does not matter
            http._elm_warmed = True
        status, body = http.get(url)
    except nse_files.NseError as e:
        log.warning("%s: %s", name, e)
        res.errors.append(name)
        return
    if status == 404:
        res.missing.append(name)
        return
    if status != 200 or not body.startswith(b"\x1f\x8b"):
        log.warning("%s: HTTP %s, not a gzip body (%d bytes); not archived", name, status, len(body))
        res.rejected.append(name)
        return
    problem = elm_problem(body)
    if problem is not None:
        log.warning("%s: %s; not archived", name, problem)
        res.rejected.append(name)
        return
    _keep(res, manifest, name, _elm_path(day), url, body, None)


def fetch_day(day: date, http: nse_files.Http | None = None, *, span: bool = True, elm: bool = True,
              suffixes: tuple[str, ...] = SPAN_SUFFIXES) -> DayResult:
    """Fetch the day's SPAN files and ELM file; archive each valid one byte for byte; update the manifest."""
    http = http or nse_files.Http()
    res = DayResult(day)
    manifest = read_manifest(day)
    if span:
        for suffix in suffixes:
            _fetch_span(day, suffix, http, res, manifest)
    if elm:
        _fetch_elm(day, http, res, manifest)
    return res


def _archived(day: date, suffixes: tuple[str, ...]) -> bool:
    return all(_span_path(day, s).exists() for s in suffixes)


def backfill(start: date, end: date, http: nse_files.Http | None = None, *,
             suffixes: tuple[str, ...] = ("s",)) -> list[DayResult]:
    """SPAN files for every NSE trading day in [start, end]; resumable (archived days are not asked again)."""
    http = http or nse_files.Http()
    out = []
    for day in calendar.trading_days(SEGMENT, start, end):
        if _archived(day, suffixes):
            res = DayResult(day, unchanged=[span_name(day, s) for s in suffixes])
        else:
            res = fetch_day(day, http, elm=False, suffixes=suffixes)
        out.append(res)
    return out


def elm_window(http: nse_files.Http | None = None, *, today: date | None = None,
               days: int = ELM_WINDOW_DAYS) -> list[DayResult]:
    """The ELM file of every trading day back `days` days that NSE still serves (newest first); resumable."""
    http = http or nse_files.Http()
    today = today or timeutil.ist_today()
    out = []
    for day in reversed(calendar.trading_days(SEGMENT, today - timedelta(days=days), today)):
        if _elm_path(day).exists():
            out.append(DayResult(day, unchanged=[elm_name(day)]))
        else:
            out.append(fetch_day(day, http, span=False))
    return out


# ── Reading the archive (consumed by the SPAN margin model) ──────────────────

def _any_archived(day: date) -> bool:
    return any(_span_path(day, s).exists() for s in SPAN_SUFFIXES)


def eod_archived(day: date) -> bool:
    """Whether the end-of-day (`.s`) file of `day` is in the archive: NSE makes one for every day it trades, so
    the calendar audit reads its absence on a weekday as a closed exchange (D-75)."""
    return _archived(day, ("s",))


def in_force_files(day: date, skipped: list | None = None) -> list[tuple[str, int]]:
    """Archived SPAN files of trade date `day` as (path, created_ns), oldest first. A file without a readable
    created time, or whose created time is not plausible for `day` (L-4), is not used; with `skipped`, it is
    recorded there as (path, reason)."""
    out, manifest = [], read_manifest(day)
    for suffix in SPAN_SUFFIXES:
        path = _span_path(day, suffix)
        if not path.exists():
            continue
        row = manifest.get(span_name(day, suffix))
        created_ns = _checked_created_ns(row.get("created") if isinstance(row, dict) else None)
        if created_ns is None:                                 # no (valid) manifest row: read the header
            created_ns = _checked_created_ns(span_created(path.read_bytes()))
        if created_ns is None:                                 # neither readable: the file is not used
            if skipped is not None:
                skipped.append((str(path), "no readable created time"))
        elif not plausible(day, created_ns):
            if skipped is not None:
                skipped.append((str(path), f"created {timeutil.ns_to_ist(created_ns):%Y-%m-%d %H:%M} IST is not "
                                           f"plausible for business day {day} (L-4)"))
        else:
            out.append((str(path), created_ns))
    return sorted(out, key=lambda t: t[1])


def _checked_created_ns(created) -> int | None:  # noqa: ANN001
    """`_created_ns` of a `YYYYMMDDHHMM` string; None for anything else (a hand-edited or damaged manifest)."""
    if not isinstance(created, str) or not re.fullmatch(r"\d{12}", created):
        return None
    try:
        return _created_ns(created)
    except ValueError:
        return None


def previous_eod(day: date, *, max_days: int = 30) -> tuple[str, int] | None:
    """The archived end-of-day (`.s`) file of the latest trading day before `day`, looking back at most `max_days`
    trading days. The validator uses it with the default: it asks which file Kite had, and a gap in the archive is
    not a gap at NSE. Bundles use `opening_eod` instead (CAM 4.4)."""
    d = day
    for _ in range(max_days):
        d = calendar.previous_trading_day(SEGMENT, d)
        for path, created_ns in in_force_files(d):
            if path.endswith(".s.zip"):
                return path, created_ns
    return None


OPENING_MAX_EMPTY_DAYS = 5


def opening_eod(day: date, *, max_empty: int = OPENING_MAX_EMPTY_DAYS,
                skipped: list | None = None) -> tuple[str, int] | None:
    """The end-of-day file a backtest opens trade date `day` with (CAM 4.4, I-2 ruling): walk back over F&O
    calendar trading days that have NO archived SPAN file of any suffix, at most `max_empty` of them, and stop at the
    first day that has any file: its `.s` if archived (and usable), else nothing. A day without any file is one NSE
    published no file for: a holiday the calendar does not hold. The calendar holds the holidays from 2009 on
    (D-75), so the walk is a safety net (and steps over the one archive gap, 2021-10-11). A day with intraday
    files but no `.s` gives nothing (never an older file)."""
    d = day
    for _ in range(max_empty + 1):
        d = calendar.previous_trading_day(SEGMENT, d)
        if not _any_archived(d):
            continue
        found: list = []
        for path, created_ns in in_force_files(d, found):
            if path.endswith(".s.zip"):
                return path, created_ns
        if skipped is not None:
            skipped.extend(x for x in found if x[0].endswith(".s.zip"))
        return None
    return None


def usable_files(day: date, skipped: list | None = None) -> list[tuple[str, int]]:
    """Every SPAN file a backtest may use during trade date `day`, as (path, created_ns) in created order, without
    lookahead (spec §5.1, CAM 4.3): the previous NSE session's `.s` when archived (`opening_eod`; never an older one),
    then D's `i1`..`i4`. D's own `.s` never: it is published after the close. Each file applies from
    its own `created_ns`; the file in force at t is the last one created at or before t, and before the first one
    nothing is (a late `i1` does not move earlier). The previous trading day is found by `opening_eod`: days with no
    archived file at all (holidays the calendar lacks) are stepped over. With `skipped`, archived files left out for
    a bad created time are recorded there as (path, reason)."""
    found: list = []
    own = [(p, c) for p, c in in_force_files(day, found) if not p.endswith(".s.zip")]
    if skipped is not None:
        skipped.extend(x for x in found if not x[0].endswith(".s.zip"))
    opener = opening_eod(day, skipped=skipped)
    if opener is not None:
        own = [opener] + own
    return sorted(own, key=lambda t: t[1])


# ── The ELM file ─────────────────────────────────────────────────────────────

_MONTH = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10,
          "NOV": 11, "DEC": 12}


def read_elm(day: date, underlyings: set[str]) -> dict[tuple[str, tuple], Decimal | None] | None:
    """{(underlying, contract key): ELM %} from the archived `F_AEL_OPT_CONTRACTS_<day>` file; None if not archived.
    A contract key is (kind, expiry "YYYY-MM-DD", strike paise | None), as `stockify.fno.span_file` keys contracts.
    Rows: instrument, symbol, expiry DD-MMM-YYYY, strike (rupees; -1 for futures), CE | PE | XX, 0, ELM %.
    The file is untrusted data: a malformed row is skipped, and a contract listed twice with different % maps to None
    (unusable, so its legs report it missing instead of taking either value); so is a 0 %, and a % that is not a
    plain bounded decimal (ELM_PCT, M-3). A strike must be a whole number of paise (L-6). The % keeps the file's
    digits. A file that does not unpack to the end as UTF-8 raises ElmFileError (the bundle skips it, M-1)."""
    from stockify import numerics
    path = paths.nse_margin_dir("elm") / f"{day:%Y}" / elm_name(day)
    if not path.exists():
        return None
    out = {}
    for line in elm_text(path.read_bytes()).splitlines():
        p = [c.strip() for c in line.split(",")]
        if len(p) < ELM_MIN_COLUMNS or p[1] not in underlyings:
            continue
        try:
            dd, mon, yyyy = p[2].split("-")
            expiry = date(int(yyyy), _MONTH[mon.upper()], int(dd)).isoformat()
            if p[4] == "XX":
                key = ("FUT", expiry, None)
            elif p[4] in ("CE", "PE"):
                key = (p[4], expiry, numerics.to_paise(p[3]))
            else:
                continue
            if not ELM_PCT.fullmatch(p[6]):
                continue
            pct = Decimal(p[6])
            if pct <= 0:                                     # NSE's ELM % is never 0: a 0 is missing
                continue
        except (ValueError, KeyError, ArithmeticError):
            continue
        k = (p[1], key)
        if k in out and out[k] != pct:
            out[k] = None
        elif k not in out:
            out[k] = pct
    return out
