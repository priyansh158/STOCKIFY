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
URLS = re.compile(r"['\"]/(orders|gtt|mf/orders|mf/sips)\b", re.IGNORECASE)
# Composed or absolute order paths: an order segment that starts a path (string start, `}`, quote, host).
ORDER_PATHS = re.compile(r"(^|[^A-Za-z0-9_.-]|kite\.trade)/(orders|gtt|mf/orders|mf/sips|portfolio/positions)\b",
                         re.IGNORECASE)
# Order segments joined piecewise ("/".join(["", "orders"])), and the GTT/SIP words on their own.
ORDER_SEGMENTS = re.compile(r"join\(.*['\"](orders|gtt|mf|portfolio)['\"]|['\"](gtt|sips|triggers)['\"]")
METHOD_PIECES = re.compile(r"['\"](place|modify|cancel|exit|delete|convert)_['\"]")
KITE_HOST = re.compile(r"kite\.trade|\bapi\.kite\b", re.IGNORECASE)
SDK_IMPORT = re.compile(r"^\s*(import|from)\s+kiteconnect\b")
SDK_STRING = re.compile(r"['\"]kiteconnect(\.[\w.]+)?['\"]")
DENY_BEGIN, DENY_END = "# ORDER-DENY-LIST BEGIN", "# ORDER-DENY-LIST END"


def tracked_sources() -> list[Path]:
    cmd = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py", "*.ps1"]
    out = subprocess.run(cmd, cwd=REPO, capture_output=True, check=True).stdout.decode("utf-8")
    return [REPO / p for p in out.split("\0") if p and not p.startswith(("tests/", ".venv/"))]


