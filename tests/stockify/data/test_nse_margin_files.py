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
    # mtime=0: no wall-clock stamp in the gzip header, so two calls give the same bytes (I-4: the old flake).
    return gzip.compress(f"OPTIDX,NIFTY,{day:%d-%b-%Y},24000,CE,0,2.0\n".upper().encode(), mtime=0)


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
            z.writestr(zipfile.ZipInfo("nsccl.20261109.i2.spn", date_time=(2026, 11, 9, 0, 0, 0)), "<spanFile/>")
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


def crc_bad_zip(day: date, suffix: str, created: str) -> bytes:
    """A SPAN zip whose header (first 64 KiB) is fine but whose member fails its CRC further on (M-1)."""
    body = span_zip_stored(day, suffix, created, pad=200_000)
    i = body.rindex(b"ZZZZ")                                  # a byte in the padding, far past the header
    return body[:i] + b"Y" + body[i + 1:]


def span_zip_stored(day: date, suffix: str, created: str, *, pad: int = 0, extra: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr(zipfile.ZipInfo(f"nsccl.{day:%Y%m%d}.{suffix}.spn", date_time=(2026, 11, 9, 0, 0, 0)),
                   f'<?xml version="1.0"?><spanFile><fileFormat>4.00</fileFormat><created>{created}</created>'
                   f"<pointInTime/><!--{'Z' * pad}--></spanFile>")
        if extra:
            z.writestr(zipfile.ZipInfo(extra, date_time=(2026, 11, 9, 0, 0, 0)), "x")
    return buf.getvalue()


class FullValidationTests(MarginFilesTest):
    """M-1: a file is checked whole before it is archived; a bad one is rejected and never kept."""

    def fetch_elm(self, body: bytes) -> M.DayResult:
        return M.fetch_day(DAY, FakeHttp({"api/reports": (200, body)}), span=False)

    def assert_elm_rejected(self, body: bytes) -> None:
        res = self.fetch_elm(body)
        self.assertEqual(res.rejected, [M.elm_name(DAY)])
        self.assertFalse((paths.nse_margin_dir("elm") / "2026" / M.elm_name(DAY)).exists())
        self.assertNotIn(M.elm_name(DAY), M.read_manifest(DAY))

    def test_a_gzip_magic_with_garbage_is_rejected(self):
        self.assert_elm_rejected(b"\x1f\x8b" + b"garbage")

    def test_a_truncated_elm_gzip_is_rejected(self):
        whole = gzip.compress(b"OPTIDX,NIFTY,23-NOV-2026,25000,CE,0,2\n" * 2000, mtime=0)
        self.assert_elm_rejected(whole[: len(whole) // 2])

    def test_a_non_utf8_elm_is_rejected(self):
        self.assert_elm_rejected(gzip.compress(b"OPTIDX,NIFTY,23-NOV-2026,25000,CE,0,2\n\xff\xfe,1,2\n", mtime=0))

    def test_an_elm_csv_of_the_wrong_shape_is_rejected(self):
        self.assert_elm_rejected(gzip.compress(b"<html>not a csv</html>\n", mtime=0))
        self.assert_elm_rejected(gzip.compress(b"OPTIDX,NIFTY,23-NOV-2026,25000,CE,0,2\nOPTIDX,NIFTY\n", mtime=0))
        self.assert_elm_rejected(gzip.compress(b"", mtime=0))

    def test_an_elm_csv_with_a_header_row_is_kept(self):
        body = gzip.compress(b"INSTRUMENT,SYMBOL,EXPIRY,STRIKE,TYPE,X,ELM\nOPTIDX,NIFTY,23-NOV-2026,25000,CE,0,2\n",
                             mtime=0)
        self.assertEqual(self.fetch_elm(body).stored, [M.elm_name(DAY)])

    def test_a_span_zip_with_a_bad_crc_after_the_header_is_rejected(self):
        body = crc_bad_zip(DAY, "i2", "202611091130")
        self.assertEqual(M.span_created(body[:]), "202611091130")       # the header alone looks fine
        res = M.fetch_day(DAY, FakeHttp({"nsccl.20261109.i2.zip": (200, body)}), elm=False, suffixes=("i2",))
        self.assertEqual(res.rejected, ["nsccl.20261109.i2.zip"])
        self.assertFalse((paths.nse_margin_dir("span") / "2026" / "nsccl.20261109.i2.zip").exists())

    def test_a_span_zip_needs_exactly_one_spn_member(self):
        two = io.BytesIO()
        with zipfile.ZipFile(two, "w") as z:
            for n in ("a.spn", "b.spn"):
                z.writestr(zipfile.ZipInfo(n, date_time=(2026, 11, 9, 0, 0, 0)),
                           "<spanFile><created>202611091130</created></spanFile>")
        res = M.fetch_day(DAY, FakeHttp({"nsccl.20261109.i2.zip": (200, two.getvalue())}), elm=False,
                          suffixes=("i2",))
        self.assertEqual(res.rejected, ["nsccl.20261109.i2.zip"])

    def test_a_whole_good_span_zip_is_kept(self):
        body = span_zip_stored(DAY, "i2", "202611091130", pad=200_000)
        res = M.fetch_day(DAY, FakeHttp({"nsccl.20261109.i2.zip": (200, body)}), elm=False, suffixes=("i2",))
        self.assertEqual(res.stored, ["nsccl.20261109.i2.zip"])


class BodyCapTests(MarginFilesTest):
    """M-4: a body over the cap is an error, never archived."""

    def test_an_oversized_body_is_an_error_and_not_archived(self):
        from stockify.daemon.clock import ManualClock
        from stockify.data import nse_files
        from tests.stockify.data.test_nse_files import FakeSession
        body = span_zip(DAY, "s", "202611091655")
        clock = ManualClock(timeutil.ist_ns(DAY, "17:30"))
        session = FakeSession({"nsccl.20261109.s.zip": [(200, body)]}, clock)
        http = nse_files.Http(session=session, clock=clock, max_bytes=len(body) - 1)
        res = M.fetch_day(DAY, http, elm=False, suffixes=("s",))
        self.assertEqual(res.errors, ["nsccl.20261109.s.zip"])
        self.assertFalse((paths.nse_margin_dir("span") / "2026" / "nsccl.20261109.s.zip").exists())
        http = nse_files.Http(session=session, clock=clock, max_bytes=len(body))
        self.assertEqual(M.fetch_day(DAY, http, elm=False, suffixes=("s",)).stored, ["nsccl.20261109.s.zip"])


class CreatedPlausibilityTests(MarginFilesTest):
    """L-4: a file's created time must lie in [D-2 12:00, D+1 06:00] IST for its business day D."""

    def fetch(self, suffix: str, created: str) -> M.DayResult:
        name = f"nsccl.20261109.{suffix}.zip"
        return M.fetch_day(DAY, FakeHttp({name: (200, span_zip(DAY, suffix, created))}), elm=False,
                           suffixes=(suffix,))

    def test_the_window_edges_are_accepted(self):
        self.assertEqual(self.fetch("i1", "202611071200").stored, ["nsccl.20261109.i1.zip"])
        self.assertEqual(self.fetch("s", "202611100600").stored, ["nsccl.20261109.s.zip"])

    def test_a_created_time_outside_the_window_is_rejected(self):
        for suffix, created in (("i1", "202611071159"), ("i3", "202611100601"), ("i2", "202501010000")):
            with self.subTest(created=created):
                res = self.fetch(suffix, created)
                self.assertEqual(res.rejected, [f"nsccl.20261109.{suffix}.zip"])
                self.assertFalse((paths.nse_margin_dir("span") / "2026" / f"nsccl.20261109.{suffix}.zip").exists())

    def test_an_archived_file_with_an_implausible_created_time_is_not_used(self):
        self.archived(DAY, "i2", "202611091130")
        manifest = M.read_manifest(DAY)
        manifest["nsccl.20261109.i2.zip"]["created"] = "202611011130"      # backdated: would apply too early
        M._write_manifest(DAY, manifest)
        skipped: list = []
        self.assertEqual(M.in_force_files(DAY, skipped), [])
        self.assertEqual([Path_name(p) for p, _ in skipped], ["nsccl.20261109.i2.zip"])
        self.assertIn("not plausible", skipped[0][1])


class OpeningFileTests(MarginFilesTest):
    """I-2: the bundle's opening `.s` steps back over days with no archived file at all (NSE published nothing:
    a holiday the calendar does not know before 2024), at most 5 of them; a day with any file stops the walk."""

    def test_a_holiday_gap_uses_the_end_of_day_file_before_it(self):
        self.archived(date(2023, 1, 25), "s", "202301251700")              # 26 Jan 2023: Republic Day, no files
        files = M.usable_files(date(2023, 1, 27))
        self.assertEqual([Path_name(p) for p, _ in files], ["nsccl.20230125.s.zip"])

    def test_a_previous_day_with_only_intraday_files_gives_nothing(self):
        self.archived(date(2023, 1, 25), "s", "202301251700")
        self.archived(date(2023, 1, 26), "i1", "202301252000")              # a file, but no .s: never older ones
        self.assertEqual(M.usable_files(date(2023, 1, 27)), [])

    def test_more_than_five_empty_days_give_nothing(self):
        # 2023-03-01 (Wed) .s; 2..9 Mar has 6 weekdays; the run day is 10 Mar.
        self.archived(date(2023, 3, 1), "s", "202303011700")
        self.assertEqual(M.usable_files(date(2023, 3, 10)), [])
        self.assertEqual(M.opening_eod(date(2023, 3, 10)), None)
        self.archived(date(2023, 3, 2), "s", "202303021700")                # now 5 empty weekdays: 3, 6, 7, 8, 9
        self.assertEqual(Path_name(M.opening_eod(date(2023, 3, 10))[0]), "nsccl.20230302.s.zip")

    def test_reading_the_archive_creates_no_year_folders(self):
        M.usable_files(date(2015, 6, 10))
        self.assertFalse((paths.nse_margin_dir("span") / "2015").exists())


def Path_name(p) -> str:  # noqa: N802
    return p.replace("\\", "/").rsplit("/", 1)[-1]


if __name__ == "__main__":
    unittest.main()
