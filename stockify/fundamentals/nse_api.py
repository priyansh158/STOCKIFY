"""FS 1: a small client for NSE's JSON API (www.nseindia.com/api) and static files, over `nse_files.Http`.

Politeness is the Http's: browser User-Agent and Referer, one cookie warm-up GET of https://www.nseindia.com/, at least
`min_interval_ms` (1 s) between requests, retries on 5xx. On top of that, each API endpoint is preceded once per
Client by a GET of the NSE page a browser would show first (its `warmup` page, for that page's cookies), as
nse_margin_files does by hand for the ELM reports API. Cookies expire on long runs, so a 401 / 403 runs the
warm-ups again and retries the request once before giving up.

Every answer used is kept byte for byte under `$LAB/nse/raw/fundamentals/<kind>/<name>` with a `<name>.sha256`
sidecar ("<hex>  <name>"); a different answer under the same name is kept beside it (`_r2`, `_r3` ...). Nothing
here modifies or deletes a raw file.

`Blocked`: NSE refused (HTTP 401 / 403, twice), or answered with an HTML page (its anti-bot page) where JSON, a CSV or
XML was expected. Only HTML counts: an XML body (XBRL filings) starts with '<' too and is data. A refusal is never
kept as data. Callers stop their run on it and try again later.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urlencode

from stockify import paths, timeutil
from stockify.config import loader
from stockify.data import nse_files


class Blocked(nse_files.NseError):
    """NSE refused the request (401 / 403) or sent an HTML page instead of the data asked for."""


def config() -> dict:
    return loader.load_json("fundamentals.json")


# ── Raw answers ──────────────────────────────────────────────────────────────

def _versioned(name: str, n: int) -> str:
    """`name` for version 1, else `_r<n>` before the last extension (`a.b.json` -> `a.b_r2.json`)."""
    if n == 1:
        return name
    stem, dot, ext = name.rpartition(".")
    return f"{stem}_r{n}.{ext}" if dot and stem else f"{name}_r{n}"


def _kept_digest(target, sidecar) -> str | None:  # noqa: ANN001
    """The sha256 recorded for a kept file. A sidecar that is missing, empty or garbled (a crash between the file and
    its sidecar) is rebuilt from the file's own bytes, so the same answer is never taken for a new version."""
    try:
        words = sidecar.read_text(encoding="utf-8").split()
    except OSError:
        words = []
    if words and re.fullmatch(r"[0-9a-f]{64}", words[0]):
        return words[0]
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    sidecar.write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    return digest


def save_raw(kind: str, name: str, body: bytes) -> str:
    """Keep `body` exactly as NSE sent it as `<kind>/<name>` (both one safe segment, else ValueError); returns its
    sha256. The same bytes already kept under that name are not written again; other bytes go to `_r2`, `_r3` ..."""
    if not paths.safe_segment(name, 160):
        raise ValueError(f"not a valid raw file name: {name!r}")
    folder = paths.fundamentals_raw_dir(kind)
    digest = hashlib.sha256(body).hexdigest()
    n = 1
    while True:
        file = _versioned(name, n)
        target, sidecar = folder / file, folder / (file + ".sha256")
        if not target.exists():
            part = folder / (file + ".part")
            part.write_bytes(body)
            os.replace(part, target)
            sidecar.write_text(f"{digest}  {file}\n", encoding="utf-8")
            return digest
        if _kept_digest(target, sidecar) == digest:
            return digest
        n += 1


def _looks_html(body: bytes) -> bool:
    """Whether `body` is an HTML page (NSE's anti-bot or error page): after an optional UTF-8 BOM and whitespace it
    opens with `<!doctype html` or an `<html` tag, in any case. XML (`<?xml`, `<xbrl ...`) is not HTML."""
    head = body[:256].removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    if head.startswith(b"<!doctype html"):
        return True
    return head.startswith(b"<html") and (len(head) == 5 or head[5:6] in (b">", b"/") or head[5:6].isspace())


def _default_name(day: str, params: dict) -> str:
    text = "_".join(str(params[k]) for k in sorted(params)) or "all"
    return f"{day}_{re.sub(r'[^A-Za-z0-9.-]+', '_', text).strip('_.') or 'all'}.json"


# ── Client ───────────────────────────────────────────────────────────────────

class Client:
    """`json(path, params, warmup=...)` for www.nseindia.com/api, `get(url, kind=, name=)` for static files.
    `last_sha256` is the hash of the last body kept."""

    def __init__(self, http: nse_files.Http | None = None, cfg: dict | None = None):
        self.http = http or nse_files.Http()
        self.cfg = cfg or config()
        self.base = self.cfg["api"]["base"].rstrip("/")
        self._warmed: set[str] = set()
        self.last_sha256: str | None = None

    def _warm(self, page: str) -> None:
        if page in self._warmed:
            return
        self._warmed.add(page)                     # once, even when the page fails: cookies are a courtesy
        try:
            self.http.get(page)
        except nse_files.NseError:
            pass

    def _rewarm(self, warmup: str | None) -> None:
        """Cookies expire (Akamai's within hours): the Http's homepage warm-up runs again on its next GET, and the
        page warm-up, if any, is fetched again now."""
        if hasattr(self.http, "_warmed"):
            self.http._warmed = False
        if warmup:
            self._warmed.discard(warmup)
            self._warm(warmup)

    def fetch(self, url: str, what: str = "the file", *, warmup: str | None = None) -> bytes:
        """The body of a 200 answer, not kept (the caller checks it, then keeps it with `save_raw`). On a 401 / 403
        the warm-ups run again once and the request is retried once; a second refusal raises Blocked, and so does an
        HTML body (never retried). NseError on any other status."""
        status, body = self.http.get(url)
        if status in (401, 403):
            self._rewarm(warmup)
            status, body = self.http.get(url)
        if status in (401, 403):
            raise Blocked(f"{url}: NSE refused the request (HTTP {status}, after warming up again): {body[:80]!r}")
        if status != 200:
            raise nse_files.NseError(f"{url}: HTTP {status}")
        if _looks_html(body):
            raise Blocked(f"{url}: NSE sent an HTML page (HTTP {status}) instead of {what}: {body.lstrip()[:80]!r}")
        return body

    def get(self, url: str, *, kind: str, name: str, warmup: str | None = None) -> bytes:
        """A static file (e.g. the Nifty 500 list on nsearchives), kept as `<kind>/<name>`."""
        if warmup:
            self._warm(warmup)
        body = self.fetch(url, "the file", warmup=warmup)
        self.last_sha256 = save_raw(kind, name, body)
        return body

    def json(self, path: str, params: dict, *, warmup: str, name: str | None = None) -> Any:
        """GET `<api base><path>?<params>` after warming `warmup` once; the body is kept under the kind named by the
        path's last segment (`/api/quote-equity` → `quote-equity`) as `name` (default `<IST YYYYMMDD>_<params>.json`)
        and returned parsed. Blocked on 401 / 403 / HTML; NseError on any other status or a body that is not JSON."""
        if not path.startswith("/api/"):
            raise ValueError(f"not an NSE API path: {path!r}")
        url = self.base + path + ("?" + urlencode(params) if params else "")
        self._warm(warmup)
        body = self.fetch(url, "JSON", warmup=warmup)
        try:
            doc = json.loads(body)
        except ValueError as e:
            raise nse_files.NseError(f"{url}: the answer is not JSON: {e}") from e
        day = f"{timeutil.ns_to_ist(self.http.clock.now_ns()):%Y%m%d}"
        self.last_sha256 = save_raw(path.rstrip("/").rsplit("/", 1)[-1], name or _default_name(day, params), body)
        return doc
