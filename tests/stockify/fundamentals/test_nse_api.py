"""FS 1: the JSON client over nse_files.Http: cookie warm-up per page, raw bodies kept with their sha256, Blocked."""
from __future__ import annotations

import hashlib
import json
import unittest
from datetime import date
from pathlib import Path

import tests.support as support  # noqa: F401  — must precede project imports
from tests.stockify.data.test_nse_files import FakeSession

from stockify import paths, timeutil
from stockify.daemon.clock import ManualClock
from stockify.data import nse_files
from stockify.fundamentals import nse_api

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "fundamentals"
DAY = date(2026, 11, 17)
QUOTE = "/api/quote-equity"
PAGE = "https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE"


class ClientCase(unittest.TestCase):
    def setUp(self):
        support.fresh_cache_dir(self.id().rsplit(".", 1)[-1])
        self.clock = ManualClock(timeutil.ist_ns(DAY, "20:00"))

    def client(self, routes: dict) -> tuple[nse_api.Client, FakeSession]:
        session = FakeSession(routes, self.clock)
        return nse_api.Client(nse_files.Http(session=session, clock=self.clock)), session

    def raw_files(self, kind: str) -> list[str]:
        return sorted(p.name for p in paths.fundamentals_raw_dir(kind).iterdir())


class BlockedTests(ClientCase):
    def test_blocked_html_raises(self):
        """NSE's real anti-bot answer (403 with an Akamai HTML page), a 401, and a 200 whose body is HTML."""
        denied = (FIX / "quote-equity_RELIANCE_403.html").read_bytes()
        self.assertTrue(denied.startswith(b"<HTML>"))
        for answer in ((403, denied), (401, b'{"msg":"unauthorised"}'), (200, b"  \r\n<!DOCTYPE html><html></html>")):
            with self.subTest(status=answer[0]):
                client, _ = self.client({"quote-equity?symbol=RELIANCE": [answer]})
                with self.assertRaises(nse_api.Blocked) as cm:
                    client.json(QUOTE, {"symbol": "RELIANCE"}, warmup=PAGE)
                self.assertIsInstance(cm.exception, nse_files.NseError)
                self.assertIn(str(answer[0]), str(cm.exception))
                self.assertEqual(self.raw_files("quote-equity"), [])        # a refusal is never kept as data

    def test_other_failures_are_nse_errors_not_blocked(self):
        for answer in ((404, b"<html>not found</html>"), (200, b"{not json")):
            with self.subTest(status=answer[0]):
                client, _ = self.client({"quote-equity?symbol=RELIANCE": [answer]})
                with self.assertRaises(nse_files.NseError) as cm:
                    client.json(QUOTE, {"symbol": "RELIANCE"}, warmup=PAGE)
                self.assertNotIsInstance(cm.exception, nse_api.Blocked)


class RawTests(ClientCase):
    def test_raw_body_saved_with_sha(self):
        body = b'{"info":{"symbol":"RELIANCE"}}'
        answers = [(200, body), (200, body), (200, body + b" ")]
        client, session = self.client({"quote-equity?symbol=RELIANCE": answers})
        doc = client.json(QUOTE, {"symbol": "RELIANCE"}, warmup=PAGE, name="RELIANCE_20261117.json")
        self.assertEqual(doc, {"info": {"symbol": "RELIANCE"}})
        digest = hashlib.sha256(body).hexdigest()
        self.assertEqual(client.last_sha256, digest)
        folder = paths.fundamentals_raw_dir("quote-equity")
        self.assertEqual(folder, paths.nse_raw_dir() / "fundamentals" / "quote-equity")
        self.assertEqual((folder / "RELIANCE_20261117.json").read_bytes(), body)
        self.assertEqual((folder / "RELIANCE_20261117.json.sha256").read_text(encoding="utf-8"),
                         f"{digest}  RELIANCE_20261117.json\n")
        # The same bytes again: nothing new. Other bytes under the same name: kept beside it, the first untouched.
        client.json(QUOTE, {"symbol": "RELIANCE"}, warmup=PAGE, name="RELIANCE_20261117.json")
        client.json(QUOTE, {"symbol": "RELIANCE"}, warmup=PAGE, name="RELIANCE_20261117.json")
        self.assertEqual(self.raw_files("quote-equity"), ["RELIANCE_20261117.json", "RELIANCE_20261117.json.sha256",
                                                          "RELIANCE_20261117_r2.json",
                                                          "RELIANCE_20261117_r2.json.sha256"])
        self.assertEqual((folder / "RELIANCE_20261117.json").read_bytes(), body)
        self.assertEqual(client.last_sha256, hashlib.sha256(body + b" ").hexdigest())
        self.assertEqual(session.calls[-1][0], "https://www.nseindia.com/api/quote-equity?symbol=RELIANCE")

    def test_default_name_is_the_ist_day_and_the_params(self):
        client, _ = self.client({"quote-equity?symbol=M%26M": [(200, b"{}")]})
        client.json(QUOTE, {"symbol": "M&M"}, warmup=PAGE)
        self.assertEqual(self.raw_files("quote-equity"), ["20261117_M_M.json", "20261117_M_M.json.sha256"])

    def test_save_raw_refuses_unsafe_names(self):
        for kind, name in (("..", "a.json"), ("quote-equity", "../a.json"), ("quote-equity", "a\\b.json"),
                           ("quote-equity", "CON.json"), ("a/b", "x.json")):
            with self.subTest(kind=kind, name=name):
                with self.assertRaises(ValueError):
                    nse_api.save_raw(kind, name, b"{}")

    def test_static_file_get_is_kept_and_blocked_alike(self):
        csv = (FIX / "ind_nifty500list.csv").read_bytes()
        url = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
        client, _ = self.client({"ind_nifty500list.csv": [(200, csv), (403, b"<HTML>denied</HTML>")]})
        self.assertEqual(client.get(url, kind="nifty500", name="20261117.csv"), csv)
        self.assertEqual(client.last_sha256, hashlib.sha256(csv).hexdigest())
        self.assertEqual(self.raw_files("nifty500"), ["20261117.csv", "20261117.csv.sha256"])
        with self.assertRaises(nse_api.Blocked):
            client.get(url, kind="nifty500", name="20261117.csv")


class WarmupTests(ClientCase):
    def test_each_warmup_page_is_fetched_once(self):
        other = "https://www.nseindia.com/get-quotes/equity?symbol=INFY"
        client, session = self.client({"quote-equity?symbol=RELIANCE": [(200, b"{}")]})
        for warmup in (PAGE, PAGE, other):
            client.json(QUOTE, {"symbol": "RELIANCE"}, warmup=warmup)
        pages = [u for u, _ in session.calls if "get-quotes" in u]
        self.assertEqual(pages, [PAGE, other])
        # Every request, warm-ups included, keeps the Http's gap of at least min_interval_ms (1 s).
        gaps = [b - a for (_, a), (_, b) in zip(session.calls, session.calls[1:])]
        self.assertTrue(all(g >= 1_000_000_000 for g in gaps), gaps)


if __name__ == "__main__":
    unittest.main()
