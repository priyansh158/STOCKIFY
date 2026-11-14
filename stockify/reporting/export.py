"""DMF 6.2: export a finished run as a reproducible zip that can never hold a secret.

Data leaves the machine here, so three rules hold: only the names in `ALLOWED` (plus the generated summary.json and,
on request, the pack files the bundle names) are ever read; every member's bytes and name are scanned for a secret
before anything is written (`check_no_secret`); and the zip is built in memory and written in one atomic step, so a
refusal leaves no file. The zip is byte-identical for the same run: members sorted, fixed timestamp, fixed level.

Memory: every member is read whole into memory, scanned, then zipped in memory. Run files are small; with
`with_packs` a pack larger than MAX_PACK_BYTES is refused (code too_large) instead of read, and the sum of all
members is capped by research.json `export.max_total_mib` (too_large, checked before each pack is read).

CSV members are exported with spreadsheet-formula cells in their free-text columns marked by a leading ' (the
stored files stay raw).

A second guard (`check_personal_data`) refuses text members that name the lab folder, the home folder, the repo
folder or the Windows user name, so a shared zip carries no personal details.
"""
from __future__ import annotations

import csv
import getpass
import io
import json
import os
import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Iterable, Mapping

from stockify import numerics, paths, research, store
from stockify.reporting import figures
from stockify.research import fileio

ALLOWED = ("bundle.json", "run.json", "manifest.json", "metrics.json", "determinism.json", "orders.csv",
           "order_events.csv", "fills.csv", "charges.csv", "positions_eod.csv", "equity.csv", "margin_daily.csv",
           "report.md", "equity.png")
# Deliberately absent: events_log.txt (free text from strategies), request.json, runner.pid, research.json,
# sensitivity.json, and anything else that happens to sit in the folder.

_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_LEVEL = 6
_SHA = re.compile(r"[0-9a-f]{64}")
_MIN_LITERAL = 4           # env KITE_API_KEY / KITE_API_SECRET values
_MIN_TOKEN_LITERAL = 8     # secret-looking values of the token file
MAX_PACK_BYTES = 512 * 1024 * 1024
_SECRET_KEYS = frozenset({"access_token", "public_token", "refresh_token", "enctoken", "request_token", "api_key",
                          "api_secret"})
_SECRET_RE = re.compile(rb"api[_-]?secret|access[_-]?token|enctoken|request[_-]?token|Bearer\s+\S+", re.IGNORECASE)
_TOKEN_FILE = "kite_token.json"


class ExportError(Exception):
    """A refusal. `code`: bad_id, not_found, not_done, pack_missing, too_large, secret or personal (the API maps
    bad_id and not_found to 404, the rest to 409)."""

    def __init__(self, message: str, code: str = "refused"):
        super().__init__(message)
        self.code = code


# ── the secret scanner (one function, used by the build and by the tests) ────────────────────────────────────────

def secret_literals() -> tuple[str, ...]:
    """The literal secret values known to this process: KITE_API_KEY and KITE_API_SECRET from the environment and
    the token file's values under secret-looking keys (access_token, public_token, refresh_token, enctoken,
    request_token, api_key, api_secret, any key containing "token" or "secret"), at least 8 characters. Other
    fields (user_name, broker, login_time) are not needles. Read only to compare; never logged or returned."""
    found: list[str] = []
    for name in ("KITE_API_KEY", "KITE_API_SECRET"):
        v = os.environ.get(name, "").strip()
        if len(v) >= _MIN_LITERAL:
            found.append(v)
    try:
        import stockify_paths
        text = (stockify_paths.state_dir() / _TOKEN_FILE).read_text(encoding="utf-8")
    except (OSError, ValueError):
        text = ""
    if text.strip():
        try:
            values = list(_secret_strings(json.loads(text)))
        except ValueError:
            values = [text.strip()]
        found.extend(v for v in values if len(v) >= _MIN_TOKEN_LITERAL)
    return tuple(dict.fromkeys(found))


