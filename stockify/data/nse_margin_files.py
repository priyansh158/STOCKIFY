"""NSE's SPAN risk-parameter files and the contract-wise ELM file, archived byte for byte (CAM 4.2, S-7).

SPAN: one zip per file, `nsccl.<YYYYMMDD>.<suffix>.zip` on the NSE archive host. Suffixes `i1` (start of day,
created the evening before the trade date), `i2`..`i4` (intraday) and `s` (end of day). Each zip holds one CME
SPAN 4.00 XML whose header carries `<created>YYYYMMDDHHMM</created>` (IST). There is no `.b` file.

ELM: the reports API, "F&O-Contract wise ELM file", a gzip of `F_AEL_OPT_CONTRACTS_<DDMMYYYY>.csv`. It needs one
cookie warm-up on the derivatives reports page and only a rolling window of recent dates exists.

NSE sometimes answers 200 with an HTML page: a body that is not a zip (SPAN) or gzip (ELM) is rejected, never kept.

Layout under `$LAB/nse/margin` (through `stockify.paths`):

    span/<YYYY>/nsccl.<YYYYMMDD>.<suffix>.zip
    elm/<YYYY>/F_AEL_OPT_CONTRACTS_<DDMMYYYY>.csv.gz
    manifest/<YYYYMMDD>.json     {file: {url, fetched_ns, sha256, bytes, created}}

Files are never modified or deleted: a download that differs from the archived file is reported as rejected and the
archived one stays. Fetching reuses `nse_files.Http` (browser headers, throttle, retries).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
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
    d = paths.nse_margin_dir(kind) / f"{day:%Y}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path(day: date):  # noqa: ANN202
    return paths.nse_margin_dir("manifest") / f"{day:%Y%m%d}.json"


def read_manifest(day: date) -> dict:
    try:
        return json.loads(_manifest_path(day).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_manifest(day: date, data: dict) -> None:
    path = _manifest_path(day)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


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
    digest = hashlib.sha256(body).hexdigest()
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            res.unchanged.append(name)
            if name not in manifest:                           # archived earlier without a manifest row
                manifest[name] = {"url": url, "fetched_ns": timeutil.now_ns(), "sha256": digest,
                                  "bytes": len(body), "created": created}
        else:
            log.warning("%s differs from the archived file; the archived one is kept", name)
            res.rejected.append(name)
        return
    target.write_bytes(body)
    manifest[name] = {"url": url, "fetched_ns": timeutil.now_ns(), "sha256": digest, "bytes": len(body),
                      "created": created}
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
    created = span_created(body) if status == 200 else None
    if created is None:
        log.warning("%s: HTTP %s, not a SPAN zip (%d bytes); not archived", name, status, len(body))
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
    _keep(res, manifest, name, _elm_path(day), url, body, None)


def fetch_day(day: date, http: nse_files.Http | None = None, *, span: bool = True, elm: bool = True,
              suffixes: tuple[str, ...] = SPAN_SUFFIXES) -> DayResult:
    """Fetch the day's SPAN files and ELM file; archive each valid one byte for byte; update the manifest."""
    http = http or nse_files.Http()
    res = DayResult(day)
    manifest = read_manifest(day)
    before = json.dumps(manifest, sort_keys=True)
    if span:
        for suffix in suffixes:
            _fetch_span(day, suffix, http, res, manifest)
    if elm:
        _fetch_elm(day, http, res, manifest)
    if json.dumps(manifest, sort_keys=True) != before:
        _write_manifest(day, manifest)
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

def in_force_files(day: date) -> list[tuple[str, int]]:
    """Archived SPAN files of trade date `day` as (path, created_ns), oldest first."""
    out, manifest = [], read_manifest(day)
    for suffix in SPAN_SUFFIXES:
        path = _span_path(day, suffix)
        if not path.exists():
            continue
        created = manifest.get(span_name(day, suffix), {}).get("created")
        if created is None:                                    # archived without a manifest row: read the header
            created = span_created(path.read_bytes())
        if created is not None:
            out.append((str(path), _created_ns(created)))
    return sorted(out, key=lambda t: t[1])


def previous_eod(day: date, *, max_days: int = 30) -> tuple[str, int] | None:
    """The archived end-of-day (`.s`) file of the latest trading day before `day`."""
    d = day
    for _ in range(max_days):
        d = calendar.previous_trading_day(SEGMENT, d)
        for path, created_ns in in_force_files(d):
            if path.endswith(".s.zip"):
                return path, created_ns
    return None
