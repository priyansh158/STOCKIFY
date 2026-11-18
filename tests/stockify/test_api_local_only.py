"""The local API answers this machine's own UI only (audit 2026-11-09: M2, M4, L1).

Three rules, each tested against the real app:
  * the `Host` must be loopback, and so must every `X-Forwarded-Host` (the web server's /api rewrite
    replaces the Host with the API's own and carries the browser's there: a rebound DNS name is refused
    on both paths);
  * a state-changing request or a WebSocket handshake that carries an `Origin` must carry the
    configured web origin (another site's page cannot log the owner out or open the tick stream);
  * one ticks-WebSocket client may hold a bounded number of tokens.

A request with no `Origin` (curl, the CLI, these tests, the UI's server-side fetches) is still served.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports

import kite_client

API = support.API_BASE_URL
WS = API.replace("http://", "ws://")      # TestClient.websocket_connect ignores base_url
EVIL = "http://evil.example"


@support.requires_fastapi
class LocalOnlyBase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        import server.main as main
        support.fresh_cache_dir(type(self).__name__)
        env = support.isolated_env({"KITE_API_KEY": "test-key", "KITE_API_SECRET": "test-secret"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.main = main
        self.web = main._WEB_ORIGIN
        self.client = TestClient(main.app, base_url=API)

    def _fake_login(self):
        kite_client.save_token({"access_token": "test-access", "obtained_at": int(time.time())})
        support.assert_sandboxed(kite_client._token_file())
        self.assertTrue(kite_client._token_file().exists())


class OriginTests(LocalOnlyBase):
    def test_another_sites_page_cannot_log_the_owner_out(self):
        self._fake_login()
        before = kite_client._token_file().read_bytes()
        r = self.client.post("/api/kite/logout", headers={"Origin": EVIL})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(kite_client._token_file().exists())
        self.assertEqual(kite_client._token_file().read_bytes(), before)

    def test_the_web_ui_can_log_out(self):
        self._fake_login()
        r = self.client.post("/api/kite/logout", headers={"Origin": self.web})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"authenticated": False})
        self.assertFalse(kite_client._token_file().exists())

    def test_a_request_with_no_origin_is_served_as_before(self):
        self._fake_login()
        r = self.client.post("/api/kite/logout")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"authenticated": False})
        self.assertFalse(kite_client._token_file().exists())

    def test_every_origin_that_is_not_the_web_origin_is_refused(self):
        self._fake_login()
        near_misses = [
            "null",                                  # a sandboxed frame or a file:// page
            "",                                      # present but empty
            self.web + ".evil.example",
            self.web.replace("http://", "https://"),
            self.web + "0",                          # another port
            self.web + "/",
            "http://localhost.evil.example:3000",
            self.web + ", " + EVIL,
        ]
        for origin in near_misses:
            with self.subTest(origin=origin):
                r = self.client.post("/api/kite/logout", headers={"Origin": origin})
                self.assertEqual(r.status_code, 403)
                self.assertTrue(kite_client._token_file().exists())

    def test_two_origin_headers_are_refused_when_either_is_foreign(self):
        self._fake_login()
        for pair in ([("Origin", self.web), ("Origin", EVIL)], [("Origin", EVIL), ("Origin", self.web)]):
            with self.subTest(pair=pair):
                r = self.client.post("/api/kite/logout", headers=pair)
                self.assertEqual(r.status_code, 403)
                self.assertTrue(kite_client._token_file().exists())

    def test_every_state_changing_method_is_covered_before_routing(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "FOO"):
            for path in ("/api/kite/logout", "/api/tests", "/api/tests/abc/cancel", "/no/such/route"):
                with self.subTest(method=method, path=path):
                    r = self.client.request(method, path, headers={"Origin": EVIL})
                    self.assertEqual(r.status_code, 403)

    def test_reads_are_not_refused_by_origin(self):
        # A foreign page may send a GET; CORS (unchanged) keeps it from reading the answer.
        for method in ("GET", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                r = self.client.request(method, "/api/health", headers={"Origin": EVIL})
                self.assertNotEqual(r.status_code, 403)
                self.assertNotIn("access-control-allow-origin", r.headers)
        r = self.client.get("/api/health", headers={"Origin": self.web})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("access-control-allow-origin"), self.web)

    def test_the_origin_compared_is_the_configured_web_origin(self):
        import stockify_paths
        from server import local_only
        with support.isolated_env({"STOCKIFY_WEB_ORIGIN": "http://localhost:3005"}, clear=False):
            origin = stockify_paths.loopback_origin("STOCKIFY_WEB_ORIGIN", "http://127.0.0.1:3000")
        guard = local_only.WebOriginOnly(None, origin)
        self.assertFalse(guard.refuses({"type": "http", "method": "POST",
                                        "headers": [(b"origin", b"http://localhost:3005")]}))
        self.assertTrue(guard.refuses({"type": "http", "method": "POST",
                                       "headers": [(b"origin", b"http://127.0.0.1:3000")]}))
        with self.assertRaises(ValueError):
            local_only.WebOriginOnly(None, "")

    def test_a_configured_origin_with_the_default_port_matches_what_a_browser_sends(self):
        # A browser never puts the scheme's default port in Origin (review m-5).
        import stockify_paths
        from server import local_only
        with support.isolated_env({"STOCKIFY_WEB_ORIGIN": "http://127.0.0.1:80"}, clear=False):
            origin = stockify_paths.loopback_origin("STOCKIFY_WEB_ORIGIN", "http://127.0.0.1:3000")
        self.assertEqual(origin, "http://127.0.0.1")
        guard = local_only.WebOriginOnly(None, origin)
        self.assertFalse(guard.refuses({"type": "http", "method": "POST", "headers": [(b"origin", b"http://127.0.0.1")]}))
        self.assertTrue(guard.refuses({"type": "http", "method": "POST", "headers": [(b"origin", b"http://127.0.0.1:3000")]}))
        # The class on its own normalises both sides, and only the scheme's own default port.
        for configured, sent, refused in (("http://localhost:80", b"http://localhost", False),
                                          ("http://localhost", b"http://localhost:80", False),
                                          ("HTTP://LocalHost:80", b"http://localhost", False),
                                          ("https://localhost:443", b"https://localhost", False),
                                          ("http://localhost:443", b"http://localhost", True),
                                          ("https://localhost:80", b"https://localhost", True),
                                          ("http://localhost:8080", b"http://localhost", True),
                                          ("http://localhost:80", b"http://localhost:8080", True),
                                          ("http://localhost:3000", b"http://localhost:30000", True)):
            with self.subTest(configured=configured, sent=sent):
                scope = {"type": "websocket", "headers": [(b"origin", sent)]}
                self.assertEqual(local_only.WebOriginOnly(None, configured).refuses(scope), refused)

    def test_the_guards_wrap_the_whole_app_in_the_right_order(self):
        from fastapi.middleware.cors import CORSMiddleware
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        from server import local_only
        stack = self.main.app.user_middleware                   # outermost first
        self.assertEqual([m.cls for m in stack], [TrustedHostMiddleware, local_only.ForwardedHostOnly,
                                                  local_only.WebOriginOnly, CORSMiddleware])
        # The real app follows the owner's STOCKIFY_REMOTE_HOST (RemoteHost* below test both settings).
        remote = self.main._REMOTE_HOST
        extra_host, extra_origin = ([remote], [f"https://{remote}"]) if remote else ([], [])
        self.assertEqual(list(stack[0].kwargs["allowed_hosts"]), ["127.0.0.1", "localhost", *extra_host])
        self.assertEqual(stack[1].kwargs["remote_host"], remote)
        self.assertEqual(stack[2].kwargs["web_origin"], self.main._WEB_ORIGIN)
        self.assertEqual(stack[2].kwargs["remote_host"], remote)
        self.assertEqual(stack[3].kwargs["allow_origins"], [self.main._WEB_ORIGIN, *extra_origin])


class HostTests(LocalOnlyBase):
    def test_a_host_that_is_not_loopback_is_refused(self):
        for host in ("evil.example", "evil.example:8001", "127.0.0.1.evil.example", "localhost.evil.example:8001",
                     "www.localhost", "0.0.0.0:8001", "192.168.1.10:8001", "[::1]:8001", ""):
            with self.subTest(host=host):
                r = self.client.get("/api/health", headers={"Host": host})
                self.assertEqual(r.status_code, 400)
                self.assertNotIn("stockify-api", r.text)

    def test_loopback_hosts_are_served_with_or_without_a_port(self):
        for host in ("127.0.0.1", "127.0.0.1:8001", "localhost", "localhost:8001", "localhost:9999"):
            with self.subTest(host=host):
                r = self.client.get("/api/health", headers={"Host": host})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json(), {"ok": True, "service": "stockify-api"})

    def test_a_bad_host_is_refused_even_with_the_web_origin(self):
        self._fake_login()
        r = self.client.post("/api/kite/logout", headers={"Host": "evil.example", "Origin": self.web})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(kite_client._token_file().exists())

    # ── the same name arriving through the web server's /api rewrite (review I-1) ──
    # Next's rewrite proxies with changeOrigin: the API sees `Host: 127.0.0.1:8001` and the
    # browser's own Host in `X-Forwarded-Host` (next/dist/server/lib/router-utils/proxy-request.js).
    # A page at a rebound name reads the API through the rewrite unless that header is checked too.

    REWRITE = {"Host": "127.0.0.1:8001"}

    def test_a_rebound_name_coming_through_the_rewrite_is_refused(self):
        self._fake_login()
        for forwarded in ("evil.example:3000", "evil.example", "127.0.0.1.evil.example:3000",
                          "localhost.evil.example", "www.localhost:3000", "[::1]:3000", "192.168.1.10:3000",
                          "", "evil.example:3000, 127.0.0.1:3000", "127.0.0.1:3000, evil.example:3000"):
            for path in ("/api/portfolio/snapshot", "/api/kite/status", "/api/health", "/no/such/route"):
                with self.subTest(forwarded=forwarded, path=path):
                    r = self.client.get(path, headers={**self.REWRITE, "X-Forwarded-Host": forwarded})
                    self.assertEqual(r.status_code, 400)
                    self.assertNotIn("stockify-api", r.text)
                    self.assertNotIn("authenticated", r.text)
        r = self.client.post("/api/kite/logout",
                             headers={**self.REWRITE, "X-Forwarded-Host": "evil.example:3000", "Origin": self.web})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(kite_client._token_file().exists())

    def test_two_forwarded_host_headers_are_refused_when_either_is_foreign(self):
        for pair in ([("X-Forwarded-Host", "127.0.0.1:3000"), ("X-Forwarded-Host", "evil.example:3000")],
                     [("X-Forwarded-Host", "evil.example:3000"), ("X-Forwarded-Host", "127.0.0.1:3000")]):
            with self.subTest(pair=pair):
                r = self.client.get("/api/health", headers=[("Host", "127.0.0.1:8001"), *pair])
                self.assertEqual(r.status_code, 400)

    def test_the_rewrite_from_a_loopback_page_is_served(self):
        for forwarded in ("127.0.0.1:3000", "localhost:3000", "127.0.0.1", "localhost", "localhost:9999"):
            with self.subTest(forwarded=forwarded):
                r = self.client.get("/api/health", headers={**self.REWRITE, "X-Forwarded-Host": forwarded})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json(), {"ok": True, "service": "stockify-api"})

    def test_the_forwarded_host_check_is_the_host_check(self):
        from server import local_only
        guard = local_only.ForwardedHostOnly(None)
        for value, refused in ((b"127.0.0.1:8001", False), (b"localhost", False), (b"LOCALHOST", True),
                               (b"localhost.", True), (b"[::1]", True), (b"127.0.0.1:3000,localhost:3000", False),
                               (b"127.0.0.1:3000,evil.example", True), (b" 127.0.0.1:3000 ", False), (b"", True)):
            with self.subTest(value=value):
                scope = {"type": "http", "method": "GET", "headers": [(b"x-forwarded-host", value)]}
                self.assertEqual(guard.refuses(scope), refused)
        self.assertFalse(guard.refuses({"type": "http", "method": "GET", "headers": []}))
        self.assertFalse(guard.refuses({"type": "lifespan", "headers": [(b"x-forwarded-host", b"evil")]}))


class TicksSocketTests(LocalOnlyBase):
    def setUp(self):
        super().setUp()
        from server.routers import ticks
        from server.services import ticker_bridge
        self.ticks = ticks
        self.bridge = ticker_bridge
        for target, name in ((ticker_bridge, "ensure_subscribed"), (ticker_bridge, "register_client")):
            p = mock.patch.object(target, name, wraps=getattr(target, name))
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        self.ensure_subscribed.side_effect = lambda tokens, mode="ltp": True      # never start the real ticker
        import kite_ticker_worker
        p = mock.patch.object(kite_ticker_worker, "unsubscribe")
        self.unsubscribe = p.start()
        self.addCleanup(p.stop)

    def _logged_in(self, yes: bool):
        p = mock.patch.object(kite_client, "is_authenticated", return_value=yes)
        p.start()
        self.addCleanup(p.stop)

    def _connect(self, query: str = "", headers: dict | None = None):  # noqa: ANN202
        return self.client.websocket_connect(WS + "/api/ws/ticks" + query, headers=dict(headers or {}))

    def _refused(self, headers: dict):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect) as refused:
            with self._connect("?tokens=738561", headers):
                pass  # pragma: no cover
        self.register_client.assert_not_called()
        self.ensure_subscribed.assert_not_called()
        return refused.exception

    def test_a_foreign_origin_cannot_open_the_tick_stream(self):
        from starlette.testclient import WebSocketDenialResponse
        self._logged_in(True)
        for origin in (EVIL, "null", self.web + ".evil.example"):
            with self.subTest(origin=origin):
                why = self._refused({"Origin": origin})
                self.assertNotIsInstance(why, WebSocketDenialResponse)     # the Origin check, not the Host check
                self.assertEqual(why.code, 1008)

    def test_a_bad_host_cannot_open_the_tick_stream(self):
        from starlette.testclient import WebSocketDenialResponse
        self._logged_in(True)
        why = self._refused({"Host": "evil.example", "Origin": self.web})
        self.assertIsInstance(why, WebSocketDenialResponse)
        self.assertEqual(why.status_code, 400)

    def test_a_rebound_name_coming_through_the_rewrite_cannot_open_the_tick_stream(self):
        from starlette.testclient import WebSocketDenialResponse
        self._logged_in(True)
        why = self._refused({"Host": "127.0.0.1:8001", "X-Forwarded-Host": "evil.example:3000", "Origin": self.web})
        self.assertIsInstance(why, WebSocketDenialResponse)
        self.assertEqual(why.status_code, 400)

    def test_the_web_origin_and_no_origin_open_the_tick_stream(self):
        self._logged_in(True)
        for headers in ({"Origin": self.web}, {}):
            with self.subTest(headers=headers):
                self.ensure_subscribed.reset_mock()
                with self._connect("?tokens=738561,408065", headers) as ws:
                    ws.send_json({"subscribe": list(range(1, self.ticks.MAX_TOKENS_PER_CLIENT + 1))})
                    self.assertEqual(ws.receive_json()["type"], "error")      # proves the socket is open and read
                self.ensure_subscribed.assert_called_once_with([738561, 408065], mode="ltp")

    def test_not_logged_in_is_still_answered_with_the_error_frame(self):
        self._logged_in(False)
        with self._connect("", {"Origin": self.web}) as ws:
            frame = ws.receive_json()
        self.assertEqual(frame["type"], "error")
        self.assertIn("not authenticated", frame["message"])

    # ── the token cap ────────────────────────────────────────────────────────

    def _tokens(self, n: int, start: int = 1000) -> list[int]:
        return list(range(start, start + n))

    def _subscribed(self) -> list[int]:
        return [t for call in self.ensure_subscribed.call_args_list for t in call.args[0]]

    def test_the_cap_is_what_the_ui_needs_and_no_more(self):
        self.assertEqual(self.ticks.MAX_TOKENS_PER_CLIENT, 200)

    def test_a_query_string_at_the_cap_is_served(self):
        self._logged_in(True)
        cap = self.ticks.MAX_TOKENS_PER_CLIENT
        query = "?tokens=" + ",".join(map(str, self._tokens(cap)))
        with self._connect(query) as ws:
            ws.send_json({"subscribe": [5]})
            frame = ws.receive_json()
        self.assertEqual(frame["type"], "error")
        self.assertIn(str(cap), frame["message"])
        self.assertEqual(self._subscribed(), self._tokens(cap))

    def test_a_query_string_over_the_cap_is_refused_whole(self):
        from starlette.websockets import WebSocketDisconnect
        self._logged_in(True)
        cap = self.ticks.MAX_TOKENS_PER_CLIENT
        query = "?tokens=" + ",".join(map(str, self._tokens(cap + 1)))
        with self._connect(query) as ws:
            frame = ws.receive_json()
            self.assertEqual(frame["type"], "error")
            self.assertIn(str(cap), frame["message"])
            with self.assertRaises(WebSocketDisconnect) as closed:
                ws.receive_json()
            self.assertEqual(closed.exception.code, 1008)
        self.ensure_subscribed.assert_not_called()
        self.register_client.assert_not_called()

    def test_repeats_of_one_token_count_once(self):
        self._logged_in(True)
        cap = self.ticks.MAX_TOKENS_PER_CLIENT
        query = "?tokens=" + ",".join(["738561"] * (cap * 3))
        with self._connect(query) as ws:
            ws.send_json({"subscribe": self._tokens(cap - 1) + [738561, "738561"]})     # exactly the cap in all
            ws.send_json({"subscribe": [7]})                                           # one too many
            self.assertEqual(ws.receive_json()["type"], "error")
        self.assertEqual(sorted(set(self._subscribed())), sorted([738561] + self._tokens(cap - 1)))
        self.assertNotIn(7, self._subscribed())

    def test_a_subscribe_message_over_the_cap_is_refused_whole_and_the_socket_stays_usable(self):
        self._logged_in(True)
        cap = self.ticks.MAX_TOKENS_PER_CLIENT
        with self._connect("?tokens=1,2") as ws:
            ws.send_json({"subscribe": self._tokens(10_000)})
            self.assertEqual(ws.receive_json()["type"], "error")
            ws.send_json({"subscribe": self._tokens(cap - 1)})                          # 2 + cap - 1 > cap
            self.assertEqual(ws.receive_json()["type"], "error")
            ws.send_json({"subscribe": self._tokens(cap - 2)})                          # fits
            ws.send_json({"subscribe": [999_999]})                                      # does not
            self.assertEqual(ws.receive_json()["type"], "error")
        self.assertEqual(self._subscribed(), [1, 2] + self._tokens(cap - 2))

    def test_unsubscribing_gives_the_room_back(self):
        self._logged_in(True)
        cap = self.ticks.MAX_TOKENS_PER_CLIENT
        query = "?tokens=" + ",".join(map(str, self._tokens(cap)))
        with self._connect(query) as ws:
            ws.send_json({"unsubscribe": [1000, 1001]})
            ws.send_json({"subscribe": [5, 6]})                                         # fits again
            ws.send_json({"subscribe": [7]})                                            # full again
            self.assertEqual(ws.receive_json()["type"], "error")
        self.unsubscribe.assert_called_once_with({1000, 1001})
        self.assertEqual(self._subscribed(), self._tokens(cap) + [5, 6])

    def test_unsubscribing_tokens_this_client_never_held_frees_nothing_and_touches_nothing(self):
        # Another socket's holdings are not this client's to drop, and give it no room (review m-4).
        self._logged_in(True)
        cap = self.ticks.MAX_TOKENS_PER_CLIENT
        query = "?tokens=" + ",".join(map(str, self._tokens(cap)))
        with self._connect(query) as ws:
            ws.send_json({"unsubscribe": [738561, 408065]})                                # never held here
            ws.send_json({"subscribe": [5]})                                               # still full
            self.assertEqual(ws.receive_json()["type"], "error")
            ws.send_json({"unsubscribe": [1000, 738561, "738561", -1]})                    # one of them held
            ws.send_json({"subscribe": [5]})                                               # one place freed
            ws.send_json({"subscribe": [6]})                                               # full again
            self.assertEqual(ws.receive_json()["type"], "error")
        self.unsubscribe.assert_called_once_with({1000})
        self.assertEqual(self._subscribed(), self._tokens(cap) + [5])


# ── STOCKIFY_REMOTE_HOST: the one Tailscale host, for the Kite login from the phone ──────────────────
# docs/superpowers/specs/2026-11-18-phone-login-tailscale-design.md. Off (unset, empty or invalid) the
# guards are exactly loopback; on, exactly one more name and one more Origin are accepted.

TS_HOST = "laptop.tail1234.ts.net"
TS_ORIGIN = "https://" + TS_HOST
# Names and origins that look like the remote host and must never pass.
TS_HOST_NEAR_MISSES = ("other.tail1234.ts.net", "laptop.tail9999.ts.net", TS_HOST + ".evil.com", "evil" + TS_HOST,
                       "x." + TS_HOST, TS_HOST.upper(), "Laptop.tail1234.ts.net", TS_HOST + ".", "tail1234.ts.net",
                       "ts.net", "evil.ts.net", TS_HOST + ",evil.example")
TS_ORIGIN_NEAR_MISSES = ("http://" + TS_HOST, TS_ORIGIN.upper(), "https://Laptop.tail1234.ts.net",
                         TS_ORIGIN + ".evil.com", "https://evil" + TS_HOST, "https://other.tail1234.ts.net",
                         TS_ORIGIN + ":443", TS_ORIGIN + ":3000", TS_ORIGIN + "/", "wss://" + TS_HOST, TS_HOST,
                         TS_ORIGIN + ", " + EVIL, "null", "")


class RemoteHostSettingTests(unittest.TestCase):
    def _read(self, value):
        import os
        import stockify_paths
        with support.isolated_env({}, clear=False):
            os.environ.pop("STOCKIFY_REMOTE_HOST", None)
            if value is not None:
                os.environ["STOCKIFY_REMOTE_HOST"] = value
            return stockify_paths.remote_host()

    def test_valid_tailscale_host_names_are_taken(self):
        for value, expected in ((TS_HOST, TS_HOST), ("  " + TS_HOST + "\t", TS_HOST),
                                ("my-pc.tail-net.ts.net", "my-pc.tail-net.ts.net"),
                                ("a.b.c.ts.net", "a.b.c.ts.net"), ("a1.b2.ts.net", "a1.b2.ts.net")):
            with self.subTest(value=value):
                self.assertEqual(self._read(value), expected)

    def test_absent_or_empty_is_off(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(self._read(value))

    def test_anything_else_is_ignored(self):
        for value in ("https://" + TS_HOST, "http://" + TS_HOST, TS_HOST + ":443", TS_HOST + "/",
                      "*.tail1234.ts.net", "**.ts.net", TS_HOST.upper(), "Laptop.tail1234.ts.net", TS_HOST + ".",
                      "tail1234.ts.net", "ts.net", ".ts.net", "laptop..ts.net", "-laptop.tail1234.ts.net",
                      "laptop-.tail1234.ts.net", "laptop.tail1234.ts.net.evil.com", "laptop.tail1234.ts.network",
                      "laptop.tail1234.ts_net", "laptop.tail1234.tsnet", "localhost", "127.0.0.1", "evil.example",
                      TS_HOST + ",evil.ts.net", TS_HOST + " evil.ts.net", "lap top.tail1234.ts.net",
                      "laptop.tail1234.ts.net\nx", "läptop.tail1234.ts.net", "a" * 64 + ".tail1234.ts.net",
                      ("a" * 60 + ".") * 5 + "ts.net", "user@" + TS_HOST):
            with self.subTest(value=value):
                self.assertIsNone(self._read(value))

    def test_an_ignored_value_is_logged_once(self):
        import logging
        import stockify_paths
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)                  # tests/run_tests.py silences logging globally
        self.addCleanup(logging.disable, previous)
        stockify_paths._remote_host_warned.discard("https://warn-once.tail1234.ts.net")
        with self.assertLogs("stockify.paths", level="WARNING") as logs:
            for _ in range(3):
                self.assertIsNone(self._read("https://warn-once.tail1234.ts.net"))
        self.assertEqual(len(logs.records), 1)
        self.assertIn("STOCKIFY_REMOTE_HOST is ignored", logs.output[0])
        self.assertNotIn("warn-once", logs.output[0])


class RemoteHostGuardTests(unittest.TestCase):
    """The guard classes on their own, with the setting off and on."""

    WEB = "http://127.0.0.1:3000"

    def _fwd(self, value: bytes):
        return {"type": "http", "method": "GET", "headers": [(b"x-forwarded-host", value)]}

    def _post(self, origin: bytes):
        return {"type": "http", "method": "POST", "headers": [(b"origin", origin)]}

    def test_allowed_hosts(self):
        from server import local_only
        self.assertEqual(local_only.allowed_hosts(None), ["127.0.0.1", "localhost"])
        self.assertEqual(local_only.allowed_hosts(""), ["127.0.0.1", "localhost"])
        self.assertEqual(local_only.allowed_hosts(TS_HOST), ["127.0.0.1", "localhost", TS_HOST])

    def test_the_guards_take_only_a_valid_tailscale_name(self):
        # A wildcard would mean any host to TrustedHostMiddleware: the guards refuse to be built with one.
        from server import local_only
        for bad in ("*", "*.ts.net", "evil.example", "https://" + TS_HOST, TS_HOST.upper(), " " + TS_HOST, "ts.net"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    local_only.allowed_hosts(bad)
                with self.assertRaises(ValueError):
                    local_only.ForwardedHostOnly(None, remote_host=bad)
                with self.assertRaises(ValueError):
                    local_only.WebOriginOnly(None, self.WEB, remote_host=bad)

    def test_forwarded_host_off_refuses_the_tailscale_host(self):
        from server import local_only
        for guard in (local_only.ForwardedHostOnly(None), local_only.ForwardedHostOnly(None, remote_host=None),
                      local_only.ForwardedHostOnly(None, remote_host="")):
            for value in (TS_HOST, TS_HOST + ":443", "127.0.0.1:3000," + TS_HOST):
                with self.subTest(value=value):
                    self.assertTrue(guard.refuses(self._fwd(value.encode())))
            self.assertFalse(guard.refuses(self._fwd(b"127.0.0.1:3000")))

    def test_forwarded_host_on_accepts_exactly_the_one_host(self):
        from server import local_only
        guard = local_only.ForwardedHostOnly(None, remote_host=TS_HOST)
        for value in (TS_HOST, TS_HOST + ":443", TS_HOST + ":3000", " " + TS_HOST + " ", TS_HOST + "," + TS_HOST,
                      "127.0.0.1:3000", "localhost", TS_HOST + ",127.0.0.1:3000"):
            with self.subTest(value=value):
                self.assertFalse(guard.refuses(self._fwd(value.encode())))
        for value in TS_HOST_NEAR_MISSES + ("evil.example", "", TS_HOST + ",evil.example", "LOCALHOST"):
            with self.subTest(value=value):
                self.assertTrue(guard.refuses(self._fwd(value.encode())))

    def test_origin_off_refuses_the_tailscale_origin(self):
        from server import local_only
        for guard in (local_only.WebOriginOnly(None, self.WEB), local_only.WebOriginOnly(None, self.WEB, remote_host=None),
                      local_only.WebOriginOnly(None, self.WEB, remote_host="")):
            self.assertTrue(guard.refuses(self._post(TS_ORIGIN.encode())))
            self.assertFalse(guard.refuses(self._post(self.WEB.encode())))

    def test_origin_on_accepts_exactly_https_of_the_one_host(self):
        from server import local_only
        guard = local_only.WebOriginOnly(None, self.WEB, remote_host=TS_HOST)
        for origin in (TS_ORIGIN, " " + TS_ORIGIN + " ", self.WEB):
            with self.subTest(origin=origin):
                self.assertFalse(guard.refuses(self._post(origin.encode())))
                self.assertFalse(guard.refuses({"type": "websocket", "headers": [(b"origin", origin.encode())]}))
        for origin in TS_ORIGIN_NEAR_MISSES + (EVIL, self.WEB + ".evil.example"):
            with self.subTest(origin=origin):
                self.assertTrue(guard.refuses(self._post(origin.encode())))
                self.assertTrue(guard.refuses({"type": "websocket", "headers": [(b"origin", origin.encode())]}))
        # Two Origin headers: either foreign one refuses, as before.
        self.assertTrue(guard.refuses({"type": "http", "method": "POST",
                                       "headers": [(b"origin", TS_ORIGIN.encode()), (b"origin", EVIL.encode())]}))


class RemoteHostAppTests(LocalOnlyBase):
    """The real routes behind the real guard stack (server.main._install_guards), with the setting off and on."""

    def _app(self, remote_host):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.router.routes.extend(self.main.app.router.routes)
        self.main._install_guards(app, self.web, remote_host)
        env = support.isolated_env({"STOCKIFY_REMOTE_HOST": remote_host or ""}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        return TestClient(app, base_url=API, follow_redirects=False)

    def test_off_the_tailscale_host_is_refused_everywhere(self):
        self._fake_login()
        client = self._app(None)
        self.assertEqual(client.get("/api/health", headers={"Host": TS_HOST}).status_code, 400)
        self.assertEqual(client.get("/api/health", headers={"Host": "127.0.0.1:8001",
                                                            "X-Forwarded-Host": TS_HOST}).status_code, 400)
        self.assertEqual(client.post("/api/kite/logout", headers={"Origin": TS_ORIGIN}).status_code, 403)
        self.assertTrue(kite_client._token_file().exists())
        self.assertNotIn("access-control-allow-origin",
                         client.get("/api/health", headers={"Origin": TS_ORIGIN}).headers)
        r = client.get("/api/health", headers={"Host": "127.0.0.1:8001", "X-Forwarded-Host": "127.0.0.1:3000"})
        self.assertEqual(r.json(), {"ok": True, "service": "stockify-api"})

    def test_on_the_one_host_is_served_and_can_log_out(self):
        self._fake_login()
        client = self._app(TS_HOST)
        for headers in ({"Host": TS_HOST}, {"Host": "127.0.0.1:8001", "X-Forwarded-Host": TS_HOST},
                        {"Host": "127.0.0.1:8001", "X-Forwarded-Host": "127.0.0.1:3000"}):
            with self.subTest(headers=headers):
                r = client.get("/api/health", headers=headers)
                self.assertEqual(r.status_code, 200)
        r = client.get("/api/health", headers={"Origin": TS_ORIGIN})
        self.assertEqual(r.headers.get("access-control-allow-origin"), TS_ORIGIN)
        r = client.post("/api/kite/logout", headers={"Host": "127.0.0.1:8001", "X-Forwarded-Host": TS_HOST,
                                                     "Origin": TS_ORIGIN})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(kite_client._token_file().exists())

    def test_on_every_other_name_and_origin_is_still_refused(self):
        self._fake_login()
        client = self._app(TS_HOST)
        for host in TS_HOST_NEAR_MISSES + ("evil.example",):
            with self.subTest(host=host):
                self.assertEqual(client.get("/api/health", headers={"Host": host}).status_code, 400)
                r = client.get("/api/kite/status", headers={"Host": "127.0.0.1:8001", "X-Forwarded-Host": host})
                self.assertEqual(r.status_code, 400)
        for origin in TS_ORIGIN_NEAR_MISSES + (EVIL,):
            with self.subTest(origin=origin):
                r = client.post("/api/kite/logout", headers={"X-Forwarded-Host": TS_HOST, "Origin": origin})
                self.assertEqual(r.status_code, 403)
                self.assertTrue(kite_client._token_file().exists())
        r = client.get("/api/health", headers={"Origin": "http://" + TS_HOST})
        self.assertNotIn("access-control-allow-origin", r.headers)

    def test_on_the_tick_stream_opens_from_the_tailscale_origin_only(self):
        from starlette.websockets import WebSocketDisconnect
        client = self._app(TS_HOST)
        with mock.patch.object(kite_client, "is_authenticated", return_value=False):
            with client.websocket_connect(WS + "/api/ws/ticks", headers={"X-Forwarded-Host": TS_HOST,
                                                                          "Origin": TS_ORIGIN}) as ws:
                self.assertEqual(ws.receive_json()["type"], "error")      # open: answered "not authenticated"
            with self.assertRaises(WebSocketDisconnect) as refused:
                with client.websocket_connect(WS + "/api/ws/ticks", headers={"X-Forwarded-Host": TS_HOST,
                                                                              "Origin": "http://" + TS_HOST}):
                    pass  # pragma: no cover
            self.assertEqual(refused.exception.code, 1008)


class KiteCallbackReturnTests(LocalOnlyBase):
    """Where /api/kite/callback sends the browser after the token exchange, with the setting off and on."""

    _app = RemoteHostAppTests._app

    def _callback(self, client, headers, token="tok"):
        with mock.patch.object(kite_client, "exchange_request_token", return_value={}) as exchange:
            r = client.get("/api/kite/callback" + (f"?request_token={token}" if token else ""), headers=headers)
        self.assertEqual(r.status_code, 302)
        return r.headers["location"], exchange

    def test_off_the_return_is_the_loopback_web_page(self):
        client = self._app(None)
        for headers in ({}, {"Host": "127.0.0.1:8001", "X-Forwarded-Host": "127.0.0.1:3000"}):
            with self.subTest(headers=headers):
                location, exchange = self._callback(client, headers)
                self.assertEqual(location, self.web + "/kite/callback?status=ok")
                exchange.assert_called_once_with("tok")

    def test_off_an_invalid_setting_returns_to_loopback(self):
        client = self._app(None)
        with support.isolated_env({"STOCKIFY_REMOTE_HOST": "https://" + TS_HOST}, clear=False):
            location, _ = self._callback(client, {})
        self.assertEqual(location, self.web + "/kite/callback?status=ok")

    def test_on_a_login_through_the_tailscale_host_returns_there(self):
        client = self._app(TS_HOST)
        for headers in ({"Host": "127.0.0.1:8001", "X-Forwarded-Host": TS_HOST},
                        {"Host": "127.0.0.1:8001", "X-Forwarded-Host": TS_HOST + ":443"},
                        {"Host": TS_HOST}):
            with self.subTest(headers=headers):
                location, _ = self._callback(client, headers)
                self.assertEqual(location, TS_ORIGIN + "/kite/callback?status=ok")
        location, exchange = self._callback(client, {"X-Forwarded-Host": TS_HOST}, token="")
        self.assertEqual(location, TS_ORIGIN + "/kite/callback?status=err&reason=missing_request_token")
        exchange.assert_not_called()
        with mock.patch.object(kite_client, "exchange_request_token", side_effect=ValueError("x")):
            r = client.get("/api/kite/callback?request_token=t", headers={"X-Forwarded-Host": TS_HOST})
        self.assertEqual(r.headers["location"], TS_ORIGIN + "/kite/callback?status=err&reason=ValueError")

    def test_on_a_login_through_loopback_returns_to_loopback(self):
        client = self._app(TS_HOST)
        for headers in ({}, {"Host": "127.0.0.1:8001", "X-Forwarded-Host": "127.0.0.1:3000"},
                        {"Host": "localhost:8001"}):
            with self.subTest(headers=headers):
                location, _ = self._callback(client, headers)
                self.assertEqual(location, self.web + "/kite/callback?status=ok")

    def test_on_a_foreign_name_never_reaches_the_callback(self):
        client = self._app(TS_HOST)
        for host in TS_HOST_NEAR_MISSES:
            with self.subTest(host=host):
                with mock.patch.object(kite_client, "exchange_request_token") as exchange:
                    r = client.get("/api/kite/callback?request_token=t", headers={"X-Forwarded-Host": host})
                self.assertEqual(r.status_code, 400)
                exchange.assert_not_called()

    def test_the_login_url_is_kites_own_in_both_modes(self):
        for remote in (None, TS_HOST):
            with self.subTest(remote=remote):
                client = self._app(remote)
                headers = {"X-Forwarded-Host": TS_HOST} if remote else {}
                r = client.get("/api/kite/login_url", headers=headers)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["url"], "https://kite.zerodha.com/connect/login?v=3&api_key=test-key")


if __name__ == "__main__":
    unittest.main()
