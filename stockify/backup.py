"""GK 5.7 (M5.13): a verified backup of the stores that cannot be rebuilt. It only ever reads the lab.

What is backed up, under the target folder:

* `snapshots/<day>/refdata.sqlite`, `lab.sqlite`, `desk.sqlite` (the holdings desk's grades and daily views; a
  few rows per holding per day) and `fundamentals.sqlite` (the screener's universe and parsed filings):
  copied with `sqlite3.Connection.backup()` from a read-only connection, so a live WAL database is copied
  consistently (committed rows only), and then checked with `PRAGMA integrity_check`. One snapshot folder per day; a second run on the same day replaces that day's.
* After a run whose snapshots all verified, the OLDEST snapshot folders beyond `storage.json` -> `backup.keep_snapshots`
  (default 7) are removed (D-147): only folders named as a date, never the run's own day, never after a failed snapshot.
  `files/` and `manifest.json` are never pruned.
* `files/<path under the lab>`: `journals/`, `ticks/archive/`, `nse/raw/` (with the screener's raw NSE answers in
  `nse/raw/fundamentals/`) and the ELM archive (`nse/margin/elm`: NSE keeps a rolling window only), copied
  incrementally. A file whose size and SHA-256 equal the entry in `manifest.json` (and whose copy is still there) is
  skipped. `storage.json` → `backup.include_span`
  adds the SPAN files, which can be fetched again (default off).
* A journal has its `.crc` checksum file and any `.torn` / `.damaged[N]` files beside it, and they travel with it. An
  open journal (not finalised) is being appended to, so it is read once into memory, its checksum file first, then
  the journal, and the whole records are checked against that checksum file with `journal.read`. A record the
  checksum file does not yet cover at the tail is left out of the copy and said so; a journal that does not verify
  is skipped (and said so), never copied as it is. Nothing is ever locked against the writer.

Never read: the tick landing files (the recorder may be writing them) and the packs (rebuildable). Nothing is
deleted, moved or changed in the lab. Every copy is read back and its SHA-256 compared with what was read from the
source: a mismatch is an error in the result and the copy is not recorded in the manifest.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from stockify import paths
from stockify.config import loader
from stockify.daemon import journal

log = logging.getLogger(__name__)

MANIFEST_SCHEMA = "stockify.backup/1"
DATABASES = ("refdata.sqlite", "lab.sqlite", "desk.sqlite",      # desk.sqlite: the holdings desk (D-152)
             "fundamentals.sqlite",                           # the fundamentals screener (FS 1)
             "watchlist.sqlite")                              # the watched stocks and their alerts (CW 2)
_CHUNK = 1 << 20
_OPEN_JOURNAL_TRIES = 3
DEFAULT_KEEP = 7
_REPO = Path(__file__).resolve().parent.parent


class BackupRefused(ValueError):
    """The target folder is not one a backup may be written to."""


@dataclass
class BackupResult:
    copied: int = 0                                   # files and database snapshots written this run
    skipped: int = 0                                  # files already in the backup unchanged, and open journals left out
    bytes: int = 0                                    # bytes written
    errors: list[str] = field(default_factory=list)  # a copy that failed or did not verify
    notes: list[str] = field(default_factory=list)   # what was left out or trimmed, and why (not errors)
    pruned: list[str] = field(default_factory=list)  # old snapshot folders removed (names, oldest first)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _read_snapshot(path: Path) -> bytes:
    """A file's bytes, read once. Opening for read never locks a Windows writer out."""
    with open(path, "rb") as f:
        return f.read()


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def expand_target(raw: str | os.PathLike) -> Path:
    """`target` as written in storage.json, with a leading `~` (home) and `%VAR%` environment variables
    expanded. A variable that is not set is refused rather than left as a literal folder name."""
    text = os.path.expandvars(os.path.expanduser(os.fspath(raw)))
    if re.search(r"%[^%\/]+%", text) or text.startswith("~"):
        raise BackupRefused(f"backup target {raw} uses a variable or ~ that is not set")
    return Path(text)


