"""I-10 secrets scan (LCB 5.1): a planted Kite secret never reaches a log, a status file or any other file.

`plant_secrets` puts one fake value where the real secrets live (the sandboxed token file, the API secret in the
environment, the fake client's access token); the tests then drive the Kite paths and `assert_no_secret` walks every
file of the sandbox except the token file. A failure names only file:line and the pattern, never the secret.
"""
from __future__ import annotations

import logging
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.support as support  # noqa: F401  — must precede project imports

import kite_client
import stockify_paths
from stockify import kite_readonly
from stockify.daemon import conn_budget, exitcodes
from tests.stockify.kite_fakes import FakeKiteConnect

SECRET = "FAKETOKEN-123"
TOKEN_FILE = "kite_token.json"


def plant_secrets(test: unittest.TestCase) -> str:
    """Plant SECRET as the access token (sandboxed token file and the fake client) and as KITE_API_SECRET."""
    root = support.fresh_cache_dir(type(test).__name__).parent.parent
    test._secret_root = root
    env = support.isolated_env({"KITE_API_KEY": "test-key", "KITE_API_SECRET": SECRET}, clear=False)
    env.start()
    test.addCleanup(env.stop)
    for target, value in (("KiteConnect", FakeKiteConnect), ("KITECONNECT_AVAILABLE", True),
                          ("_client", None), ("_client_token_fp", "")):
        p = mock.patch.object(kite_client, target, value)
        p.start()
        test.addCleanup(p.stop)
    kite_client.save_token({"access_token": SECRET, "obtained_at": int(time.time()), "user_name": "Test"})
    assert kite_client.get_client().access_token == SECRET
    return SECRET


def assert_no_secret(root: Path, secret: str) -> None:
    """Fail on the first file under `root` (other than the token file) that holds `secret`: file:line only."""
    needle = secret.encode("utf-8")
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or path.name == TOKEN_FILE:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if needle in data:
            line = data[:data.index(needle)].count(b"\n") + 1
            raise AssertionError(f"{path.relative_to(root).as_posix()}:{line}: planted Kite secret (I-10)")


class _LogCapture:
    """Re-enable logging (tests/run_tests.py silences it) and send every record to a file in the sandbox logs."""

    def __init__(self, test: unittest.TestCase):
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        test.addCleanup(logging.disable, previous)
        self.path = stockify_paths.logs_dir() / "secrets-test.log"
        self.handler = logging.FileHandler(self.path, encoding="utf-8")
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        self.records: list[logging.LogRecord] = []
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.handler)
        capture = self

        class _Keep(logging.Handler):
            def emit(self, record):  # noqa: ANN001
                capture.records.append(record)
        self.keeper = _Keep(logging.DEBUG)
        root.addHandler(self.keeper)
        test.addCleanup(root.setLevel, old_level)
        test.addCleanup(root.removeHandler, self.keeper)
        test.addCleanup(root.removeHandler, self.handler)
        test.addCleanup(self.handler.close)


class _FakeWs:
    url = f"wss://ws.kite.trade?api_key=test-key&access_token={SECRET}"


def _session():  # noqa: ANN202
    from stockify.daemon.ticker import RecorderSession
    clock = mock.Mock()
    clock.now_ns.return_value = 1_000
    manager = mock.MagicMock()
    manager.reference_ids.return_value = set()
    return RecorderSession(mock.MagicMock(), mock.MagicMock(), manager, clock)


