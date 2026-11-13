"""DMF 6.2: export a finished run as a reproducible zip that can never hold a secret (logic, CLI, API, U-10)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import random
import re
import unittest
import zipfile
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest
from tests.stockify.research.fixtures import make_run

import stockify.store as store
import stockify_paths
from stockify import __main__ as cli
from stockify import paths
from stockify.reporting import export

SECRET_NOTE = "Bearer abc123"
SECRET_PATTERN = rb"(?i)api[_-]?secret|access[_-]?token|enctoken|request[_-]?token|Bearer\s+\S+"


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def add_files(run_id: str, *, note: str | None = None, packs: tuple[str, ...] = ()) -> Path:
    """The files of a finished run beyond make_run's four, a bundle naming `packs`, and the files that never leave."""
    folder = paths.run_dir(run_id)
    header = "order_id,symbol,qty" + (",note" if note is not None else "")
    row = "1,NSE:A,10" + (f",{note}" if note is not None else "")
    bundle = json.dumps({"packs": [{"sha256": s, "path": f"packs/{s}.evt"} for s in packs]})
    for name, text in {"orders.csv": f"{header}\n{row}\n", "fills.csv": "fill_id,qty\n1,10\n",
                       "report.md": "# run\n", "determinism.json": "{}", "bundle.json": bundle}.items():
        (folder / name).write_text(text, encoding="utf-8")
    (folder / "equity.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01")
    (folder / "events_log.txt").write_text("strategy says: hello\n", encoding="utf-8")
    (folder / "request.json").write_text("{}", encoding="utf-8")
    (folder / "runner.pid").write_text("123", encoding="utf-8")
    for s in packs:
        (paths.packs_dir() / f"{s}.evt").write_bytes(b"EVT\x00" + s.encode())
    return folder


class ExportBase(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        self.sha = _sha("pack-1")
        make_run("r1", "fam", with_folder=True)
        self.folder = add_files("r1", packs=(self.sha,))

    def names(self, path: Path) -> list[str]:
        with zipfile.ZipFile(path) as z:
            return z.namelist()


class ExportLogicTests(ExportBase):
    def test_zip_holds_the_allowed_files_and_summary(self):
        out = export.build("r1")
        self.assertEqual(out, paths.export_path("r1", False))
        present = {n for n in export.ALLOWED if (self.folder / n).exists()}
        self.assertEqual(set(self.names(out)), present | {"summary.json"})
        self.assertEqual(self.names(out), sorted(self.names(out)))
        with zipfile.ZipFile(out) as z:
            s = json.loads(z.read("summary.json"))
        self.assertEqual(set(s), {"run_id", "config", "metrics", "labels", "family_id", "role", "packs"})
        self.assertEqual((s["run_id"], s["family_id"], s["role"]), ("r1", "fam", "trial"))

    def test_packs_excluded_by_default_with_hashes_listed(self):
        with zipfile.ZipFile(export.build("r1")) as z:
            self.assertFalse([n for n in z.namelist() if n.startswith("packs/")])
            s = json.loads(z.read("summary.json"))
        self.assertEqual(s["packs"], [{"sha256": self.sha, "bytes": 4 + 64, "included": False}])

    def test_with_packs_includes_them(self):
        out = export.build("r1", with_packs=True)
        self.assertEqual(out, paths.export_path("r1", True))
        with zipfile.ZipFile(out) as z:
            self.assertEqual(z.read(f"packs/{self.sha}.evt"), b"EVT\x00" + self.sha.encode())
            self.assertTrue(json.loads(z.read("summary.json"))["packs"][0]["included"])

    def test_zip_is_byte_identical_for_the_same_run(self):
        a = export.build("r1", with_packs=True).read_bytes()
        paths.export_path("r1", True).unlink()
        self.assertEqual(export.build("r1", with_packs=True).read_bytes(), a)
        with zipfile.ZipFile(io.BytesIO(a)) as z:
            self.assertTrue(all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in z.infolist()))

    def test_events_log_and_request_json_are_never_exported(self):
        names = set(self.names(export.build("r1", with_packs=True)))
        for n in ("events_log.txt", "request.json", "runner.pid", "research.json", "sensitivity.json"):
            self.assertNotIn(n, names)

    def test_a_planted_unexpected_file_in_the_folder_is_not_exported(self):
        (self.folder / "notes.txt").write_text("hi", encoding="utf-8")
        (self.folder / "sub").mkdir()
        (self.folder / "sub" / "orders.csv").write_text("x", encoding="utf-8")
        names = self.names(export.build("r1"))
        self.assertNotIn("notes.txt", names)
        self.assertFalse([n for n in names if "/" in n])

    def test_running_or_failed_run_refused(self):
        for status in ("running", "failed", "queued"):
            make_run(f"s-{status}", None, status=status, with_folder=True)
            with self.assertRaises(export.ExportError) as ctx:
                export.build(f"s-{status}")
            self.assertEqual(ctx.exception.code, "not_done")
            self.assertFalse(paths.export_path(f"s-{status}", False).exists())

    def test_unknown_or_pruned_run_refused_not_a_traceback(self):
        with self.assertRaises(export.ExportError) as ctx:
            export.build("nope")
        self.assertEqual(ctx.exception.code, "not_found")
        make_run("pruned", None, with_folder=False)
        with self.assertRaises(export.ExportError) as ctx:
            export.build("pruned")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_path_traversal_ids_refused(self):
        for bad in ("..", "../x", "a/b", "a\\b", "C:\\x", "", "x" * 200, "a\r\nb"):
            with self.assertRaises(export.ExportError, msg=bad) as ctx:
                export.build(bad)
            self.assertEqual(ctx.exception.code, "bad_id")

    def test_missing_pack_file_refused_only_with_packs(self):
        (paths.packs_dir() / f"{self.sha}.evt").unlink()
        export.build("r1")
        with self.assertRaises(export.ExportError):
            export.build("r1", with_packs=True)

    def test_a_hostile_pack_name_in_the_bundle_is_refused(self):
        (self.folder / "bundle.json").write_text(
            json.dumps({"packs": [{"sha256": "../../x", "path": "x"}]}), encoding="utf-8")
        with self.assertRaises(export.ExportError):
            export.build("r1", with_packs=True)

    def test_a_directory_named_like_an_allowed_file_is_skipped(self):
        (self.folder / "charges.csv").mkdir()
        self.assertNotIn("charges.csv", self.names(export.build("r1")))

    def test_out_path_is_honoured(self):
        out = paths.exports_dir() / "mine.zip"
        self.assertEqual(export.build("r1", out=out), out)
        self.assertTrue(out.exists())