def check_target(target: Path) -> Path:
    """The target resolved, or BackupRefused: not the lab (or inside it, or holding it), not the repo, not a file."""
    resolved = Path(target).resolve()
    lab = paths.lab_dir().resolve()
    if _inside(resolved, lab):
        raise BackupRefused(f"backup target {target} is inside the lab ({lab}): a backup there is not a second copy")
    if _inside(lab, resolved):
        raise BackupRefused(f"backup target {target} holds the lab ({lab}): pick a separate folder")
    if _inside(resolved, _REPO):
        raise BackupRefused(f"backup target {target} is the repo or inside it ({_REPO}): pick a folder outside it")
    if resolved.exists() and not resolved.is_dir():
        raise BackupRefused(f"backup target {target} is a file, not a folder")
    return resolved


class _Run:
    def __init__(self, target: Path, day: date, include_span: bool, keep_snapshots: int = DEFAULT_KEEP) -> None:
        self.keep_snapshots = keep_snapshots
        self.snapshots_ok = 0
        self.snapshots_failed = False
        self.target = target
        self.lab = paths.lab_dir()
        self.day = day
        self.include_span = include_span
        self.res = BackupResult()
        self.manifest = self._load_manifest()

    # -- manifest ---------------------------------------------------------------------------------------------

    def _load_manifest(self) -> dict[str, dict]:
        path = self.target / "manifest.json"
        if not path.exists():
            return {}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            files = doc["files"]
            if doc.get("schema") != MANIFEST_SCHEMA or not isinstance(files, dict):
                raise ValueError("unexpected shape")
            return files
        except (OSError, ValueError, KeyError) as e:
            self.res.notes.append(f"manifest.json could not be read ({e}): every file is copied afresh")
            return {}

    def _save_manifest(self) -> None:
        path = self.target / "manifest.json"
        tmp = path.with_name("manifest.json.part")
        tmp.write_text(json.dumps({"schema": MANIFEST_SCHEMA, "files": self.manifest}, indent=1, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, path)

    # -- helpers ----------------------------------------------------------------------------------------------

    def _rel(self, src: Path) -> str:
        return src.relative_to(self.lab).as_posix()

    def _dest(self, rel: str) -> Path:
        return self.target / "files" / rel

    def _is_current(self, rel: str, size: int, digest: str) -> bool:
        entry = self.manifest.get(rel)
        dst = self._dest(rel)
        try:
            return (entry == {"size": size, "sha256": digest} and dst.is_file() and dst.stat().st_size == size)
        except OSError:
            return False

    def _record(self, rel: str, size: int, digest: str) -> None:
        self.manifest[rel] = {"size": size, "sha256": digest}
        self.res.copied += 1
        self.res.bytes += size

    def _verify(self, part: Path, size: int, digest: str, rel: str) -> bool:
        try:
            ok = part.stat().st_size == size and _sha256_file(part) == digest
        except OSError as e:
            self.res.errors.append(f"{rel}: the copy could not be read back to verify it: {e}")
            return False
        if not ok:
            self.res.errors.append(f"{rel}: verification failed: the copy does not match what was read from the lab")
        return ok

    # -- plain files ------------------------------------------------------------------------------------------

    def copy_file(self, src: Path) -> None:
        rel = self._rel(src)
        dst = self._dest(rel)
        part = dst.with_name(dst.name + ".part")
        try:
            size = src.stat().st_size
            entry = self.manifest.get(rel)
            if entry and entry.get("size") == size and dst.is_file() and dst.stat().st_size == size \
                    and _sha256_file(src) == entry.get("sha256"):
                self.res.skipped += 1
                return
            dst.parent.mkdir(parents=True, exist_ok=True)
            h, n = hashlib.sha256(), 0
            with open(src, "rb") as fin, open(part, "wb") as fout:
                while chunk := fin.read(_CHUNK):
                    h.update(chunk)
                    n += len(chunk)
                    fout.write(chunk)
                fout.flush()
                os.fsync(fout.fileno())
            if self._verify(part, n, h.hexdigest(), rel):
                os.replace(part, dst)
                self._record(rel, n, h.hexdigest())
            else:
                part.unlink(missing_ok=True)
        except OSError as e:
            self.res.errors.append(f"{rel}: {e}")
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass

    # -- journals ---------------------------------------------------------------------------------------------

    def copy_open_journal(self, src: Path) -> None:
        """Sidecar first, then the journal, each read once; verified with journal.read on a scratch copy."""
        rel = self._rel(src)
        dst = self._dest(rel)
        crc_src = Path(str(src) + ".crc")
        why = "no attempt was made"
        for _ in range(_OPEN_JOURNAL_TRIES):
            try:
                crc = _read_snapshot(crc_src) if crc_src.exists() else b""
                data = _read_snapshot(src)
                dst.parent.mkdir(parents=True, exist_ok=True)
                part = dst.with_name(dst.name + ".part")
                try:
                    part.write_bytes(data)
                    if crc:
                        Path(str(part) + ".crc").write_bytes(crc)
                    _, records, torn = journal.read(part)
                finally:
                    part.unlink(missing_ok=True)
                    Path(str(part) + ".crc").unlink(missing_ok=True)
            except journal.JournalError as e:
                why = str(e)
                continue
            except OSError as e:
                self.res.errors.append(f"{rel}: {e}")
                return
            if torn:
                data = data[:journal.HEADER_SIZE + len(records)]
                self.res.notes.append(f"open journal {rel}: {torn} bytes at its end (a record the checksum file does "
                                      "not cover yet) were trimmed from the copy; they stay in the lab")
            self._store_open_journal(rel, dst, data, crc)
            return
        self.res.skipped += 1
        self.res.notes.append(f"open journal {rel} skipped: it did not verify against its checksum file after "
                              f"{_OPEN_JOURNAL_TRIES} tries ({why}); the next run tries again")

    def _store_open_journal(self, rel: str, dst: Path, data: bytes, crc: bytes) -> None:
        digest = hashlib.sha256(data).hexdigest()
        crc_rel = rel + ".crc"
        crc_digest = hashlib.sha256(crc).hexdigest()
        if self._is_current(rel, len(data), digest) and (not crc or self._is_current(crc_rel, len(crc), crc_digest)):
            self.res.skipped += 1
            return
        pieces = [(rel, dst, data, digest)]
        if crc:
            pieces.insert(0, (crc_rel, self._dest(crc_rel), crc, crc_digest))          # the sidecar first
        for name, path, blob, want in pieces:
            part = path.with_name(path.name + ".part")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(part, "wb") as f:
                    f.write(blob)
                    f.flush()
                    os.fsync(f.fileno())
                if self._verify(part, len(blob), want, name):
                    os.replace(part, path)
                    self._record(name, len(blob), want)
                else:
                    part.unlink(missing_ok=True)
                    return
            except OSError as e:
                self.res.errors.append(f"{name}: {e}")
                return

    def copy_tree(self, root: Path, *, journals: bool = False) -> None:
        files = sorted(p for p in root.rglob("*") if p.is_file())
        handled: set[Path] = set()
        if journals:
            for p in files:
                if p.suffix != ".evt":
                    continue
                try:
                    final = journal.is_finalised(p)
                except journal.JournalError as e:
                    self.res.notes.append(f"{self._rel(p)} is not a readable journal ({e}); copied as it is")
                    continue
                if not final:
                    handled.add(p)
                    handled.add(Path(str(p) + ".crc"))
                    self.copy_open_journal(p)
        for p in files:
            if p not in handled:
                self.copy_file(p)

    # -- databases --------------------------------------------------------------------------------------------

    def snapshot_db(self, src: Path) -> None:
        if not src.is_file():
            self.res.notes.append(f"{src.name} does not exist in the lab: nothing to back up")
            return
        folder = self.target / "snapshots" / f"{self.day:%Y-%m-%d}"
        dst = folder / src.name
        part = folder / (src.name + ".part")
        name = f"snapshots/{self.day:%Y-%m-%d}/{src.name}"
        source = dest = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            part.unlink(missing_ok=True)
            source = sqlite3.connect(paths.readonly_uri(src), uri=True, timeout=30)
            dest = sqlite3.connect(part)
            source.backup(dest)
            verdict = dest.execute("PRAGMA integrity_check").fetchone()[0]
            dest.close()
            dest = None
            if verdict != "ok":
                part.unlink(missing_ok=True)
                self.snapshots_failed = True
                self.res.errors.append(f"{name}: verification failed: integrity_check says {verdict!r}")
                return
            size = part.stat().st_size
            os.replace(part, dst)
            self.res.copied += 1
            self.res.bytes += size
            self.snapshots_ok += 1
        except (sqlite3.Error, OSError) as e:
            self.snapshots_failed = True
            self.res.errors.append(f"{name}: {e}")
        finally:
            for con in (source, dest):
                if con is not None:
                    con.close()
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass

    def prune_snapshots(self) -> None:
        """Remove the oldest `snapshots/<date>` folders beyond keep_snapshots, only after today's verified."""
        if self.snapshots_failed or not self.snapshots_ok:
            return
        root = self.target / "snapshots"
        dated: list[tuple[date, Path]] = []
        try:
            for p in root.iterdir():
                try:
                    d = datetime.strptime(p.name, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if p.name != f"{d:%Y-%m-%d}" or p.is_symlink() or os.path.isjunction(p) or not p.is_dir():
                    continue
                dated.append((d, p))
        except OSError as e:
            self.res.notes.append(f"snapshots could not be listed for pruning: {e}")
            return
        dated.sort()
        lab = self.lab.resolve()
        for d, p in dated[:max(0, len(dated) - self.keep_snapshots)]:
            real = p.resolve()
            if d == self.day or not _inside(real, root.resolve()) or _inside(real, lab) or _inside(real, _REPO):
                continue
            try:
                shutil.rmtree(p)
            except OSError as e:
                self.res.notes.append(f"old snapshot {p.name} could not be removed: {e}")
                continue
            self.res.pruned.append(p.name)
            log.info("backup: pruned old snapshot %s (keeping %d)", p.name, self.keep_snapshots)

    def run(self) -> BackupResult:
        self.target.mkdir(parents=True, exist_ok=True)
        try:
            for name in DATABASES:
                self.snapshot_db(self.lab / name)
            self.prune_snapshots()
            self.copy_tree(paths.journals_dir(), journals=True)
            self.copy_tree(paths.ticks_archive_dir())
            self.copy_tree(paths.nse_raw_dir())
            self.copy_tree(paths.nse_margin_dir("elm"))
            if self.include_span:
                self.copy_tree(paths.nse_margin_dir("span"))
        finally:
            try:
                self._save_manifest()
            except OSError as e:
                self.res.errors.append(f"manifest.json: {e}")
        return self.res


def run(target: Path, *, day: date) -> BackupResult:
    """Back the lab up to `target` (see the module doc). Raises BackupRefused for a target that may not be used."""
    resolved = check_target(expand_target(target))
    cfg = loader.load_json("storage.json").get("backup") or {}
    try:
        res = _Run(resolved, day, bool(cfg.get("include_span", False)),
                   int(cfg.get("keep_snapshots", DEFAULT_KEEP))).run()
    except OSError as e:
        raise BackupRefused(f"backup target {target} cannot be written: {e}") from e
    log.info("backup to %s: %d copied, %d skipped, %d bytes, %d errors, %d old snapshots pruned", resolved, res.copied,
             res.skipped, res.bytes, len(res.errors), len(res.pruned))
    return res
