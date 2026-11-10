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

    def test_the_guards_wrap_the_whole_app_in_the_right_order(self):
        from fastapi.middleware.cors import CORSMiddleware
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        from server import local_only
        stack = self.main.app.user_middleware                   # outermost first
        self.assertEqual([m.cls for m in stack], [TrustedHostMiddleware, local_only.ForwardedHostOnly,
                                                  local_only.WebOriginOnly, CORSMiddleware])
        self.assertEqual(list(stack[0].kwargs["allowed_hosts"]), ["127.0.0.1", "localhost"])
        self.assertEqual(stack[2].kwargs["web_origin"], self.main._WEB_ORIGIN)
        self.assertEqual(stack[3].kwargs["allow_origins"], [self.main._WEB_ORIGIN])


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


if __name__ == "__main__":
    unittest.main()