def requests_adapter_type() -> type:
    import requests.adapters
    return requests.adapters.BaseAdapter


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

    def test_stripped_margin_calls_send_lowercase_false_and_refuse_true(self):
        k = kite_client.get_client()
        k.basket_order_margins([{"x": 1}])                     # the SDK's default would be True
        k.order_margins([{"x": 1}], mode="compact")
        self.assertEqual(k.sent[-2][4], {"consider_positions": "false"})
        self.assertEqual(k.sent[-1][4], {"consider_positions": "false", "mode": "compact"})
        for bad in (True, "False", 0, None):
            with self.subTest(bad=bad), self.assertRaises(kite_client.OrderRouteForbidden):
                k.basket_order_margins([], consider_positions=bad)

    def test_url_args_traversal_is_refused_before_the_sdk(self):
        k = kite_client.get_client()
        for args in ({"exchange": "../orders/regular"}, {"exchange": "../%6Frders/regular"},
                     {"exchange": "..%2F%2E%2E%2Fgtt/triggers"}, {"exchange": "%252E%252E/mf/sips"}):
            with self.subTest(args=args), self.assertRaises(kite_client.OrderRouteForbidden):
                k._post("market.instruments", url_args=args, params={})
        self.assertEqual(k.reqsession.sent, [])

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
        import requests
        import requests.adapters
        self.wire: list = []                      # every request that reached the real transport (stubbed)

        def stub_send(adapter, request, **kwargs):  # noqa: ANN001, ANN202
            self.wire.append(request)
            response = requests.Response()
            response.status_code = 200
            response.headers["content-type"] = "application/json"
            response._content = b'{"status": "success", "data": {"access_token": "t", "user_id": "AB1", "login_time": "2026-11-09 08:00:00"}}'
            response.request, response.url = request, request.url
            return response
        p = mock.patch.object(requests.adapters.HTTPAdapter, "send", stub_send)
        p.start()
        self.addCleanup(p.stop)

    def _client(self):  # noqa: ANN202
        from kiteconnect import KiteConnect
        return kite_client.strip_order_routes(KiteConnect(api_key="test-key", access_token="test-access"))

    def test_review_reproductions_are_refused_at_the_adapter(self):
        kc = self._client()
        session = kc.reqsession
        probes = [
            lambda: kc._post("market.instruments", url_args={"exchange": "../%6Frders/regular"}, params={"q": 1}),
            lambda: kc._delete("market.instruments", url_args={"exchange": "../%67tt/triggers/1"}),
            lambda: kc._put("market.instruments", url_args={"exchange": "../portfolio/%70ositions"}),
            lambda: kc._post("market.instruments", url_args={"exchange": "..%2forders%2fregular"}),
            lambda: session.request("POST", "https://api.kite.trade/%6Frders/regular"),
            lambda: session.request("POST", "https://api.kite.trade/margins/../orders/regular"),
            lambda: session.request("DELETE", "https://api.kite.trade/gtt/triggers/1"),
            lambda: session.request("PUT", "https://api.kite.trade/portfolio/positions"),
            lambda: session.request("POST", "https://api.kite.trade/margins/basket/../../orders/regular"),
            lambda: session.request("POST", "https://api.kite.trade/user/profile"),
            lambda: session.request("PATCH", "https://api.kite.trade/margins/basket"),
        ]
        kc._routes["sneaky"] = "/%6frders/regular"
        probes.append(lambda: kc._post("sneaky", params={}))
        for i, probe in enumerate(probes):
            with self.subTest(i=i), self.assertRaises(kite_client.OrderRouteForbidden):
                probe()
        self.assertEqual(self.wire, [])

    def test_the_adapter_itself_refuses_a_prepared_order(self):
        import requests
        kc = self._client()
        prepared = requests.Request("POST", "https://api.kite.trade/%6Frders/regular", data={"q": 1}).prepare()
        self.assertEqual(prepared.url, "https://api.kite.trade/orders/regular")   # requests decodes %6F itself
        for prefix in ("https://", "http://"):
            with self.subTest(prefix=prefix), self.assertRaises(kite_client.OrderRouteForbidden):
                kc.reqsession.get_adapter(prefix + "api.kite.trade/").send(prepared)
        self.assertEqual(self.wire, [])

    def test_method_override_headers_and_keys_are_refused(self):
        kc = self._client()
        s = kc.reqsession
        probes = [
            lambda: s.request("GET", "https://api.kite.trade/user/profile", headers={"X-HTTP-Method-Override": "POST"}),
            lambda: s.request("POST", "https://api.kite.trade/session/token",
                              headers={"x-http-method-override": "DELETE"}),
            lambda: s.request("GET", "https://api.kite.trade/user/profile", headers={"X-HTTP-Method": "PUT"}),
            lambda: s.request("POST", "https://api.kite.trade/margins/basket", headers={"X-METHOD-OVERRIDE": "DELETE"}),
            lambda: s.request("GET", "https://api.kite.trade/user/profile?_method=POST"),
            lambda: s.request("GET", "https://api.kite.trade/user/profile", params={"_Method": "DELETE"}),
            lambda: s.request("POST", "https://api.kite.trade/margins/basket", data={"_method": "DELETE"}),
            lambda: s.request("POST", "https://api.kite.trade/margins/basket", json={"_method": "PUT"}),
            lambda: s.request("POST", "https://api.kite.trade/margins/basket", json=[{"_method": "PUT"}]),
        ]
        for i, probe in enumerate(probes):
            with self.subTest(i=i), self.assertRaises(kite_client.OrderRouteForbidden):
                probe()
        self.assertEqual(self.wire, [])

    def test_a_longer_prefix_mount_makes_every_request_refuse(self):
        import requests.adapters
        kc = self._client()
        kc.reqsession.mount("https://api.kite.trade/", requests.adapters.HTTPAdapter())
        for call in (lambda: kc.reqsession.request("POST", "https://api.kite.trade/orders/regular"),
                     lambda: kc.reqsession.request("GET", "https://api.kite.trade/user/profile"),
                     lambda: kc.profile()):
            with self.assertRaises(kite_client.OrderRouteForbidden):
                call()
        self.assertEqual(self.wire, [])

    def test_a_replaced_prefix_mount_makes_every_request_refuse(self):
        import requests.adapters
        kc = self._client()
        kc.reqsession.mount("http://", requests.adapters.HTTPAdapter())   # the https guard notices
        with self.assertRaises(kite_client.OrderRouteForbidden):
            kc.profile()
        self.assertEqual(self.wire, [])

    def test_the_inner_transport_is_not_an_attribute(self):
        kc = self._client()
        guard = kc.reqsession.get_adapter("https://api.kite.trade/")
        self.assertTrue(guard.paper_only)
        self.assertFalse(any(isinstance(v, requests_adapter_type()) for v in vars(guard).values()))

    def test_allowlisted_calls_pass_with_the_exact_query(self):
        kc = self._client()
        kc.basket_order_margins([{"tradingsymbol": "NIFTY"}])
        kc.order_margins([{"tradingsymbol": "NIFTY"}], mode="compact")
        kc.get_virtual_contract_note([{"order_id": "1"}])
        kite_readonly.ReadOnlyKite(kc).basket_order_margins([])
        kc.profile()
        kc.margins()
        sent = [(r.method, r.url) for r in self.wire]
        self.assertEqual(sent, [
            ("POST", "https://api.kite.trade/margins/basket?consider_positions=false"),
            ("POST", "https://api.kite.trade/margins/orders?consider_positions=false&mode=compact"),
            ("POST", "https://api.kite.trade/charges/orders"),
            ("POST", "https://api.kite.trade/margins/basket?consider_positions=false"),
            ("GET", "https://api.kite.trade/user/profile"),
            ("GET", "https://api.kite.trade/user/margins"),
        ])

    def test_login_exchange_passes(self):
        kc = self._client()
        kc.generate_session("req-123", api_secret="test-secret")
        self.assertEqual([(r.method, r.url) for r in self.wire], [("POST", "https://api.kite.trade/session/token")])

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
        self.assertEqual(fake.calls[-1]["route"], "order.margins.basket")
        self.assertEqual(fake.calls[-1]["query_params"], {"consider_positions": "false"})
        ro.basket_order_margins([], consider_positions=False, mode="compact")
        self.assertEqual(fake.calls[-1]["query_params"], {"consider_positions": "false", "mode": "compact"})

    def test_order_margins_sends_consider_positions_false(self):
        fake = FakeKite()
        ro = kite_readonly.ReadOnlyKite(fake)
        with self.assertRaises(kite_readonly.ForbiddenKiteCall):
            ro.order_margins([], consider_positions=True)
        ro.order_margins([{"tradingsymbol": "X"}], mode="compact")
        call = fake.calls[-1]
        self.assertEqual(call["route"], "order.margins")
        self.assertEqual(call["query_params"], {"consider_positions": "false", "mode": "compact"})

    def test_wrapper_over_the_stripped_client(self):
        with mock.patch.object(kite_client, "KiteConnect", FakeKiteConnect):
            inner = kite_client.strip_order_routes(FakeKiteConnect(api_key="k"))
        ro = kite_readonly.ReadOnlyKite(inner)
        ro.order_margins([])
        ro.basket_order_margins([])
        self.assertEqual([s[0] for s in inner.sent], ["order.margins", "order.margins.basket"])
        self.assertEqual(inner.sent[-1][4], {"consider_positions": "false"})

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
    """Backstop scans over every tracked or new .py/.ps1 outside tests/ (the transport adapter is the real guard).

    Allowlists are named and minimal: the deny-list block in kite_client.py, the files listed per scan, and the
    exact lines in OWN_API_LINES.
    """

    # Exact lines that only look like Kite access: our own API's route ending in /orders, and the strategy load
    # check's list of modules it refuses (LCB 5.3).
    OWN_API_LINES = {"server/routers/tests.py": {'@router.get("/api/tests/{run_id}/orders", '
                                                 'response_model=list[dict[str, Any]])'},
                     "stockify/strategy.py": {'BANNED_ATTR_NAMES = {"importlib", "sys", "modules", "kite_client", '
                                              '"kiteconnect", "os", "subprocess", "socket",'}}

    def _scan(self, patterns, allowed_files=()) -> list[str]:  # noqa: ANN001
        matches = []
        for path in tracked_sources():
            rel = path.relative_to(REPO).as_posix()
            if rel in allowed_files:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            owner = rel == "kite_client.py"
            inside = False
            for no, line in enumerate(lines, 1):
                if owner and line.startswith(DENY_BEGIN):
                    inside = True
                elif owner and line.startswith(DENY_END):
                    inside = False
                elif inside or line.strip() in self.OWN_API_LINES.get(rel, ()):
                    continue
                elif any(p.search(line) for p in patterns):
                    matches.append(f"{rel}:{no}")
        return matches

    def test_the_scan_sees_the_repo(self):
        names = {p.relative_to(REPO).as_posix() for p in tracked_sources()}
        self.assertIn("kite_client.py", names)
        self.assertIn("stockify/kite_readonly.py", names)
        self.assertIn("run.ps1", names)
        self.assertFalse(any(n.startswith("tests/") for n in names))

    def test_no_source_calls_an_order_method_or_route(self):
        self.assertEqual(self._scan([METHODS, METHOD_PIECES]), [])

    def test_no_source_names_an_order_route_key_or_url(self):
        self.assertEqual(self._scan([ROUTES, URLS, ORDER_PATHS, ORDER_SEGMENTS]), [])

    def test_the_patterns_catch_composed_urls(self):
        for text in ('requests.post("https://api.kite.trade/orders/regular")', 'f"{root}/orders/{variety}"',
                     "root + '/gtt/triggers'", '"/".join(["", "orders", v])', "'/mf/sips/' + sip",
                     'url = "/portfolio/positions"', 'getattr(k, "place_" + "order")',
                     'requests.post("https://API.KITE.TRADE/orders/regular", headers=h, data=d)',
                     'requests.post("https://Api.Kite.Trade/Orders/regular")'):
            with self.subTest(text=text):
                self.assertTrue(any(p.search(text) for p in (URLS, ORDER_PATHS, ORDER_SEGMENTS, METHOD_PIECES,
                                                             KITE_HOST)))
        for text in ('"/api/margins/orders"', '"/charges/orders"', 'store.run_rows(run_id, "orders")'):
            with self.subTest(text=text):
                self.assertFalse(any(p.search(text) for p in (URLS, ORDER_PATHS, ORDER_SEGMENTS)))

    def test_no_direct_http_to_kite(self):
        self.assertEqual(self._scan([KITE_HOST], allowed_files=("kite_client.py",)), [])

    def test_kiteconnect_is_imported_only_for_the_ticker_and_its_version(self):
        allowed = re.compile(r"^\s*(from kiteconnect import KiteTicker|import kiteconnect\.__version__ as \w+)\s*$")
        hits = []
        for hit in self._scan([SDK_IMPORT, SDK_STRING], allowed_files=("kite_client.py",)):
            rel, no = hit.rsplit(":", 1)
            if not allowed.match((REPO / rel).read_text(encoding="utf-8").splitlines()[int(no) - 1]):
                hits.append(hit)
        self.assertEqual(hits, [])

    def test_only_kite_client_builds_a_kiteconnect_or_touches_its_internals(self):
        pattern = re.compile(r"\bKiteConnect\b|\._routes\b|\breqsession\b|\bkiteconnect\.connect\b|"
                             r"\btype\([^()]*(\([^()]*\))?[^()]*\)\s*\(|\.__class__\s*\(|\btype\(\s*[\w.]*get_client")
        self.assertEqual(self._scan([pattern], allowed_files=("kite_client.py",)), [])
        for text in ("kite_client.get_client().__class__(api_key=k)", "type(kite_client.get_client())",
                     "type(get_client())(api_key=k)"):
            with self.subTest(text=text):
                self.assertTrue(pattern.search(text))

    def test_private_request_methods_only_in_the_two_kite_modules(self):
        pattern = re.compile(r"\._(post|put|delete|request)\s*\(")
        self.assertEqual(self._scan([pattern], allowed_files=("kite_client.py", "stockify/kite_readonly.py")), [])

    def test_kite_ticker_is_built_only_at_the_three_budgeted_sites(self):
        pattern = re.compile(r"\bKiteTicker\s*\(")
        sites = ("kite_ticker_worker.py", "stockify/daemon/ticker.py", "stockify/kitecheck.py")
        self.assertEqual(self._scan([pattern], allowed_files=sites), [])
        for rel in sites:
            text = (REPO / rel).read_text(encoding="utf-8")
            self.assertEqual(len(pattern.findall(text)), 1, rel)
            self.assertIn("conn_budget.register(", text, rel)

    def test_the_deny_list_block_exists_once_and_only_assigns(self):
        lines = (REPO / "kite_client.py").read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(line.startswith(DENY_BEGIN) for line in lines), 1)
        self.assertEqual(sum(line.startswith(DENY_END) for line in lines), 1)
        begin = next(i for i, line in enumerate(lines) if line.startswith(DENY_BEGIN))
        end = next(i for i, line in enumerate(lines) if line.startswith(DENY_END))
        call = re.compile(r"\b(?!frozenset\b)[A-Za-z_]\w*\s*\(")
        for line in lines[begin + 1:end]:
            code = line.split("#", 1)[0]
            with self.subTest(line=line):
                self.assertIsNone(call.search(code))


if __name__ == "__main__":
    unittest.main()
