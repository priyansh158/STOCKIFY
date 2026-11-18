"""GK 5.7 (M5.13): the verified backup of the stores that cannot be rebuilt. It only reads the lab."""
from __future__ import annotations

import builtins
import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.daemon.test_journal import clock_records, writer

from stockify import __main__ as cli, backup, paths
from stockify.config import loader

DAY = date(2026, 11, 11)


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(root: Path, *, ignore_wal_files: bool = False) -> dict[str, tuple[int, int]]:
    """Every file under root: relative path -> (size, mtime_ns). `ignore_wal_files` leaves out `*-shm` and `*-wal`:
    a read-only connection to a WAL store may make them beside it (stockify.paths.readonly_uri), and nothing else."""
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()
            and not (ignore_wal_files and p.name.endswith(("-shm", "-wal")))}


def make_db(path: Path, rows: int = 5, wal: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    con.commit()
    return con


def count(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        con.close()


class BackupCase(SandboxedDBTest):
    """Starts from a brand-new empty state dir, so no other test's files (a stray `-shm`) are in the lab tree."""
    db_modules = ()

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(dir=support.SANDBOX_ROOT, prefix="backup-target-")
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / "bk"
        self.lab = paths.lab_dir()
        self.addCleanup(shutil.rmtree, self.lab, ignore_errors=True)
        make_db(paths.refdata_db()).close()
        make_db(paths.lab_db(), rows=3).close()
        make_db(paths.desk_db(), rows=2).close()          # the holdings desk's store (HD 12: bounded, so backed up)
        self.fin = paths.journal_file("s1", DAY)
        w = writer(self.fin)
        w.append(clock_records(5))
        w.close(finalise=True)
        (paths.journals_dir("s1") / "20261111.bundle.json").write_text("{}", encoding="utf-8")
        self.arch = paths.ticks_archive_dir(DAY) / "part-0.parquet"
        self.arch.write_bytes(b"parquet-bytes" * 100)
        self.raw = paths.nse_raw_dir("fo_bhav") / "fo_20261111.csv"
        self.raw.write_text("a,b\n1,2\n", encoding="utf-8")
        self.elm = paths.nse_margin_dir("elm") / "ael_20261111.csv.gz"
        self.elm.write_bytes(b"elm-bytes")
        self.span = paths.nse_margin_dir("span") / "span_20261111.zip"
        self.span.write_bytes(b"span-bytes")

    def run_backup(self, **cfg):
        real = loader.load_json

        def fake(name, *a, **k):
            doc = real(name, *a, **k)
            if name == "storage.json":
                doc = {**doc, "backup": {"target": None, "include_span": False, "keep_snapshots": 7, **cfg}}
            return doc
        with mock.patch.object(loader, "load_json", fake):
            return backup.run(self.target, day=DAY)

    def backed_up(self) -> list[str]:
        return sorted(p.relative_to(self.target).as_posix() for p in self.target.rglob("*") if p.is_file())


class CopyTests(BackupCase):
    def test_backup_copies_and_verifies(self):
        before = tree(self.lab)
        res = self.run_backup()
        self.assertEqual(res.errors, [])
        self.assertGreater(res.copied, 0)
        self.assertEqual(res.skipped, 0)
        self.assertGreater(res.bytes, 0)
        snap = self.target / "snapshots" / "2026-11-11"
        self.assertEqual(count(snap / "refdata.sqlite"), 5)
        self.assertEqual(count(snap / "lab.sqlite"), 3)
        self.assertEqual(count(snap / "desk.sqlite"), 2)
        files = self.target / "files"
        for src in (self.fin, Path(str(self.fin) + ".crc"), self.arch, self.raw, self.elm,
                    paths.journals_dir("s1") / "20261111.bundle.json"):
            dst = files / src.relative_to(self.lab)
            self.assertEqual(sha(dst), sha(src), src.name)
        manifest = json.loads((self.target / "manifest.json").read_text(encoding="utf-8"))
        rel = self.arch.relative_to(self.lab).as_posix()
        self.assertEqual(manifest["files"][rel], {"size": self.arch.stat().st_size, "sha256": sha(self.arch)})
        self.assertEqual(tree(self.lab), before, "the lab was changed")

    def test_second_run_skips_unchanged(self):
        first = self.run_backup()
        second = self.run_backup()
        self.assertEqual(second.errors, [])
        self.assertEqual(second.copied, 3)                      # the three databases are snapshotted each run
        self.assertEqual(second.skipped, first.copied - 3)
        self.raw.write_text("a,b\n1,3\n", encoding="utf-8")
        third = self.run_backup()
        self.assertEqual(third.copied, 4)                        # the changed file, and the three databases
        self.assertEqual(sha(self.target / "files" / self.raw.relative_to(self.lab)), sha(self.raw))

    def test_a_backed_up_file_that_went_missing_is_copied_again(self):
        self.run_backup()
        dst = self.target / "files" / self.arch.relative_to(self.lab)
        dst.unlink()
        res = self.run_backup()
        self.assertTrue(dst.exists())
        self.assertEqual(res.errors, [])

    def test_a_copy_that_does_not_match_is_an_error_and_not_recorded(self):
        real = backup._sha256_file

        def lying(path):
            digest = real(path)
            if "part-0.parquet" in Path(path).name and str(self.target) in str(path):
                return "0" * 64
            return digest
        with mock.patch.object(backup, "_sha256_file", lying):
            res = self.run_backup()
        self.assertTrue(any("part-0.parquet" in e and "verif" in e for e in res.errors), res.errors)
        manifest = json.loads((self.target / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn(self.arch.relative_to(self.lab).as_posix(), manifest["files"])
        again = self.run_backup()
        self.assertEqual(again.errors, [])                       # next honest run copies it afresh
        self.assertEqual(sha(self.target / "files" / self.arch.relative_to(self.lab)), sha(self.arch))


class SqliteTests(BackupCase):
    def test_live_wal_db_copy_is_consistent(self):
        con = make_db(paths.lab_db(), rows=0, wal=True)
        con.executemany("INSERT INTO t (v) VALUES (?)", [(f"w{i}",) for i in range(50)])
        con.commit()                                             # committed, still only in the -wal file
        con.execute("BEGIN")
        con.execute("INSERT INTO t (v) VALUES ('uncommitted')")  # an open transaction: must not appear
        self.addCleanup(con.close)
        self.assertTrue(Path(str(paths.lab_db()) + "-wal").exists())
        wal_before = Path(str(paths.lab_db()) + "-wal").stat().st_size
        res = self.run_backup()
        self.assertEqual(res.errors, [])
        copy = self.target / "snapshots" / "2026-11-11" / "lab.sqlite"
        check = sqlite3.connect(copy)
        try:
            self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            values = {r[0] for r in check.execute("SELECT v FROM t")}
        finally:
            check.close()
        self.assertEqual(len(values), 50 + 3)                    # 3 rows from setUp's make_db plus the 50 committed
        self.assertNotIn("uncommitted", values)
        self.assertEqual(Path(str(paths.lab_db()) + "-wal").stat().st_size, wal_before)
        self.assertFalse(any(n.endswith("-wal") or n.endswith("-shm") for n in self.backed_up()))

    def test_the_sources_are_opened_read_only(self):
        seen = []
        real = sqlite3.connect

        def spy(database, *a, **k):
            seen.append((str(database), k.get("uri", False)))
            return real(database, *a, **k)
        with mock.patch.object(sqlite3, "connect", spy):
            self.run_backup()
        lab_opens = [s for s in seen if any(n in s[0] for n in ("refdata.sqlite", "lab.sqlite", "desk.sqlite"))
                     if str(self.target) not in s[0]]
        self.assertEqual(len(lab_opens), 3)
        for name, uri in lab_opens:
            self.assertTrue(uri and "mode=ro" in name, name)

    def test_a_missing_database_is_noted_not_an_error(self):
        paths.lab_db().unlink()
        res = self.run_backup()
        self.assertEqual(res.errors, [])
        self.assertTrue(any("lab.sqlite" in n for n in res.notes), res.notes)


class SelectionTests(BackupCase):
    def test_elm_archive_included_span_optional(self):
        res = self.run_backup()
        names = self.backed_up()
        self.assertIn("files/nse/margin/elm/ael_20261111.csv.gz", names)
        self.assertNotIn("files/nse/margin/span/span_20261111.zip", names)
        self.assertEqual(res.errors, [])
        res = self.run_backup(include_span=True)
        self.assertIn("files/nse/margin/span/span_20261111.zip", self.backed_up())
        self.assertEqual(res.errors, [])

    def test_never_touches_landing_or_packs(self):
        landing = paths.ticks_landing_file(DAY)
        make_db(landing, wal=True).close()
        pack = paths.packs_dir() / "abc.evt"
        pack.write_bytes(b"pack")
        before = tree(self.lab, ignore_wal_files=True)
        touched = []
        real_open, real_connect = builtins.open, sqlite3.connect

        def spy_open(file, *a, **k):
            touched.append(str(file))
            return real_open(file, *a, **k)

        def spy_connect(database, *a, **k):
            touched.append(str(database))
            return real_connect(database, *a, **k)
        with mock.patch.object(builtins, "open", spy_open), mock.patch.object(sqlite3, "connect", spy_connect), \
                mock.patch.object(Path, "read_bytes", autospec=True,
                                  side_effect=lambda self_, *a, **k: touched.append(str(self_)) or real_open(self_, "rb").read()):
            res = self.run_backup()
        self.assertEqual(res.errors, [])
        for name in touched:
            self.assertNotIn("landing", name)
            self.assertNotIn("packs", name)
        self.assertEqual([n for n in self.backed_up() if "landing" in n or "packs" in n or "abc.evt" in n], [])
        self.assertEqual(tree(self.lab, ignore_wal_files=True), before)


class JournalTests(BackupCase):
    def open_journal(self, sid="s2"):
        path = paths.journal_file(sid, DAY)
        w = writer(path)
        w.append(clock_records(8))
        w.close(finalise=False)           # fsynced and closed, never finalised: an open journal
        return path

    def test_sidecars_travel_with_the_journal(self):
        torn = Path(str(self.fin) + ".torn")
        torn.write_bytes(b"torn-bytes")
        damaged = Path(str(self.fin) + ".damaged2")
        damaged.write_bytes(b"damaged-bytes")
        res = self.run_backup()
        self.assertEqual(res.errors, [])
        for side in (torn, damaged, Path(str(self.fin) + ".crc")):
            self.assertEqual(sha(self.target / "files" / side.relative_to(self.lab)), sha(side), side.name)

    def test_an_open_journal_is_copied_consistently(self):
        path = self.open_journal()
        res = self.run_backup()
        self.assertEqual(res.errors, [])
        dst = self.target / "files" / path.relative_to(self.lab)
        self.assertEqual(sha(dst), sha(path))
        self.assertEqual(sha(Path(str(dst) + ".crc")), sha(Path(str(path) + ".crc")))
        from stockify.daemon import journal
        self.assertEqual(journal.read(dst)[2], 0)

    def test_an_open_journal_is_copied_sidecar_first_and_verified_against_it(self):
        path = self.open_journal()
        order = []
        real = backup._read_snapshot

        def spy(p):
            order.append(Path(p).name)
            return real(p)
        with mock.patch.object(backup, "_read_snapshot", spy):
            self.run_backup()
        mine = [n for n in order if n.startswith(path.name)]
        self.assertEqual(mine, [path.name + ".crc", path.name])

    def test_an_open_journal_that_never_verifies_is_skipped_and_said(self):
        path = self.open_journal()
        real = backup._read_snapshot
        crc_name = path.name + ".crc"

        def stale_sidecar(p):
            data = real(p)
            return b"" if Path(p).name == crc_name else data           # the sidecar is gone: nothing vouches for the records
        with mock.patch.object(backup, "_read_snapshot", stale_sidecar):
            res = self.run_backup()
        self.assertEqual(res.errors, [])
        self.assertGreaterEqual(res.skipped, 1)
        self.assertTrue(any("open journal" in n and path.name in n for n in res.notes), res.notes)
        self.assertNotIn(path.relative_to(self.lab).as_posix(),
                         [n.removeprefix("files/") for n in self.backed_up()])
        self.assertTrue(path.exists())

    def test_an_open_journal_with_a_damaged_record_in_the_middle_is_skipped_and_said(self):
        path = self.open_journal()
        raw = bytearray(path.read_bytes())
        raw[64 + 40 * 3 + 20] ^= 0xFF                      # inside the fourth record (sizes are fixed: a clock is 40+ bytes)
        path.write_bytes(bytes(raw))
        res = self.run_backup()
        self.assertEqual(res.errors, [])
        self.assertTrue(any("open journal" in n and path.name in n for n in res.notes), res.notes)
        self.assertFalse((self.target / "files" / path.relative_to(self.lab)).exists())

    def test_an_open_journal_one_record_ahead_of_its_sidecar_is_trimmed_and_verified(self):
        path = self.open_journal()
        real = backup._read_snapshot
        crc_name = path.name + ".crc"

        def lag_one(p):
            data = real(p)
            return data[:-4] if Path(p).name == crc_name else data
        with mock.patch.object(backup, "_read_snapshot", lag_one):
            res = self.run_backup()
        self.assertEqual(res.errors, [])
        from stockify.daemon import journal
        header, records, torn = journal.read(self.target / "files" / path.relative_to(self.lab))
        self.assertEqual((torn, len(records) > 0), (0, True))
        self.assertTrue(any("one record" in n or "trimmed" in n for n in res.notes), res.notes)
        again = self.run_backup()
        self.assertEqual(again.errors, [])

    def test_the_writer_is_not_locked_out(self):
        path = paths.journal_file("s3", DAY)
        w = writer(path)
        self.addCleanup(w.close, finalise=False)
        w.append(clock_records(3))
        res = self.run_backup()
        self.assertEqual(res.errors, [])
        w.append(clock_records(2, t0=5_000))                  # still appendable after the backup


class TargetTests(BackupCase):
    def test_refused_targets(self):
        repo = Path(backup.__file__).resolve().parent.parent
        for bad in (self.lab, self.lab / "journals", repo, repo / "docs", self.lab / "new_dir"):
            with self.subTest(target=str(bad)):
                with self.assertRaises(backup.BackupRefused) as ctx:
                    backup.run(bad, day=DAY)
                self.assertTrue(str(ctx.exception))
        file_target = Path(self.tmp.name) / "afile"
        file_target.write_text("x", encoding="utf-8")
        with self.assertRaises(backup.BackupRefused):
            backup.run(file_target, day=DAY)
        self.assertFalse((self.lab / "new_dir").exists())

    def test_the_refusal_names_the_reason(self):
        with self.assertRaisesRegex(backup.BackupRefused, "inside the lab"):
            backup.run(self.lab / "x", day=DAY)
        repo = Path(backup.__file__).resolve().parent.parent
        with self.assertRaisesRegex(backup.BackupRefused, "repo"):
            backup.run(repo, day=DAY)


class ExpandTargetTests(BackupCase):
    def test_home_and_env_vars_expand(self):
        home = Path(self.tmp.name) / "home"
        with support.isolated_env({"USERPROFILE": str(home), "HOME": str(home), "BK_ROOT": str(home / "r")}):
            self.assertEqual(backup.expand_target("~/STOCKIFY-backup"), home / "STOCKIFY-backup")
            self.assertEqual(backup.expand_target("%BK_ROOT%/x"), home / "r" / "x")
            self.assertEqual(backup.expand_target(r"D:\plain"), Path(r"D:\plain"))

    def test_an_unset_variable_is_refused(self):
        with support.isolated_env({}):
            with self.assertRaises(backup.BackupRefused):
                backup.expand_target("%STOCKIFY_NO_SUCH_VAR%/bk")

    def test_the_lab_is_still_refused_after_expansion(self):
        with support.isolated_env({"LABVAR": str(self.lab)}):
            with self.assertRaisesRegex(backup.BackupRefused, "lab"):
                backup.run("%LABVAR%/inner", day=DAY)
        with support.isolated_env({"USERPROFILE": str(self.lab), "HOME": str(self.lab)}):
            with self.assertRaisesRegex(backup.BackupRefused, "lab"):
                backup.run("~/STOCKIFY-backup", day=DAY)

    def test_run_accepts_a_variable_target(self):
        with support.isolated_env({"BKT": str(self.target)}):
            res = self.run_backup_to("%BKT%")
        self.assertFalse(res.errors)
        self.assertTrue((self.target / "snapshots" / f"{DAY:%Y-%m-%d}" / "lab.sqlite").is_file())

    def run_backup_to(self, target):
        return backup.run(target, day=DAY)


class PruneTests(BackupCase):
    def make(self, *names):
        for n in names:
            d = self.target / "snapshots" / n
            d.mkdir(parents=True)
            (d / "lab.sqlite").write_bytes(b"old")

    def snaps(self):
        return sorted(p.name for p in (self.target / "snapshots").iterdir())

    def test_oldest_beyond_keep_are_removed(self):
        self.make("2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04")
        res = self.run_backup(keep_snapshots=3)
        self.assertEqual(res.pruned, ["2026-11-01", "2026-11-02"])
        self.assertEqual(len(self.snaps()), 3)
        self.assertIn(f"{DAY:%Y-%m-%d}", self.snaps())

    def test_files_and_manifest_survive_and_others_untouched(self):
        self.make("2026-10-01", "not-a-date", "2026-13-45")
        (self.target / "snapshots" / "notes.txt").write_text("x", encoding="utf-8")
        self.run_backup(keep_snapshots=1)
        self.assertEqual(self.snaps(), [f"{DAY:%Y-%m-%d}", "2026-13-45", "not-a-date", "notes.txt"])
        self.assertTrue((self.target / "manifest.json").is_file())
        self.assertTrue((self.target / "files" / "nse/raw/fo_bhav/fo_20261111.csv").is_file())

    def test_a_second_run_the_same_day_prunes_nothing_more(self):
        self.make("2026-11-01")
        self.run_backup(keep_snapshots=2)
        res = self.run_backup(keep_snapshots=2)
        self.assertEqual(res.pruned, [])

    def test_nothing_is_pruned_when_a_snapshot_fails(self):
        self.make("2026-11-01", "2026-11-02")
        paths.lab_db().write_bytes(b"this is not a database" * 50)
        res = self.run_backup(keep_snapshots=1)
        self.assertTrue(res.errors)
        self.assertEqual(res.pruned, [])
        self.assertIn("2026-11-01", self.snaps())

    def test_a_snapshot_dated_after_the_run_day_is_kept_as_newest(self):
        self.make("2099-01-01", "2026-01-01")
        res = self.run_backup(keep_snapshots=2)
        self.assertEqual(res.pruned, ["2026-01-01"])
        self.assertIn(f"{DAY:%Y-%m-%d}", self.snaps())


class ConfigTests(unittest.TestCase):
    def test_storage_ships_a_home_relative_target(self):
        doc = loader.load_json("storage.json")
        self.assertEqual(doc["backup"], {"target": "~/STOCKIFY-backup", "include_span": False, "keep_snapshots": 7})

    def test_keep_snapshots_is_bounded(self):
        for bad in (0, -1, 366, 1.5, "7", True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(loader.ConfigError):
                    loader.load_json("storage.json", self.write(
                        {"backup": {"target": None, "include_span": False, "keep_snapshots": bad}}))
        for good in (1, 365):
            loader.load_json("storage.json", self.write(
                {"backup": {"target": None, "include_span": False, "keep_snapshots": good}}))

    def write(self, doc: dict) -> Path:
        tmp = tempfile.TemporaryDirectory(dir=support.SANDBOX_ROOT)
        self.addCleanup(tmp.cleanup)
        base = json.loads((loader.CONFIG_DIR / "storage.json").read_text(encoding="utf-8"))
        base.update(doc)
        (Path(tmp.name) / "storage.json").write_text(json.dumps(base), encoding="utf-8")
        return Path(tmp.name)

    def test_unknown_keys_are_refused(self):
        for doc in ({"backup": {"target": None, "include_span": False, "keep_snapshots": 7, "extra": 1}},
                    {"backup": {"target": None, "include_spam": True}},
                    {"retention": 3}):
            with self.subTest(doc=doc):
                with self.assertRaises(loader.ConfigError):
                    loader.load_json("storage.json", self.write(doc))

    def test_wrong_types_are_refused(self):
        for bad in ({"target": 5, "include_span": False, "keep_snapshots": 7},
                    {"target": "", "include_span": False, "keep_snapshots": 7},
                    {"target": None, "include_span": "yes", "keep_snapshots": 7}, {"target": None},
                    {"target": None, "include_span": False}, "x"):
            with self.subTest(bad=bad):
                with self.assertRaises(loader.ConfigError):
                    loader.load_json("storage.json", self.write({"backup": bad}))

    def test_a_target_and_the_span_switch_are_accepted(self):
        doc = loader.load_json("storage.json", self.write({"backup": {"target": "D:\\bk", "include_span": True, "keep_snapshots": 3}}))
        self.assertEqual(doc["backup"]["target"], "D:\\bk")


class CliTests(BackupCase):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["backup", "run", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_no_target_exits_with_a_clear_message(self):
        real = loader.load_json

        def null_target(name, *a, **k):
            doc = real(name, *a, **k)
            return {**doc, "backup": {**doc["backup"], "target": None}} if name == "storage.json" else doc
        with mock.patch.object(loader, "load_json", null_target):
            code, out, err = self.cli()
        self.assertEqual(code, 2)
        self.assertIn("backup.target", err)
        self.assertFalse(self.target.exists())

    def test_target_flag_runs_the_backup(self):
        code, out, err = self.cli("--target", str(self.target))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("copied", out)
        self.assertTrue((self.target / "manifest.json").exists())

    def test_a_refused_target_exits_2(self):
        code, out, err = self.cli("--target", str(self.lab / "x"))
        self.assertEqual(code, 2)
        self.assertIn("inside the lab", err)

    def test_errors_exit_1(self):
        with mock.patch.object(backup, "run", return_value=backup.BackupResult(1, 0, 10, ["boom"])):
            code, out, err = self.cli("--target", str(self.target))
        self.assertEqual(code, 1)
        self.assertIn("boom", err)


if __name__ == "__main__":
    unittest.main()
