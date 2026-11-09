"""Paper only (LCB 5.1): no code path reaches a Kite order route.

`kite_client.get_client()` strips the order routes for every caller, and `kite_readonly.ReadOnlyKite` lets only the
read calls through, with `consider_positions` forced False. The fake client mimics kiteconnect's route table and
method names (tests/stockify/kite_fakes.py); when the real SDK is importable it is checked too, without any request.
"""
from __future__ import annotations

import re
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports

import kite_client
from stockify import kite_readonly
from tests.stockify.kite_fakes import (EXPECTED_DENY_5_2_2, ORDER_METHODS_5_2_2, ROUTES_5_2_2, SDK_VERSION,
                                       FakeKite, FakeKiteConnect)

REPO = Path(kite_client.__file__).resolve().parent

METHODS = re.compile(r"\b(place_order|modify_order|cancel_order|exit_order|place_autoslice_order|place_gtt|"
                     r"modify_gtt|delete_gtt|place_mf_order|cancel_mf_order|place_mf_sip|modify_mf_sip|cancel_mf_sip|"
                     r"convert_position)\b")
ROUTES = re.compile(r"['\"](order\.(place|modify|cancel)|gtt\.(place|modify|delete)|mf\.(order|sip)\.[a-z]+|"
                    r"portfolio\.positions\.convert)['\"]")
URLS = re.compile(r"['\"]/(orders|gtt|mf/orders|mf/sips)\b")
DENY_BEGIN, DENY_END = "# ORDER-DENY-LIST BEGIN", "# ORDER-DENY-LIST END"


def tracked_sources() -> list[Path]:
    cmd = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py", "*.ps1"]
    out = subprocess.run(cmd, cwd=REPO, capture_output=True, check=True).stdout.decode("utf-8")
    return [REPO / p for p in out.split("\0") if p and not p.startswith(("tests/", ".venv/"))]