def _secret_strings(node, key: str = "") -> Iterable[str]:  # noqa: ANN001
    """String values under secret-looking keys (a list inherits the key it hangs from)."""
    if isinstance(node, str):
        k = key.lower()
        if k in _SECRET_KEYS or "token" in k or "secret" in k:
            yield node.strip()
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _secret_strings(v, str(k))
    elif isinstance(node, list):
        for v in node:
            yield from _secret_strings(v, key)


def find_secret(members: Mapping[str, bytes], *, literals: Iterable[str] | None = None) -> str | None:
    """The name of the first member (sorted) whose name or bytes hold a secret literal or secret-shaped text, else
    None. Never returns or prints the matched text."""
    needles = [s.encode("utf-8") for s in (secret_literals() if literals is None else literals) if s]
    for name in sorted(members):
        blob = name.encode("utf-8", "surrogatepass") + b"\n" + bytes(members[name])
        if _SECRET_RE.search(blob) or any(n in blob for n in needles):
            return name
    return None


def check_no_secret(members: Mapping[str, bytes], *, literals: Iterable[str] | None = None) -> None:
    hit = find_secret(members, literals=literals)
    if hit is not None:
        raise ExportError(f"the export would contain something that looks like a secret: {hit}", "secret")


# ── the personal-data guard ──────────────────────────────────────────────────────────────────────────────────────

def _is_text(name: str) -> bool:
    return not (name == "equity.png" or (name.startswith("packs/") and name.endswith(".evt")))


def _folder_needles() -> list[bytes]:
    """The lab, home and repo folders, lower-cased, in every spelling a file may carry: backslashes, forward
    slashes, and the doubled backslashes of JSON text."""
    out: list[bytes] = []
    for folder in (paths.lab_dir(), Path.home(), Path(__file__).resolve().parents[2]):
        back = str(folder).rstrip("\\/").lower().replace("/", "\\")
        if len(back) < 4:
            continue
        for v in (back, back.replace("\\", "/"), back.replace("\\", "\\\\")):
            if v.encode("utf-8") not in out:
                out.append(v.encode("utf-8"))
    return out


def _user_word() -> re.Pattern | None:
    try:
        user = getpass.getuser().strip()
    except Exception:  # noqa: BLE001  — no user name known: only the folder check applies
        return None
    if len(user) < 3:
        return None
    return re.compile(rb"(?<![a-z0-9_])" + re.escape(user.lower().encode("utf-8")) + rb"(?![a-z0-9_])")


def find_personal(members: Mapping[str, bytes]) -> tuple[str, str] | None:
    """(member name, kind) of the first member (sorted) that names a local folder or the Windows user name, in its
    name or, for a text member, its bytes. Never returns the matched text."""
    folders, word = _folder_needles(), _user_word()
    for name in sorted(members):
        for blob in (name.lower().encode("utf-8", "surrogatepass"),
                     bytes(members[name]).lower() if _is_text(name) else b""):
            if any(n in blob for n in folders):
                return name, "a local folder path"
            if word is not None and word.search(blob):
                return name, "your user name"
    return None


def check_personal_data(members: Mapping[str, bytes]) -> None:
    hit = find_personal(members)
    if hit is not None:
        raise ExportError(f"the export would contain {hit[1]}: {hit[0]}", "personal")


# ── spreadsheet formulas ─────────────────────────────────────────────────────────────────────────────────────────

# Columns that hold text a strategy or the engine wrote. A cell there that starts with = + - @ (or a tab or a return)
# would run as a formula when the CSV is opened in a spreadsheet, so the export copy puts a ' in front of it.
# Numeric columns (price_paise, qty ...) are never touched, and neither is a cell that is only a number.
_FREE_TEXT_COLUMNS = frozenset({"note", "tag", "reason", "detail", "evidence"})
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"[+-]?\d+(\.\d+)?")


def neutralise_formulas(name: str, data: bytes) -> bytes:
    """`data` of the CSV member `name` with spreadsheet-formula cells in the free-text columns marked with a leading
    '. A member that is not a CSV, has no such column, or has nothing to mark comes back as the very same bytes."""
    if not name.endswith(".csv"):
        return data
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))
    except (UnicodeDecodeError, csv.Error):
        return data
    if not rows:
        return data
    cols = [i for i, h in enumerate(rows[0]) if h in _FREE_TEXT_COLUMNS]
    changed = False
    for row in rows[1:]:
        for i in cols:
            if i < len(row) and row[i].startswith(_FORMULA_START) and not _PLAIN_NUMBER.fullmatch(row[i]):
                row[i] = "'" + row[i]
                changed = True
    if not changed:
        return data
    out = io.StringIO(newline="")
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue().encode("utf-8")


