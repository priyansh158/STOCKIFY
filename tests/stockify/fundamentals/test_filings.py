"""FS 2: NSE's results filings per company (both listings) and the resumable XBRL download."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.data.test_nse_files import FakeSession
from tests.support import SandboxedDBTest

from stockify import paths, timeutil
from stockify.daemon.clock import ManualClock
from stockify.data import nse_files
from stockify.fundamentals import filings, nse_api, store

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "fundamentals"
DAY = date(2026, 11, 17)
SINCE = date(2021, 4, 1)

Q3_CONS = "https://nsearchives.nseindia.in/corporate/xbrl/INDAS_117297_1348248_16012025081520.xml"
Q3_STAND = "https://nsearchives.nseindia.in/corporate/xbrl/INDAS_117298_1348254_16012025082021.xml"
IF_CONS = "https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_INDAS_1750537_30102026093254_WEB.xml"


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def listing_routes(quarterly: bytes | None = None, integrated: bytes | None = None) -> dict:
    return {
        "period=Quarterly": [(200, quarterly if quarterly is not None else fixture("filings_RELIANCE_quarterly.json"))],
        "period=Half-Yearly": [(200, fixture("filings_RELIANCE_half-yearly.json"))],
        "period=Annual": [(200, fixture("filings_RELIANCE_annual.json"))],
        "type=Integrated+Filing-+Financials": [(200, integrated if integrated is not None
                                                 else fixture("filings_integrated_RELIANCE.json"))],
    }


class FilingsCase(SandboxedDBTest):
    db_modules = (store,)

    def setUp(self):
        super().setUp()
        self.clock = ManualClock(timeutil.ist_ns(DAY, "20:45"))

    def client(self, routes: dict) -> tuple[nse_api.Client, FakeSession]:
        session = FakeSession(routes, self.clock)
        return nse_api.Client(nse_files.Http(session=session, clock=self.clock)), session

    def rows(self, **where) -> list[dict]:
        return [r for r in filings.for_symbol("RELIANCE") if all(r[k] == v for k, v in where.items())]

    def xbrl_requests(self, session: FakeSession) -> list[str]:
        return [u for u, _ in session.calls if u.endswith(".xml")]


class ListTests(FilingsCase):
    def test_list_parses_periods_basis_and_broadcast_ist(self):
        client, session = self.client(listing_routes())
        got = filings.list_for("RELIANCE", client=client, since=SINCE)
        # Legacy listing: 29 quarterly + 5 annual from 2021-04-01 (no half-years); integrated listing: 14 quarters,
        # plus an annual row for each of its 4 March filings.
        self.assertEqual(len(got), 29 + 5 + 14 + 4)
        self.assertEqual(len(filings.for_symbol("RELIANCE")), len(got))
        self.assertTrue(all(r["period_end"] >= "2021-04-01" for r in got))
        self.assertEqual({r["period_kind"] for r in got if r["source"] == "results"}, {"Q", "A"})

        q3 = filings.get(filings.filing_id("RELIANCE", "2024-12-31", "Q", "standalone",
                                           timeutil.ist_ns(date(2025, 1, 16), "20:20:21"), Q3_STAND))
        self.assertIsNotNone(q3)
        self.assertEqual((q3["period_start"], q3["period_end"], q3["period_kind"], q3["basis"], q3["source"]),
                         ("2024-10-01", "2024-12-31", "Q", "standalone", "results"))
        # "16-Jan-2025 20:20:21" IST is 14:50:21 UTC.
        self.assertEqual(q3["broadcast_ns"], 1737039021 * 1_000_000_000)
        self.assertEqual(q3["broadcast_source"], "broadCastDate")
        self.assertEqual((q3["xbrl_url"], q3["status"], q3["seq_number"]), (Q3_STAND, "listed", "1189823"))

        cons = self.rows(period_end="2024-12-31", basis="consolidated")
        self.assertEqual([r["xbrl_url"] for r in cons], [Q3_CONS])
        annual = self.rows(period_end="2024-03-31", period_kind="A")
        self.assertEqual(sorted(r["basis"] for r in annual), ["consolidated", "standalone"])

        integ = self.rows(period_end="2026-09-30")
        self.assertEqual(sorted((r["basis"], r["period_kind"], r["source"]) for r in integ),
                         [("consolidated", "Q", "integrated"), ("standalone", "Q", "integrated")])
        ic = [r for r in integ if r["basis"] == "consolidated"][0]
        self.assertEqual((ic["xbrl_url"], ic["broadcast_source"]), (IF_CONS, "broadcast_Date"))
        self.assertEqual(ic["broadcast_ns"], timeutil.ist_ns(date(2026, 10, 30), "21:32:55"))

        # Both listings and every period asked for, each after its page's warm-up; answers kept raw.
        urls = [u for u, _ in session.calls]
        self.assertIn(filings.LEGACY_WARMUP, urls)
        self.assertIn(filings.INTEGRATED_WARMUP, urls)
        kept = sorted(p.name for p in paths.fundamentals_raw_dir("corporates-financial-results").iterdir())
        self.assertEqual(len(kept), 6)                           # three answers, each with its .sha256
        gaps = [b - a for (_, a), (_, b) in zip(session.calls, session.calls[1:])]
        self.assertTrue(all(g >= 1_000_000_000 for g in gaps), gaps)

    def test_integrated_march_filing_is_also_the_year(self):
        client, session = self.client(listing_routes())
        filings.list_for("RELIANCE", client=client, since=SINCE)
        integ = [r for r in filings.for_symbol("RELIANCE") if r["source"] == "integrated"]
        self.assertEqual(sorted({r["period_end"] for r in integ if r["period_kind"] == "A"}),
                         ["2025-03-31", "2026-03-31"])
        self.assertEqual({r["period_kind"] for r in integ if not r["period_end"].endswith("-03-31")}, {"Q"})
        mar = self.rows(period_end="2026-03-31", basis="consolidated")
        self.assertEqual(sorted(r["period_kind"] for r in mar), ["A", "Q"])
        q, a = sorted(mar, key=lambda r: r["period_kind"], reverse=True)
        # Same file, time and sequence number as the quarter; its own id (the period kind is part of it); the year
        # runs from 1 April.
        for k in ("xbrl_url", "broadcast_ns", "broadcast_source", "seq_number", "source", "status"):
            self.assertEqual(a[k], q[k], k)
        self.assertEqual(a["broadcast_ns"], timeutil.ist_ns(date(2026, 4, 24), "22:57:12"))
        self.assertEqual(a["filing_id"], filings.filing_id("RELIANCE", "2026-03-31", "A", "consolidated",
                                                           a["broadcast_ns"], a["xbrl_url"]))
        self.assertNotEqual(a["filing_id"], q["filing_id"])
        self.assertEqual((a["period_start"], q["period_start"]), ("2025-04-01", None))

    def test_listing_twice_adds_nothing_and_keeps_status(self):
        client, _ = self.client(listing_routes())
        first = filings.list_for("RELIANCE", client=client, since=SINCE)
        fid = first[0]["filing_id"]
        filings.mark(fid, "held", "test")
        filings.list_for("RELIANCE", client=client, since=SINCE)
        self.assertEqual(len(filings.for_symbol("RELIANCE")), len(first))
        self.assertEqual((filings.get(fid)["status"], filings.get(fid)["reason"]), ("held", "test"))

    def test_half_years_fallback_times_and_missing_xbrl_from_the_real_old_rows(self):
        client, _ = self.client(listing_routes())
        filings.list_for("RELIANCE", client=client, since=date(2005, 1, 1))
        halves = self.rows(period_kind="H")
        self.assertEqual(len(halves), 6)
        # Before XBRL, NSE links "…/xbrl/-": listed as held with a plain reason, never downloaded.
        self.assertTrue(all(r["status"] == "held" and r["xbrl_url"] is None for r in halves))
        self.assertTrue(all("no XBRL" in r["reason"] for r in halves))
        # 31-Mar-2007 has no broadCastDate or exchdisstime: the filingDate "26-Apr-2007 18:00" is used.
        q = self.rows(period_end="2007-03-31", period_kind="Q")[0]
        self.assertEqual((q["broadcast_source"], q["broadcast_ns"]),
                         ("filingDate", timeutil.ist_ns(date(2007, 4, 26), "18:00")))
        # 31-Dec-2006 has no time at all ("-").
        q = self.rows(period_end="2006-12-31", period_kind="Q")[0]
        self.assertEqual((q["broadcast_source"], q["broadcast_ns"], q["status"]), (None, None, "held"))

    def test_broadcast_fallback_order(self):
        base = {"broadCastDate": None, "exchdisstime": "16-Jan-2025 20:20:54", "filingDate": "16-Jan-2025 20:20"}
        self.assertEqual(filings.broadcast_of(base),
                         (timeutil.ist_ns(date(2025, 1, 16), "20:20:54"), "exchdisstime"))
        self.assertEqual(filings.broadcast_of({**base, "exchdisstime": "-"}),
                         (timeutil.ist_ns(date(2025, 1, 16), "20:20"), "filingDate"))
        self.assertEqual(filings.broadcast_of({**base, "broadCastDate": "16-Jan-2025 20:20:21"})[1], "broadCastDate")
        with self.assertRaises(ValueError):
            filings.parse_ist("31-Foo-2025 20:20:21")

    def test_same_period_new_broadcast_is_a_new_filing(self):
        doc = json.loads(fixture("filings_RELIANCE_quarterly.json"))
        orig = next(r for r in doc if r["xbrl"] == Q3_CONS)
        revised = {**orig, "broadCastDate": "20-Jan-2025 18:00:00", "seqNumber": "1190000",
                   "xbrl": Q3_CONS.replace("117297", "117999")}
        quarterly = json.dumps(doc + [revised]).encode()
        client, _ = self.client(listing_routes(quarterly=quarterly))
        filings.list_for("RELIANCE", client=client, since=SINCE)
        cons = self.rows(period_end="2024-12-31", period_kind="Q", basis="consolidated")
        self.assertEqual(len(cons), 2)
        self.assertEqual(len({r["filing_id"] for r in cons}), 2)
        self.assertEqual(sorted(r["broadcast_ns"] for r in cons),
                         [timeutil.ist_ns(date(2025, 1, 16), "20:15:20"), timeutil.ist_ns(date(2025, 1, 20), "18:00")])
        # The same filing listed in both the quarterly and the annual answer stays two rows (Q and A).
        q4 = self.rows(period_end="2024-03-31", basis="consolidated")
        self.assertEqual(sorted(r["period_kind"] for r in q4), ["A", "Q"])
        # ... even if NSE gave both the same time and link.
        self.assertNotEqual(filings.filing_id("X", "2024-03-31", "Q", "consolidated", 1, Q3_CONS),
                            filings.filing_id("X", "2024-03-31", "A", "consolidated", 1, Q3_CONS))

    def test_blocked_listing_records_nothing(self):
        routes = listing_routes()
        routes["period=Annual"] = [(403, fixture("quote-equity_RELIANCE_403.html"))]
        client, _ = self.client(routes)
        with self.assertRaises(nse_api.Blocked):
            filings.list_for("RELIANCE", client=client, since=SINCE)
        self.assertEqual(filings.for_symbol("RELIANCE"), [])

    def test_listing_of_another_symbol_is_refused(self):
        client, _ = self.client(listing_routes())
        with self.assertRaises(ValueError):
            filings.list_for("INFY", client=client, since=SINCE)   # every row answers RELIANCE


class DownloadTests(FilingsCase):
    def list_two(self) -> None:
        """Only the two Q3 FY25 filings (real listing rows) are listed."""
        doc = [r for r in json.loads(fixture("filings_RELIANCE_quarterly.json")) if r["xbrl"] in (Q3_CONS, Q3_STAND)]
        routes = {k: [(200, b"[]")] for k in listing_routes()}
        routes["period=Quarterly"] = [(200, json.dumps(doc).encode())]
        routes["type=Integrated+Filing-+Financials"] = [(200, b'{"data":[],"totalCount":0}')]
        client, _ = self.client(routes)
        self.assertEqual(len(filings.list_for("RELIANCE", client=client, since=SINCE)), 2)

    def xbrl_routes(self, cons=None, stand=None) -> dict:
        return {Q3_CONS.rsplit("/", 1)[1]: cons or [(200, fixture("xbrl_RELIANCE_20241231_consolidated.xml"))],
                Q3_STAND.rsplit("/", 1)[1]: stand or [(200, fixture("xbrl_RELIANCE_20241231_standalone.xml"))]}

    def test_download_is_resumable(self):
        self.list_two()
        client, session = self.client(self.xbrl_routes())
        got = filings.download_pending(client=client)
        self.assertEqual((got["downloaded"], got["skipped"], got["failed"], got["stopped"]), (2, 0, 0, None))
        for basis, name in (("consolidated", "xbrl_RELIANCE_20241231_consolidated.xml"),
                            ("standalone", "xbrl_RELIANCE_20241231_standalone.xml")):
            row = self.rows(basis=basis)[0]
            body = fixture(name)
            self.assertEqual((row["status"], row["xbrl_sha256"]), ("downloaded", hashlib.sha256(body).hexdigest()))
            self.assertRegex(row["xbrl_file"], rf"^2024/RELIANCE_2024-12-31_{basis}_[0-9a-f]{{12}}\.xml$")
            file = filings.xbrl_path(row)
            self.assertEqual(file.parent.parent, paths.fundamentals_raw_dir("xbrl"))
            self.assertEqual(file.read_bytes(), body)
            self.assertEqual(Path(str(file) + ".sha256").read_text(encoding="utf-8"),
                             f"{row['xbrl_sha256']}  {file.name}\n")
        self.assertEqual(len(self.xbrl_requests(session)), 2)

        # A second run downloads nothing and makes no request.
        client2, session2 = self.client(self.xbrl_routes())
        got = filings.download_pending(client=client2)
        self.assertEqual((got["downloaded"], got["skipped"], got["failed"]), (0, 0, 0))
        self.assertEqual(session2.calls, [])

    def test_file_already_on_disk_is_not_fetched_again(self):
        """A run that kept the file but died before the store said so: the next run takes the file as it is."""
        self.list_two()
        client, _ = self.client(self.xbrl_routes())
        filings.download_pending(client=client)
        for r in filings.for_symbol("RELIANCE"):
            filings.mark(r["filing_id"], "listed")
        client2, session2 = self.client(self.xbrl_routes())
        got = filings.download_pending(client=client2)
        self.assertEqual((got["downloaded"], got["skipped"]), (0, 2))
        self.assertEqual(self.xbrl_requests(session2), [])
        self.assertEqual({r["status"] for r in filings.for_symbol("RELIANCE")}, {"downloaded"})

    def test_blocked_stops_cleanly_and_keeps_progress(self):
        self.list_two()
        denied = fixture("quote-equity_RELIANCE_403.html")
        # Newest first: the standalone (20:20:21) comes before the consolidated (20:15:20). The second is refused.
        client, session = self.client(self.xbrl_routes(cons=[(403, denied)]))
        got = filings.download_pending(client=client)
        self.assertEqual((got["downloaded"], got["failed"], got["stopped"]), (1, 0, "blocked"))
        self.assertEqual(self.rows(basis="standalone")[0]["status"], "downloaded")
        cons = self.rows(basis="consolidated")[0]
        self.assertEqual((cons["status"], cons["xbrl_file"]), ("listed", None))          # unmarked, retried later
        self.assertFalse(any(p.suffix == ".part" for p in paths.fundamentals_raw_dir("xbrl").rglob("*")))
        # An HTML page with status 200 (the anti-bot page) is Blocked as well, and never kept.
        client, _ = self.client(self.xbrl_routes(cons=[(200, b"\r\n<!DOCTYPE html><html>denied</html>")]))
        self.assertEqual(filings.download_pending(client=client)["stopped"], "blocked")
        self.assertEqual(len(list(paths.fundamentals_raw_dir("xbrl").rglob("*.xml"))), 1)
        # Resumed later: only the missing one is fetched.
        client, session = self.client(self.xbrl_routes())
        got = filings.download_pending(client=client)
        self.assertEqual((got["downloaded"], got["stopped"]), (1, None))
        self.assertEqual(self.xbrl_requests(session), [Q3_CONS])

    def test_deadline_and_limit_stop_before_a_request(self):
        self.list_two()
        client, session = self.client(self.xbrl_routes())
        got = filings.download_pending(client=client, deadline_ns=self.clock.now_ns())
        self.assertEqual((got["downloaded"], got["stopped"]), (0, "deadline"))
        self.assertEqual(session.calls, [])
        got = filings.download_pending(client=client, limit=1)
        self.assertEqual((got["downloaded"], got["stopped"]), (1, "limit"))
        self.assertEqual(filings.download_pending(client=client)["downloaded"], 1)

    def test_can_fetch_is_asked_before_each_request(self):
        """FS 8.3: the job's budget counts requests, retries included; the download asks before each fetch."""
        self.list_two()
        client, session = self.client(self.xbrl_routes())
        asked = []

        def can_fetch():  # noqa: ANN202
            asked.append(len(session.calls))
            return len(asked) == 1
        got = filings.download_pending(client=client, can_fetch=can_fetch)
        self.assertEqual((got["downloaded"], got["stopped"]), (1, "limit"))
        self.assertEqual(len(asked), 2)
        self.assertEqual(asked[0], 0)

    def test_failures_are_counted_and_the_run_goes_on(self):
        self.list_two()
        client, _ = self.client(self.xbrl_routes(stand=[(404, b"<html>not found</html>")],
                                                 cons=[(200, b"<?xml version='1.0'?><xbrli:xbrl")]))
        got = filings.download_pending(client=client)
        self.assertEqual((got["downloaded"], got["failed"], got["stopped"]), (0, 2, None))
        stand, cons = self.rows(basis="standalone")[0], self.rows(basis="consolidated")[0]
        self.assertEqual(stand["status"], "held")                       # NSE has no file at the link
        self.assertIn("404", stand["reason"])
        self.assertEqual(cons["status"], "listed")                      # a broken body is tried again next time
        self.assertIn("not a well-formed XBRL", cons["reason"])
        self.assertEqual(list(paths.fundamentals_raw_dir("xbrl").rglob("*.xml")), [])
        # Broken a second time: held with the reason, and never fetched again.
        client, _ = self.client(self.xbrl_routes(cons=[(200, b"<?xml version='1.0'?><xbrli:xbrl")]))
        self.assertEqual(filings.download_pending(client=client)["failed"], 1)
        cons = self.rows(basis="consolidated")[0]
        self.assertEqual(cons["status"], "held")
        self.assertIn("not a well-formed XBRL", cons["reason"])
        self.assertIn("twice", cons["reason"])
        client, session = self.client(self.xbrl_routes())
        self.assertEqual(filings.download_pending(client=client)["failed"], 0)
        self.assertEqual(self.xbrl_requests(session), [])

    def test_403_warms_up_again_and_retries_once(self):
        """Cookies expire during a long backfill: one refusal is followed by a fresh warm-up and one retry."""
        self.list_two()
        denied = fixture("quote-equity_RELIANCE_403.html")
        client, session = self.client(self.xbrl_routes(
            stand=[(403, denied), (200, fixture("xbrl_RELIANCE_20241231_standalone.xml"))]))
        got = filings.download_pending(client=client)
        self.assertEqual((got["downloaded"], got["failed"], got["stopped"]), (2, 0, None))
        urls = [u for u, _ in session.calls]
        self.assertEqual(urls[:4], ["https://www.nseindia.com/", Q3_STAND, "https://www.nseindia.com/", Q3_STAND])
        self.assertEqual(self.xbrl_requests(session), [Q3_STAND, Q3_STAND, Q3_CONS])

    def test_429_and_a_bom_html_page_stop_the_run_like_a_refusal(self):
        self.list_two()
        for answer in ((429, b"Too Many Requests"), (200, b"\xef\xbb\xbf\r\n<HTML><HEAD><TITLE>Access Denied")):
            with self.subTest(status=answer[0]):
                client, session = self.client(self.xbrl_routes(stand=[answer]))
                got = filings.download_pending(client=client)
                self.assertEqual((got["downloaded"], got["failed"], got["stopped"]), (0, 0, "blocked"))
                self.assertEqual(self.xbrl_requests(session), [Q3_STAND])          # nothing after the refusal
                row = self.rows(basis="standalone")[0]
                self.assertEqual((row["status"], row["reason"]), ("listed", None))
        self.assertEqual(list(paths.fundamentals_raw_dir("xbrl").rglob("*.xml")), [])

    def test_same_file_listed_twice_is_fetched_once(self):
        """NSE lists the year's last quarter and the year under one XBRL link."""
        doc = json.loads(fixture("filings_RELIANCE_annual.json"))
        url = "https://nsearchives.nseindia.in/corporate/xbrl/INDAS_104634_1106038_22042024074704.xml"
        routes = {k: [(200, b"[]")] for k in listing_routes()}
        routes["period=Quarterly"] = [(200, fixture("filings_RELIANCE_quarterly.json"))]
        routes["period=Annual"] = [(200, json.dumps(doc).encode())]
        routes["type=Integrated+Filing-+Financials"] = [(200, b'{"data":[],"totalCount":0}')]
        client, _ = self.client(routes)
        filings.list_for("RELIANCE", client=client, since=date(2024, 3, 31))
        both = [r for r in filings.for_symbol("RELIANCE") if r["xbrl_url"] == url]
        self.assertEqual(sorted(r["period_kind"] for r in both), ["A", "Q"])
        body = fixture("xbrl_RELIANCE_20241231_consolidated.xml")               # any real XBRL body will do
        client, session = self.client({u.rsplit("/", 1)[1]: [(200, body)]
                                       for u in {r["xbrl_url"] for r in filings.for_symbol("RELIANCE")}})
        filings.download_pending(client=client)
        self.assertEqual(sum(1 for u in self.xbrl_requests(session) if u == url), 1)
        both = [filings.get(r["filing_id"]) for r in both]
        self.assertEqual({(r["status"], r["xbrl_file"]) for r in both}, {("downloaded", both[0]["xbrl_file"])})


if __name__ == "__main__":
    import unittest
    unittest.main()