class ExportCliTests(ExportBase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["export", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_prints_the_path(self):
        code, out, _ = self.run_cli("r1")
        self.assertEqual((code, out.strip()), (0, str(paths.export_path("r1", False))))
        code, out, _ = self.run_cli("r1", "--with-packs")
        self.assertEqual((code, out.strip()), (0, str(paths.export_path("r1", True))))

    def test_refusals_exit_1(self):
        code, _, err = self.run_cli("nope")
        self.assertEqual(code, 1)
        self.assertIn("nope", err)


class ExportApiTests(ExportBase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from server.main import app
        self.client = TestClient(app, base_url=support.API_BASE_URL)

    def test_download_and_404_409(self):
        r = self.client.get("/api/tests/r1/export")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/zip")
        self.assertEqual(r.headers["content-disposition"], 'attachment; filename="r1.zip"')
        self.assertEqual(r.content, export.build("r1").read_bytes())
        r = self.client.get("/api/tests/r1/export", params={"with_packs": "true"})
        self.assertEqual(r.headers["content-disposition"], 'attachment; filename="r1-with-packs.zip"')
        self.assertIn(f"packs/{self.sha}.evt", zipfile.ZipFile(io.BytesIO(r.content)).namelist())
        self.assertEqual(self.client.get("/api/tests/nope/export").status_code, 404)
        self.assertEqual(self.client.get("/api/tests/../export").status_code, 404)
        make_run("busy", None, status="running", with_folder=True)
        self.assertEqual(self.client.get("/api/tests/busy/export").status_code, 409)

    def test_a_secret_in_a_note_is_a_409_with_no_text_echoed(self):
        add_files("r1", note=SECRET_NOTE)
        r = self.client.get("/api/tests/r1/export")
        self.assertEqual(r.status_code, 409)
        self.assertNotIn("abc123", r.text)


class SecretScanTests(unittest.TestCase):
    def test_patterns_and_literals(self):
        for text in (b"Bearer abc", b"x api_secret y", b"API-SECRET", b"AccessToken", b"access-token",
                     b"ENCTOKEN=1", b"request_token", b"requesttoken", b"bearer\txyz"):
            self.assertEqual(export.find_secret({"m": text}, literals=()), "m", text)
        self.assertEqual(export.find_secret({"m": b"hello LITERAL-9 x"}, literals=("LITERAL-9",)), "m")
        self.assertEqual(export.find_secret({"aaaenctoken": b"fine"}, literals=()), "aaaenctoken")
        h = _sha("x").encode()
        self.assertIsNone(export.find_secret({"m": h + b" " + h, "packs/" + h.decode() + ".evt": h}, literals=()))

    def test_error_never_shows_the_match(self):
        with self.assertRaises(export.ExportError) as ctx:
            export.check_no_secret({"orders.csv": b"note,Bearer SUPERSECRETVALUE"}, literals=())
        self.assertIn("orders.csv", str(ctx.exception))
        self.assertNotIn("SUPERSECRETVALUE", str(ctx.exception))


class ExportSecretsTests(ExportBase):
    """U-10: with fake secrets planted everywhere, no export holds one, and one that would is refused."""

    def setUp(self):
        super().setUp()
        rnd = random.Random(7).getrandbits(48)
        self.key, self.secret, self.token = f"KEY-{rnd:x}", f"SEKRET-{rnd:x}", f"TOK-{rnd:x}-ACCESS"
        env = support.isolated_env({"KITE_API_SECRET": self.secret, "KITE_API_KEY": self.key}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        (stockify_paths.state_dir() / "kite_token.json").write_text(
            json.dumps({"access_token": self.token, "user_name": "Test"}), encoding="utf-8")
        (self.folder / "sibling.txt").write_text(f"{self.key} {self.secret} {self.token}", encoding="utf-8")
        (self.folder / "events_log.txt").write_text(f"{self.key} {self.secret} Bearer {self.token}", encoding="utf-8")

    def test_export_contains_no_secret(self):
        for with_packs in (False, True):
            with zipfile.ZipFile(export.build("r1", with_packs=with_packs)) as z:
                for n in z.namelist():
                    blob = n.encode() + b"\n" + z.read(n)
                    for lit in (self.key, self.secret, self.token):
                        self.assertNotIn(lit.encode(), blob, n)
                    self.assertIsNone(re.search(SECRET_PATTERN, blob), n)
                    self.assertIsNone(export.find_secret({n: z.read(n)}, literals=export.secret_literals()), n)

    def test_literals_include_env_and_token_file(self):
        lits = export.secret_literals()
        for v in (self.key, self.secret, self.token):
            self.assertIn(v, lits)

    def test_a_note_with_a_bearer_or_the_key_is_refused_and_leaves_no_zip(self):
        for note in (SECRET_NOTE, f"see {self.key}", self.secret, self.token, "api_secret=1"):
            add_files("r1", note=note, packs=(self.sha,))
            with self.assertRaises(export.ExportError) as ctx:
                export.build("r1")
            self.assertIn("looks like a secret", str(ctx.exception))
            self.assertIn("orders.csv", str(ctx.exception))
            self.assertNotIn(note, str(ctx.exception))
            self.assertEqual(list(paths.exports_dir().iterdir()), [], note)

    def test_a_secret_in_a_pack_or_the_summary_is_refused(self):
        (paths.packs_dir() / f"{self.sha}.evt").write_bytes(b"xx " + self.secret.encode())
        export.build("r1")                                     # packs left out: not scanned, not shipped
        with self.assertRaises(export.ExportError):
            export.build("r1", with_packs=True)
        self.assertEqual(list(paths.exports_dir().iterdir()), [paths.export_path("r1", False)])
        make_run("cfg", None, with_folder=True)
        store.update_run("cfg", config_json=json.dumps({"name": f"n-{self.key}"}))
        with self.assertRaises(export.ExportError):
            export.build("cfg")


class ExportPropertyTests(ExportBase):
    """Seeded random folders: only allowed files plus summary.json; same bytes twice; a planted secret anywhere in
    an allowed file is refused."""
    ITER = 40

    def test_random_folders(self):
        rnd = random.Random(20261112)
        pool = list(export.ALLOWED) + ["events_log.txt", "request.json", "runner.pid", "research.json", "x.txt",
                                       "orders.csv.bak", "a b", "..x", "bundle.json.old"]
        for i in range(self.ITER):
            run_id = f"p{i}"
            make_run(run_id, None, with_folder=True)
            folder = paths.run_dir(run_id)
            for name in rnd.sample(pool, rnd.randint(0, len(pool))):
                body = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(1, 200)))
                body = re.sub(SECRET_PATTERN, b"z", body)
                body = body.replace(b"ecret", b"z").replace(b"earer", b"z")
                (folder / name).write_bytes(body)
            (folder / "bundle.json").write_bytes(b"{}")
            out = export.build(run_id)
            allowed_here = {n for n in export.ALLOWED if (folder / n).is_file()}
            self.assertEqual(set(self.names(out)), allowed_here | {"summary.json"}, i)
            first = out.read_bytes()
            out.unlink()
            self.assertEqual(export.build(run_id).read_bytes(), first)
            victim = rnd.choice(sorted(allowed_here))
            data = (folder / victim).read_bytes()
            cut = rnd.randint(0, len(data))
            planted = rnd.choice([b"Bearer q1", b"access_token", b"ApiSecret", b"request-token", b"EncToken"])
            (folder / victim).write_bytes(data[:cut] + planted + data[cut:])
            out.unlink()
            with self.assertRaises(export.ExportError):
                export.build(run_id)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