class SecretsTest(unittest.TestCase):
    def test_planted_secret_is_found(self):
        root = support.fresh_cache_dir(type(self).__name__).parent.parent
        assert_no_secret(root, SECRET)
        (root / "STOCKIFY" / "logs").mkdir(parents=True, exist_ok=True)
        (root / "STOCKIFY" / "logs" / "x.log").write_text(f"first line\nleaked {SECRET} here\n", encoding="utf-8")
        with self.assertRaises(AssertionError) as ctx:
            assert_no_secret(root, SECRET)
        self.assertIn("STOCKIFY/logs/x.log:2", str(ctx.exception))
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_the_token_file_itself_is_skipped(self):
        secret = plant_secrets(self)
        self.assertTrue((stockify_paths.state_dir() / TOKEN_FILE).exists())
        assert_no_secret(self._secret_root, secret)

    def test_ticker_errors_never_log_the_websocket_url(self):
        secret = plant_secrets(self)
        logs = _LogCapture(self)
        session = _session()
        session._on_close(_FakeWs(), 1006, f"connection lost to {_FakeWs.url}")
        session._on_close(_FakeWs(), 1000, "normal")
        session._on_error(_FakeWs(), 1006, f"error at {_FakeWs.url}: access_token={secret}")
        logs.handler.flush()
        texts = [r.getMessage() for r in logs.records if r.name.startswith("stockify.daemon.ticker")]
        self.assertTrue(texts)
        for text in texts:
            self.assertNotIn(secret, text)
            self.assertNotIn("wss://", text)
        closes = [r for r in logs.records if "closed" in r.getMessage()]
        self.assertEqual(len(closes), 2)                          # every close code, 1000 included
        self.assertTrue(all(r.levelno == logging.ERROR for r in closes))
        self.assertTrue(any("1006" in r.getMessage() for r in closes))
        assert_no_secret(self._secret_root, secret)

    def test_redact_covers_urls_params_json_and_auth_headers(self):
        from stockify.log import redact
        for text in (f"wss://ws.kite.trade?api_key=k&access_token={SECRET}", f"access_token={SECRET}&x=1",
                     f'{{"access_token": "{SECRET}", "user": "AB"}}', f"'api_secret': '{SECRET}'",
                     f"Authorization: token test-key:{SECRET}", f"refresh_token = {SECRET}"):
            with self.subTest(text=text):
                self.assertNotIn(SECRET, redact(text))
        self.assertEqual(redact("code 1006, reason going away"), "code 1006, reason going away")

    def test_redact_filter_cleans_the_sdk_ticker_logger(self):
        from stockify.log import RedactFilter
        logs = _LogCapture(self)
        logs.handler.addFilter(RedactFilter())
        logging.getLogger("kiteconnect.ticker").error("Connection error: %s - %s", 1006,
                                                     f"closed {_FakeWs.url}")
        logs.handler.flush()
        text = logs.path.read_text(encoding="utf-8")
        self.assertIn("Connection error: 1006", text)
        self.assertNotIn(SECRET, text)

    def test_token_error_still_stops_with_76(self):
        session = _session()
        session._on_error(_FakeWs(), 403, "TokenException")
        self.assertEqual(session.exit_code, exitcodes.TOKEN_EXPIRED)

    def test_api_ticker_errors_never_keep_the_websocket_url(self):
        import kite_ticker_worker
        secret = plant_secrets(self)
        sup = kite_ticker_worker._Supervisor()
        sup._on_close(_FakeWs(), 1006, f"lost {_FakeWs.url}")
        sup._on_error(_FakeWs(), 1006, f"error access_token={secret}&x=1")
        status = sup.status()
        self.assertEqual(len(status["errors"]), 2)
        for text in status["errors"]:
            self.assertNotIn(secret, text)
            self.assertNotIn("wss://", text)

    def test_kite_paths_leave_no_secret_behind(self):
        secret = plant_secrets(self)
        logs = _LogCapture(self)
        k = kite_client.get_client()
        with self.assertRaises(kite_client.OrderRouteForbidden):
            k.place_order(variety="regular")
        ro = kite_readonly.client()
        ro.quote(["NSE:RELIANCE"])
        ro.basket_order_margins([])
        with self.assertRaises(kite_readonly.ForbiddenKiteCall):
            ro.access_token  # noqa: B018
        kite_client.status_snapshot()
        ticket = conn_budget.register("api", limit=3)
        ticket.release()
        session = _session()
        session._on_error(_FakeWs(), 403, f"token rejected {_FakeWs.url}")
        logging.getLogger("stockify.test").info("client is %r, read-only is %r", k, ro)
        logs.handler.flush()
        for record in logs.records:
            self.assertNotIn(secret, record.getMessage())
        assert_no_secret(self._secret_root, secret)


if __name__ == "__main__":
    unittest.main()