class StrippedClientTest(unittest.TestCase):
    """get_client() with the fake SDK class patched in and a fresh sandboxed token."""

    def setUp(self):
        support.fresh_cache_dir(type(self).__name__)
        env = support.isolated_env({"KITE_API_KEY": "test-key", "KITE_API_SECRET": "test-secret"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for target, value in (("KiteConnect", FakeKiteConnect), ("KITECONNECT_AVAILABLE", True),
                              ("_client", None), ("_client_token_fp", "")):
            p = mock.patch.object(kite_client, target, value)
            p.start()
            self.addCleanup(p.stop)
        kite_client.save_token({"access_token": "test-access", "obtained_at": int(time.time())})

    def test_order_methods_unreachable_on_get_client(self):
        k = kite_client.get_client()
        for name in ORDER_METHODS_5_2_2:
            with self.subTest(name=name), self.assertRaises(kite_client.OrderRouteForbidden):
                getattr(k, name)(variety="regular")
        self.assertEqual(k.sent, [])
        self.assertEqual(k.reqsession.sent, [])

    def test_class_methods_on_the_stripped_instance_are_refused_too(self):
        k = kite_client.get_client()
        for name in ORDER_METHODS_5_2_2:
            with self.subTest(name=name), self.assertRaises(kite_client.OrderRouteForbidden):
                getattr(FakeKiteConnect, name)(k, variety="regular")
        self.assertEqual(k.reqsession.sent, [])

    def test_private_request_to_order_route_refused(self):
        k = kite_client.get_client()
        calls = [lambda: k._post("order.place", params={}), lambda: k._request("gtt.place", "POST"),
                 lambda: k._request("/orders/regular", "POST"), lambda: k._put("order.modify"),
                 lambda: k._delete("order.cancel"), lambda: k._get("orders"),
                 lambda: k._put("portfolio.positions.convert"), lambda: k._request("portfolio.positions", "PUT"),
                 lambda: k._request("https://api.kite.trade/mf/sips", "POST"),
                 lambda: k._post("/GTT/triggers"),
                 lambda: FakeKiteConnect._request(k, "order.place", "POST"),
                 lambda: k.reqsession.post("https://api.kite.trade/orders/regular"),
                 lambda: k.reqsession.request("DELETE", "https://api.kite.trade/gtt/triggers/1")]
        for i, call in enumerate(calls):
            with self.subTest(i=i), self.assertRaises((kite_client.OrderRouteForbidden, KeyError)):
                call()
        for key in EXPECTED_DENY_5_2_2:
            self.assertNotIn(key, k._routes)
        self.assertEqual(k.reqsession.sent, [])

    def test_getattr_and_routes_cannot_bring_an_order_route_back(self):
        k = kite_client.get_client()
        k._routes["order.place"] = "/orders/{variety}"           # even a caller re-adding the key is refused
        with self.assertRaises(kite_client.OrderRouteForbidden):
            k._post("order.place", url_args={"variety": "regular"})
        self.assertEqual(k.reqsession.sent, [])

    def test_reads_still_work(self):
        k = kite_client.get_client()
        k.quote("NSE:RELIANCE")
        k.ltp("NSE:RELIANCE")
        k.historical_data(256265, None, None, "day")
        k.instruments()
        k.profile()
        k.margins()
        k.positions()                                              # GET /portfolio/positions is a read
        k.basket_order_margins([], consider_positions=False)
        k.order_margins([])
        k.get_virtual_contract_note([])
        self.assertEqual([s[0] for s in k.sent], [
            "market.quote", "market.quote.ltp", "market.historical", "market.instruments.all", "user.profile",
            "user.margins", "portfolio.positions", "order.margins.basket", "order.margins", "order.contract_note"])
        self.assertEqual(len(k.reqsession.sent), 10)

    def test_every_caller_shares_the_one_stripped_client(self):
        k = kite_client.get_client()
        self.assertIs(kite_client.get_client(), k)
        self.assertEqual(k.access_token, "test-access")

    def test_login_exchange_uses_a_stripped_client(self):
        FakeKiteConnect.instances.clear()
        kite_client.exchange_request_token("req-123")
        kc = FakeKiteConnect.instances[-1]
        with self.assertRaises(kite_client.OrderRouteForbidden):
            kc.place_order(variety="regular")
        self.assertEqual([s[0] for s in kc.sent], ["api.token"])

    def test_deny_list_from_the_fake_table(self):
        self.assertEqual(kite_client.order_route_keys(ROUTES_5_2_2), EXPECTED_DENY_5_2_2)

    def test_a_table_missing_a_required_key_fails_loudly(self):
        table = {k: v for k, v in ROUTES_5_2_2.items() if k != "gtt.delete"}
        with self.assertRaises(RuntimeError):
            kite_client.order_route_keys(table)

    def test_an_unknown_order_like_key_is_denied(self):
        table = dict(ROUTES_5_2_2, **{"order.place.iceberg": "/iceberg", "gtt.bulk": "/v2/triggers"})
        keys = kite_client.order_route_keys(table)
        self.assertIn("order.place.iceberg", keys)
        self.assertIn("gtt.bulk", keys)

    def test_margin_and_charges_calculators_are_kept(self):
        for key in ("order.margins", "order.margins.basket", "order.contract_note", "portfolio.positions",
                    "market.quote", "market.historical", "user.profile"):
            self.assertNotIn(key, kite_client.ORDER_ROUTE_KEYS | kite_client.order_route_keys(ROUTES_5_2_2))


@unittest.skipUnless(support.HAVE_KITECONNECT, "kiteconnect not installed")
class RealSdkTest(unittest.TestCase):
    """The installed SDK itself: route table, method names, and a stripped instance (no request is sent)."""

    def setUp(self):
        import requests.adapters
        p = mock.patch.object(requests.adapters.HTTPAdapter, "send",
                              side_effect=AssertionError("a test tried to reach the network"))
        p.start()
        self.addCleanup(p.stop)

    def test_fake_table_matches_the_installed_sdk(self):
        from kiteconnect import KiteConnect, __version__
        if __version__.__version__ != SDK_VERSION:
            self.skipTest(f"kiteconnect {__version__.__version__}: re-check kite_fakes and the deny-list")
        self.assertEqual(KiteConnect._routes, ROUTES_5_2_2)
        self.assertEqual(kite_client.ORDER_ROUTE_KEYS, EXPECTED_DENY_5_2_2)

    def test_every_order_method_of_the_sdk_is_bound_to_refuse(self):
        from kiteconnect import KiteConnect
        writers = {n for n in dir(KiteConnect) if n.startswith(("place_", "modify_", "cancel_", "exit_",
                                                                  "delete_", "convert_"))}
        self.assertEqual(writers, set(ORDER_METHODS_5_2_2))
        kc = kite_client.strip_order_routes(KiteConnect(api_key="test-key"))
        for name in sorted(writers):
            with self.subTest(name=name), self.assertRaises(kite_client.OrderRouteForbidden):
                getattr(kc, name)(variety="regular", order_id="1", trigger_id=1, sip_id="1")

    def test_real_private_calls_are_refused_before_any_request(self):
        from kiteconnect import KiteConnect
        kc = kite_client.strip_order_routes(KiteConnect(api_key="test-key"))
        for call in (lambda: kc._post("order.place", url_args={"variety": "regular"}, params={}),
                     lambda: kc._request("gtt.place", "POST"), lambda: kc._request("/orders/regular", "POST"),
                     lambda: KiteConnect.place_order(kc, variety="regular", exchange="NSE", tradingsymbol="X",
                                                     transaction_type="BUY", quantity=1, product="CNC",
                                                     order_type="MARKET"),
                     lambda: KiteConnect._request(kc, "order.place", "POST", url_args={"variety": "regular"}),
                     lambda: kc.reqsession.post("https://api.kite.trade/orders/regular")):
            with self.assertRaises((kite_client.OrderRouteForbidden, KeyError)):
                call()
        self.assertNotIn("order.place", kc._routes)
        self.assertIn("order.place", KiteConnect._routes)          # the class itself is untouched


class ReadOnlyKiteTest(unittest.TestCase):
    def test_wrapper_refuses_private_and_order_names(self):
        ro = kite_readonly.ReadOnlyKite(FakeKite())
        for name in ("_client", "_post", "_request", "place_order", "__dict__", "__class__", "__closure__",
                     "orders", "positions", "holdings", "reqsession", "access_token", "set_access_token"):
            with self.subTest(name=name), self.assertRaises(kite_readonly.ForbiddenKiteCall):
                getattr(ro, name)

    def test_wrapper_cannot_be_given_attributes(self):
        ro = kite_readonly.ReadOnlyKite(FakeKite())
        with self.assertRaises((kite_readonly.ForbiddenKiteCall, AttributeError)):
            ro.place_order = lambda **_: None

    def test_allowed_reads_pass_through(self):
        fake = FakeKite()
        ro = kite_readonly.ReadOnlyKite(fake)
        ro.quote(["NSE:RELIANCE"])
        ro.ltp(["NSE:TCS"])
        self.assertEqual([c["name"] for c in fake.calls], ["quote", "ltp"])

    def test_allowed_names_are_exactly_the_brief(self):
        self.assertEqual(kite_readonly.ALLOWED, frozenset({
            "basket_order_margins", "order_margins", "get_virtual_contract_note", "quote", "ltp", "ohlc",
            "profile", "margins", "historical_data", "instruments"}))

    def test_consider_positions_true_is_refused_and_false_is_sent(self):
        fake = FakeKite()
        ro = kite_readonly.ReadOnlyKite(fake)
        for bad in (True, 1, "true", None):
            with self.subTest(bad=bad), self.assertRaises(kite_readonly.ForbiddenKiteCall):
                ro.basket_order_margins([], consider_positions=bad)
        self.assertEqual(fake.calls, [])
        ro.basket_order_margins([])
        self.assertIs(fake.calls[-1]["consider_positions"], False)
        ro.basket_order_margins([], consider_positions=False, mode="compact")
        self.assertIs(fake.calls[-1]["consider_positions"], False)
        self.assertEqual(fake.calls[-1]["mode"], "compact")

    def test_order_margins_sends_consider_positions_false(self):
        fake = FakeKite()
        ro = kite_readonly.ReadOnlyKite(fake)
        with self.assertRaises(kite_readonly.ForbiddenKiteCall):
            ro.order_margins([], consider_positions=True)
        ro.order_margins([{"tradingsymbol": "X"}], mode="compact")
        call = fake.calls[-1]
        self.assertEqual(call["route"], "order.margins")
        self.assertIs(call["query_params"]["consider_positions"], False)
        self.assertEqual(call["query_params"]["mode"], "compact")

    def test_wrapper_over_the_stripped_client(self):
        with mock.patch.object(kite_client, "KiteConnect", FakeKiteConnect):
            inner = kite_client.strip_order_routes(FakeKiteConnect(api_key="k"))
        ro = kite_readonly.ReadOnlyKite(inner)
        ro.order_margins([])
        ro.basket_order_margins([])
        self.assertEqual([s[0] for s in inner.sent], ["order.margins", "order.margins.basket"])
        self.assertIs(inner.sent[-1][4]["consider_positions"], False)

    def test_client_passes_auth_errors_through(self):
        err = kite_client.KiteTokenExpired("expired")
        with mock.patch.object(kite_client, "get_client", side_effect=err):
            with self.assertRaises(kite_client.KiteTokenExpired) as ctx:
                kite_readonly.client()
        self.assertIs(ctx.exception, err)

    def test_client_wraps_get_client(self):
        fake = FakeKite()
        with mock.patch.object(kite_client, "get_client", return_value=fake):
            ro = kite_readonly.client()
        self.assertIsInstance(ro, kite_readonly.ReadOnlyKite)
        ro.quote(["NSE:X"])
        self.assertEqual(fake.calls[-1]["name"], "quote")


class SourceScanTest(unittest.TestCase):
    """No tracked or new source outside tests/ names an order method, route key or URL (but the deny-list)."""

    def _scan(self, patterns) -> list[str]:
        matches = []
        for path in tracked_sources():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            allowed = path.name == "kite_client.py" and path.parent == REPO
            inside = False
            for no, line in enumerate(lines, 1):
                if allowed and line.startswith(DENY_BEGIN):
                    inside = True
                elif allowed and line.startswith(DENY_END):
                    inside = False
                elif not inside and any(p.search(line) for p in patterns):
                    matches.append(f"{path.relative_to(REPO).as_posix()}:{no}")
        return matches

    def test_the_scan_sees_the_repo(self):
        names = {p.relative_to(REPO).as_posix() for p in tracked_sources()}
        self.assertIn("kite_client.py", names)
        self.assertIn("stockify/kite_readonly.py", names)
        self.assertIn("run.ps1", names)
        self.assertFalse(any(n.startswith("tests/") for n in names))

    def test_no_source_calls_an_order_method_or_route(self):
        self.assertEqual(self._scan([METHODS]), [])

    def test_no_source_names_an_order_route_key_or_url(self):
        self.assertEqual(self._scan([ROUTES, URLS]), [])

    def test_the_deny_list_block_exists_once(self):
        text = (REPO / "kite_client.py").read_text(encoding="utf-8")
        self.assertEqual(text.count(DENY_BEGIN), 1)
        self.assertEqual(text.count(DENY_END), 1)

    def test_only_kite_client_builds_a_kiteconnect_or_touches_its_internals(self):
        pattern = re.compile(r"\bKiteConnect\s*\(|\._routes\b|\breqsession\b")
        hits = [m for m in self._scan([pattern]) if not m.startswith("kite_client.py:")]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
