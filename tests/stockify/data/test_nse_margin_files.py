"""NSE SPAN and ELM margin-file archive (CAM 4.2): fetch, validate, keep byte for byte, manifest."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import unittest
import zipfile
from datetime import date
from urllib.parse import unquote

import tests.support as support  # noqa: F401  — must precede project imports

from stockify import paths, timeutil
from stockify.data import nse_margin_files as M

DAY = date(2026, 11, 9)          # a Monday
FRI = date(2026, 11, 6)
SAT = date(2026, 11, 7)


def span_zip(day: date, suffix: str, created: str = "202611082026") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        # A fixed member time: the zip's bytes must not depend on the wall clock (a second build compares equal).
        z.writestr(zipfile.ZipInfo(f"nsccl.{day:%Y%m%d}.{suffix}.spn", date_time=(2026, 11, 9, 0, 0, 0)),
                   f'<?xml version="1.0"?><spanFile><fileFormat>4.00</fileFormat><created>{created}</created>'
                   f"<pointInTime/></spanFile>")
    return buf.getvalue()


def elm_gz(day: date) -> bytes:
    return gzip.compress(f"OPTIDX,NIFTY,2026-11-10,24000,CE,0,2.0\n".encode())


class FakeHttp:
    """Answers `.get(url)` from a {url fragment: (status, body)} table; 404 otherwise."""

    def __init__(self, routes: dict[str, tuple[int, bytes]] | None = None):
        self.routes = routes or {}
        self.urls: list[str] = []

    def get(self, url: str) -> tuple[int, bytes]:
        self.urls.append(url)
        for fragment, answer in self.routes.items():
            if fragment in url:
                return answer
        return 404, b'{"error":"not found"}'


def span_url(day: date, suffix: str) -> str:
    return M.span_url(day, suffix)


class MarginFilesTest(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])

    def routes(self, day: date = DAY, *, i1=True, i2=True, s=True, elm=True) -> dict:
        out: dict = {}
        if i1:
            out[f"nsccl.{day:%Y%m%d}.i1.zip"] = (200, span_zip(day, "i1", "202611082026"))
        if i2:
            out[f"nsccl.{day:%Y%m%d}.i2.zip"] = (200, span_zip(day, "i2", "202611091130"))
        if s:
            out[f"nsccl.{day:%Y%m%d}.s.zip"] = (200, span_zip(day, "s", "202611091655"))
        if elm:
            out["api/reports"] = (200, elm_gz(day))
        return out

    def archived(self, day: date, suffix: str, created: str) -> None:
        """Put a SPAN file in the archive through the module itself."""
        http = FakeHttp({f"nsccl.{day:%Y%m%d}.{suffix}.zip": (200, span_zip(day, suffix, created))})
        M.fetch_day(day, http, elm=False, suffixes=(suffix,))


class UrlTests(unittest.TestCase):
    def test_span_url(self):
        self.assertEqual(M.span_url(DAY, "i1"),
                         "https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.20261109.i1.zip")

    def test_elm_request_carries_exact_archive_name_and_date(self):
        url = M.elm_request(DAY)
        self.assertTrue(url.startswith("https://www.nseindia.com/api/reports?archives="))
        self.assertTrue(url.endswith("&date=09-Nov-2026&type=equity&mode=single"))
        archives = unquote(url.split("archives=")[1].split("&date=")[0])
        self.assertEqual(json.loads(archives), [{"name": "F&O-Contract wise ELM file", "type": "archives",
                                                  "category": "derivatives", "section": "equity"}])

    def test_margin_dir_kinds(self):
        for kind in ("span", "elm", "manifest"):
            support.assert_sandboxed(paths.nse_margin_dir(kind))
        self.assertEqual(paths.nse_margin_dir("span").name, "span")
        with self.assertRaises(ValueError):
            paths.nse_margin_dir("../x")


class FetchDayTests(MarginFilesTest):
    def test_fetch_day_stores_zip_and_gz_and_manifest(self):
        routes = self.routes()
        res = M.fetch_day(DAY, FakeHttp(routes))
        span_dir = paths.nse_margin_dir("span") / "2026"
        elm_dir = paths.nse_margin_dir("elm") / "2026"
        self.assertEqual((span_dir / "nsccl.20261109.i1.zip").read_bytes(), routes["nsccl.20261109.i1.zip"][1])
        self.assertEqual((span_dir / "nsccl.20261109.s.zip").read_bytes(), routes["nsccl.20261109.s.zip"][1])
        gz = (elm_dir / "F_AEL_OPT_CONTRACTS_09112026.csv.gz").read_bytes()
        self.assertEqual(gz, routes["api/reports"][1])
        self.assertEqual(sorted(res.stored), ["F_AEL_OPT_CONTRACTS_09112026.csv.gz", "nsccl.20261109.i1.zip",
                                              "nsccl.20261109.i2.zip", "nsccl.20261109.s.zip"])
        self.assertEqual(sorted(res.missing), ["nsccl.20261109.i3.zip", "nsccl.20261109.i4.zip"])
        self.assertEqual(res.rejected, [])
        manifest = json.loads((paths.nse_margin_dir("manifest") / "20261109.json").read_text(encoding="utf-8"))
        i1 = manifest["nsccl.20261109.i1.zip"]
        self.assertEqual(i1["sha256"], hashlib.sha256(routes["nsccl.20261109.i1.zip"][1]).hexdigest())
        self.assertEqual((i1["created"], i1["bytes"], i1["url"]),
                         ("202611082026", len(routes["nsccl.20261109.i1.zip"][1]), span_url(DAY, "i1")))
        self.assertGreater(i1["fetched_ns"], 0)
        elm = manifest["F_AEL_OPT_CONTRACTS_09112026.csv.gz"]
        self.assertIsNone(elm["created"])
        self.assertEqual(elm["sha256"], hashlib.sha256(gz).hexdigest())

    def test_elm_is_warmed_up_once_then_requested(self):
        http = FakeHttp(self.routes())
        M.fetch_day(DAY, http)
        M.fetch_day(DAY, http)
        warm = [u for u in http.urls if u == "https://www.nseindia.com/all-reports-derivatives"]
        self.assertEqual(len(warm), 1)
        first_elm = next(i for i, u in enumerate(http.urls) if "api/reports" in u)
        self.assertLess(http.urls.index(warm[0]), first_elm)

    def test_html_body_with_status_200_is_rejected(self):
        routes = self.routes(i2=False)
        routes["nsccl.20261109.i2.zip"] = (200, b"<!DOCTYPE html><html>Access Denied</html>")
        res = M.fetch_day(DAY, FakeHttp(routes))
        self.assertEqual(res.rejected, ["nsccl.20261109.i2.zip"])
        self.assertNotIn("nsccl.20261109.i2.zip", res.stored)
        self.assertFalse((paths.nse_margin_dir("span") / "2026" / "nsccl.20261109.i2.zip").exists())
        manifest = json.loads((paths.nse_margin_dir("manifest") / "20261109.json").read_text(encoding="utf-8"))
        self.assertNotIn("nsccl.20261109.i2.zip", manifest)

    def test_html_instead_of_gzip_elm_is_rejected(self):
        routes = self.routes(elm=False)
        routes["api/reports"] = (200, b"<html>blocked</html>")
        res = M.fetch_day(DAY, FakeHttp(routes))
        self.assertEqual(res.rejected, ["F_AEL_OPT_CONTRACTS_09112026.csv.gz"])
        self.assertFalse((paths.nse_margin_dir("elm") / "2026").exists()
                         and any((paths.nse_margin_dir("elm") / "2026").iterdir()))

    def test_zip_without_a_created_stamp_is_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("nsccl.20261109.i2.spn", "<spanFile/>")
        routes = {"nsccl.20261109.i2.zip": (200, buf.getvalue())}
        res = M.fetch_day(DAY, FakeHttp(routes), elm=False)
        self.assertEqual(res.rejected, ["nsccl.20261109.i2.zip"])

    def test_404_is_missing_not_error(self):
        res = M.fetch_day(DAY, FakeHttp({}))
        self.assertEqual(res.stored, [])
        self.assertEqual(sorted(res.missing), sorted(
            [f"nsccl.20261109.{s}.zip" for s in M.SPAN_SUFFIXES] + ["F_AEL_OPT_CONTRACTS_09112026.csv.gz"]))
        self.assertEqual(res.rejected, [])

    def test_refetch_same_bytes_is_idempotent(self):
        http = FakeHttp(self.routes())
        M.fetch_day(DAY, http)
        manifest_path = paths.nse_margin_dir("manifest") / "20261109.json"
        before = manifest_path.read_text(encoding="utf-8")
        target = paths.nse_margin_dir("span") / "2026" / "nsccl.20261109.s.zip"
        mtime = target.stat().st_mtime_ns
        res = M.fetch_day(DAY, http)
        self.assertEqual(res.stored, [])
        self.assertIn("nsccl.20261109.s.zip", res.unchanged)
        self.assertEqual(manifest_path.read_text(encoding="utf-8"), before)
        self.assertEqual(target.stat().st_mtime_ns, mtime)

    def test_a_changed_file_never_overwrites_the_archive(self):
        M.fetch_day(DAY, FakeHttp(self.routes()))
        other = self.routes()
        other["nsccl.20261109.s.zip"] = (200, span_zip(DAY, "s", "202611091700"))
        res = M.fetch_day(DAY, FakeHttp(other))
        self.assertEqual(res.rejected, ["nsccl.20261109.s.zip"])
        kept = (paths.nse_margin_dir("span") / "2026" / "nsccl.20261109.s.zip").read_bytes()
        self.assertEqual(kept, self.routes()["nsccl.20261109.s.zip"][1])

    def test_a_crash_between_files_keeps_manifest_rows_of_stored_files(self):
        class Crashing(FakeHttp):
            def get(self, url):
                if ".i3.zip" in url:
                    raise RuntimeError("power cut")
                return super().get(url)
        with self.assertRaises(RuntimeError):
            M.fetch_day(DAY, Crashing(self.routes()))
        manifest = json.loads((paths.nse_margin_dir("manifest") / "20261109.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest), ["nsccl.20261109.i1.zip", "nsccl.20261109.i2.zip"])
        fetched = manifest["nsccl.20261109.i1.zip"]["fetched_ns"]
        M.fetch_day(DAY, FakeHttp(self.routes()))                      # the next run keeps the real fetch time
        again = json.loads((paths.nse_margin_dir("manifest") / "20261109.json").read_text(encoding="utf-8"))
        self.assertEqual(again["nsccl.20261109.i1.zip"]["fetched_ns"], fetched)

    def test_a_failed_write_never_becomes_the_archived_file(self):
        from unittest.mock import patch
        target = paths.nse_margin_dir("span") / "2026" / "nsccl.20261109.s.zip"
        with patch("stockify.data.nse_margin_files.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                M.fetch_day(DAY, FakeHttp(self.routes()), elm=False, suffixes=("s",))
        self.assertFalse(target.exists())
        res = M.fetch_day(DAY, FakeHttp(self.routes()), elm=False, suffixes=("s",))      # retry stores it
        self.assertEqual(res.stored, ["nsccl.20261109.s.zip"])
        self.assertEqual(target.read_bytes(), self.routes()["nsccl.20261109.s.zip"][1])

    def test_a_leftover_tmp_file_is_not_an_archive_file(self):
        year = paths.nse_margin_dir("span") / "2026"
        year.mkdir(parents=True, exist_ok=True)
        (year / "nsccl.20261109.s.zip.tmp").write_bytes(b"PK partial")
        self.assertEqual(M.in_force_files(DAY), [])
        self.assertEqual(M.backfill(DAY, DAY, FakeHttp({}))[0].missing, ["nsccl.20261109.s.zip"])   # still asked for
        res = M.fetch_day(DAY, FakeHttp(self.routes()), elm=False, suffixes=("s",))
        self.assertEqual(res.stored, ["nsccl.20261109.s.zip"])

    def test_complete_needs_end_of_day_and_elm(self):
        res = M.fetch_day(DAY, FakeHttp(self.routes(s=False)))
        self.assertFalse(M.complete(res))
        res = M.fetch_day(DAY, FakeHttp(self.routes()))
        self.assertTrue(M.complete(res))


class QueryTests(MarginFilesTest):
    def test_in_force_files_sorted_by_created(self):
        self.archived(DAY, "s", "202611091655")
        self.archived(DAY, "i1", "202611082026")
        self.archived(DAY, "i2", "202611091130")
        files = M.in_force_files(DAY)
        self.assertEqual([Path_name(p) for p, _ in files],
                         ["nsccl.20261109.i1.zip", "nsccl.20261109.i2.zip", "nsccl.20261109.s.zip"])
        self.assertEqual([c for _, c in files],
                         [timeutil.ist_ns(date(2026, 11, 8), "20:26"), timeutil.ist_ns(DAY, "11:30"),
                          timeutil.ist_ns(DAY, "16:55")])
        self.assertEqual(M.in_force_files(date(2026, 11, 10)), [])

    def test_previous_eod_skips_non_trading_days(self):
        self.archived(FRI, "s", "202611061655")
        self.archived(SAT, "s", "202611071655")           # not a trading day: must be ignored
        self.archived(DAY, "i1", "202611082026")          # not an end-of-day file
        path, created = M.previous_eod(DAY)
        self.assertEqual(Path_name(path), "nsccl.20261106.s.zip")
        self.assertEqual(created, timeutil.ist_ns(FRI, "16:55"))

    def test_previous_eod_none_when_nothing_archived(self):
        self.assertIsNone(M.previous_eod(DAY))


class BackfillTests(MarginFilesTest):
    def test_backfill_is_resumable_and_trading_days_only(self):
        routes = {f"nsccl.{d:%Y%m%d}.s.zip": (200, span_zip(d, "s", f"{d:%Y%m%d}1655")) for d in (FRI, DAY)}
        http = FakeHttp(routes)
        out = M.backfill(FRI, DAY, http)
        self.assertEqual([r.day for r in out], [FRI, DAY])                     # Sat and Sun skipped
        self.assertTrue(all(r.stored for r in out))
        n = len(http.urls)
        again = M.backfill(FRI, DAY, http)
        self.assertEqual(len(http.urls), n)                                    # nothing re-requested
        self.assertTrue(all(r.unchanged and not r.stored for r in again))


class ElmWindowTests(MarginFilesTest):
    def test_elm_window_stores_existing_days_only(self):
        http = FakeHttp({"date=09-Nov-2026": (200, elm_gz(DAY)), "date=06-Nov-2026": (200, elm_gz(FRI))})
        results = M.elm_window(http, today=DAY, days=7)
        stored = [r.day for r in results if r.stored]
        self.assertEqual(stored, [DAY, FRI])
        self.assertFalse(any("nsccl" in u for u in http.urls))                 # ELM only, no SPAN requests


class DaemonJobTests(MarginFilesTest):
    def setUp(self):
        super().setUp()
        from stockify.daemon import scheduler
        self.state = scheduler.State()

    def run_job(self, res: M.DayResult, now_ist: str):
        from unittest.mock import patch

        from stockify import daemon
        with patch.object(M, "fetch_day", return_value=res), \
                patch.object(timeutil, "now_ns", return_value=timeutil.ist_ns(DAY, now_ist)):
            daemon._run_job("margin_files", DAY, self.state)

    def test_complete_marks_done(self):
        self.run_job(M.DayResult(DAY, stored=[M.span_name(DAY, "s"), M.elm_name(DAY)]), "17:30")
        self.assertTrue(self.state.done("margin_files", DAY))

    def test_incomplete_retries_until_the_window_ends(self):
        partial = M.DayResult(DAY, stored=[M.span_name(DAY, "i1")], missing=[M.span_name(DAY, "s")])
        self.run_job(partial, "17:30")
        self.assertFalse(self.state.done("margin_files", DAY))
        self.run_job(partial, "22:30")
        self.assertTrue(self.state.done("margin_files", DAY))

    def test_missed_job_is_caught_up_on_a_later_day(self):
        from stockify.daemon import scheduler
        wake = timeutil.ist_ns(date(2026, 11, 10), "06:00")
        self.assertIn(("margin_files", DAY), scheduler.missed_jobs(wake, self.state))
        self.assertNotIn(("margin_files", date(2026, 11, 10)), scheduler.missed_jobs(wake, self.state))
        self.state.mark("margin_files", DAY)
        self.assertNotIn(("margin_files", DAY), scheduler.missed_jobs(wake, self.state))


def Path_name(p) -> str:  # noqa: N802
    return p.replace("\\", "/").rsplit("/", 1)[-1]


if __name__ == "__main__":
    unittest.main()