# ── the build ────────────────────────────────────────────────────────────────────────────────────────────────

def _json(text: str | None, default):  # noqa: ANN001, ANN202
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def _bundle_packs(folder: Path) -> list[str]:
    """The pack hashes the bundle names, in bundle order; anything that is not a 64-hex hash is refused (it would
    become a file name)."""
    p = folder / "bundle.json"
    if not p.is_file() or p.is_symlink():
        return []
    data = _json(p.read_text(encoding="utf-8"), {})
    rows = data.get("packs", []) if isinstance(data, dict) else []
    shas = []
    for row in rows if isinstance(rows, list) else []:
        sha = row.get("sha256") if isinstance(row, dict) else None
        if not (isinstance(sha, str) and _SHA.fullmatch(sha)):
            raise ExportError("the bundle names a pack that is not a 64-digit hash", "refused")
        shas.append(sha)
    return shas


def build(run_id: str, *, with_packs: bool = False, out: Path | None = None) -> Path:
    """Write the zip of a finished run and return its path (default `paths.export_path`)."""
    try:
        folder = paths.run_path(run_id)
        target = Path(out) if out is not None else paths.export_path(run_id, with_packs)
    except ValueError:
        raise ExportError(f"not a valid run id: {str(run_id)[:60]!r}", "bad_id") from None
    row = store.get_run(run_id)
    if row is None or not folder.is_dir():
        raise ExportError(f"no test run {run_id!r} (or its folder was removed)", "not_found")
    if row.get("status") != "done":
        raise ExportError(f"run {run_id!r} is {row.get('status')!r}; only a finished run can be exported", "not_done")

    limit_mib = research.settings()["export"]["max_total_mib"]
    limit = limit_mib * 1024 * 1024
    too_big = ExportError(f"the export would be larger than {limit_mib} MiB uncompressed (the export.max_total_mib "
                          "setting in research.json)" + ("; leave the packs out" if with_packs else ""), "too_large")
    members: dict[str, bytes] = {}
    total = 0
    for name in ALLOWED:
        p = folder / name
        if p.is_file() and not p.is_symlink():
            data = neutralise_formulas(name, p.read_bytes())
            total += len(data)
            if total > limit:
                raise too_big
            members[name] = data

    pack_rows = []
    for sha in dict.fromkeys(_bundle_packs(folder)):
        pack = paths.packs_dir() / f"{sha}.evt"
        size = pack.stat().st_size if pack.is_file() else None
        pack_rows.append({"sha256": sha, "bytes": size, "included": with_packs})
        if with_packs:
            if size is None or pack.is_symlink():
                raise ExportError(f"pack {sha} named by the run is not in the pack folder", "pack_missing")
            if size > MAX_PACK_BYTES:
                raise ExportError(f"pack {sha} is larger than {MAX_PACK_BYTES // (1024 * 1024)} MiB", "too_large")
            total += size
            if total > limit:                              # before the pack is read: a huge one never enters memory
                raise too_big
            members[f"packs/{sha}.evt"] = pack.read_bytes()

    # the summary shows what the pages show (D-135): withheld figures are null with a reason; the run folder's own
    # metrics.json in the bundle is the hashed artifact and stays raw
    shown, withheld = figures.present(_json(row.get("metrics_json"), {}))
    summary = {"run_id": run_id, "config": _json(row.get("config_json"), {}), "metrics": shown, "withheld": withheld,
               "labels": _json(row.get("labels_json"), []), "family_id": row.get("family_id"), "role": row.get("role"),
               "packs": pack_rows}
    members["summary.json"] = numerics.dumps_canonical(summary).encode("utf-8")

    check_personal_data(members)
    check_no_secret(members)                               # before a single byte is written anywhere

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, _ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            z.writestr(info, members[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=_LEVEL)
    fileio.atomic_write_bytes(target, buf.getvalue())
    return target
