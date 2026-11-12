"""server.log_redaction: Kite login secrets never reach the access log (I-10)."""
from __future__ import annotations

import io
import logging
import unittest

import tests.support  # noqa: F401  — must precede project imports

from server import log_redaction


class RedactionTests(unittest.TestCase):
    def test_request_token_in_the_callback_path_is_blanked(self):
        path = "/api/kite/callback?action=login&status=success&request_token=gCNlIuG3UJZ"
        self.assertEqual(log_redaction.redact(path),
                         "/api/kite/callback?action=login&status=success&request_token=***")

    def test_other_parameters_and_paths_are_untouched(self):
        for text in ("/api/health", "/api/search?q=RELIANCE&limit=10", "/portfolio?status=ok"):
            self.assertEqual(log_redaction.redact(text), text)

    def test_every_sensitive_name_case_insensitively(self):
        out = log_redaction.redact("a?ACCESS_TOKEN=x&api_secret=y&checksum=z&request_token=w")
        self.assertEqual(out, "a?ACCESS_TOKEN=***&api_secret=***&checksum=***&request_token=***")

    def test_filter_rewrites_uvicorn_access_records(self):
        # tests/run_tests.py silences logging globally; re-enable it for this test.
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)
        logger = logging.getLogger("test.uvicorn.access")
        logger.propagate = False
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        log_redaction.install("test.uvicorn.access")
        log_redaction.install("test.uvicorn.access")   # idempotent
        self.assertEqual(sum(isinstance(f, log_redaction.RedactQueryFilter) for f in logger.filters), 1)
        # uvicorn's access format: client, method, path, http version, status
        logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET",
                    "/api/kite/callback?request_token=SECRET123", "1.1", 302)
        self.assertNotIn("SECRET123", stream.getvalue())
        self.assertIn("request_token=***", stream.getvalue())

    def test_header_and_json_forms_are_blanked_too(self):
        """IWB 5.2: error text from the Kite client and `requests` can carry these forms, not only `name=value`."""
        header = "HTTP 403 Authorization: token abcd1234abcd1234:SECRET123tok rejected"
        out = log_redaction.redact(header)
        self.assertNotIn("SECRET123tok", out)
        self.assertNotIn("abcd1234abcd1234", out)
        self.assertIn("HTTP 403", out)
        body = log_redaction.redact('{"status": "error", "access_token": "SECRET123tok", "n": 1}')
        self.assertNotIn("SECRET123tok", body)
        self.assertIn('"status": "error"', body)
        self.assertIn('"n": 1', body)
        single = log_redaction.redact("{'api_secret': 'SECRET123tok'}")
        self.assertNotIn("SECRET123tok", single)
        self.assertEqual(log_redaction.redact("access_token=SECRET123tok&x=1"), "access_token=***&x=1")


class TracebackRedactionTests(unittest.TestCase):
    """The differential review's M2: a traceback is formatted after the filters run, so the formatter scrubs it."""
    SECRET = "SECRET123tok"

    def logger_to(self, stream):
        from stockify import log as slog
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)
        logger = logging.getLogger("test.stockify.traceback")
        logger.propagate = False
        handler = logging.StreamHandler(stream)
        handler.setFormatter(slog.RedactFormatter("%(levelname)s %(message)s"))
        handler.addFilter(slog.RedactFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        return logger

    def test_log_exception_with_a_token_in_the_exception_is_scrubbed(self):
        stream = io.StringIO()
        logger = self.logger_to(stream)
        try:
            raise ConnectionError(f"handshake failed for wss://ws.kite.trade/?api_key=K&access_token={self.SECRET}")
        except ConnectionError:
            logger.exception("ticker error")
        out = stream.getvalue()
        self.assertIn("ticker error", out)
        self.assertIn("ConnectionError", out)
        self.assertNotIn(self.SECRET, out)
        self.assertNotIn("ws.kite.trade", out)

    def test_exc_info_chained_causes_stack_text_and_authorization_are_scrubbed(self):
        stream = io.StringIO()
        logger = self.logger_to(stream)
        key = "apikey" + "1234"
        try:
            try:
                raise ValueError(f"Authorization: token {key}:{self.SECRET} was refused")
            except ValueError as inner:
                raise RuntimeError(f'{{"access_token": "{self.SECRET}"}}') from inner
        except RuntimeError:
            logger.error("failed", exc_info=True, stack_info=True)
        out = stream.getvalue()
        self.assertIn("RuntimeError", out)
        self.assertNotIn(self.SECRET, out)
        self.assertNotIn("apikey1234", out)

    def test_setup_puts_the_redacting_formatter_on_every_root_handler(self):
        from stockify import log as slog
        handler = logging.StreamHandler(io.StringIO())
        logging.root.addHandler(handler)
        self.addCleanup(logging.root.removeHandler, handler)
        slog.setup()
        self.assertIsInstance(handler.formatter, slog.RedactFormatter)

    def test_a_log_file_that_cannot_be_opened_never_stops_the_daemon(self):
        """M5 final re-review open item 3: an unwritable logs folder must not stop the daemon before the recorder."""
        import sys
        from unittest import mock
        from stockify import log as slog
        before = list(logging.root.handlers)
        self.addCleanup(setattr, sys, "excepthook", sys.excepthook)
        with mock.patch.object(slog, "file_handler", side_effect=PermissionError(13, "denied")):
            slog.setup(file="stockify-daemon.log")                      # must not raise
        added = [h for h in logging.root.handlers if h not in before]
        for h in added:
            self.addCleanup(logging.root.removeHandler, h)
        self.assertFalse([h for h in added if isinstance(h, logging.FileHandler)])  # console only

    def test_the_log_file_rotates_at_the_configured_size(self):
        import tempfile
        from pathlib import Path
        from stockify import log as slog
        with tempfile.TemporaryDirectory() as d:
            handler = slog.file_handler(Path(d) / "x.log", max_bytes=2000, backups=2)
            try:
                logger = logging.getLogger("test.stockify.rotate")
                logger.propagate = False
                logger.addHandler(handler)
                logger.setLevel(logging.INFO)
                previous = logging.root.manager.disable
                logging.disable(logging.NOTSET)
                self.addCleanup(logging.disable, previous)
                for i in range(200):
                    logger.info("line %d access_token=%s", i, self.SECRET)
            finally:
                handler.close()
            names = sorted(p.name for p in Path(d).iterdir())
            self.assertEqual(names, ["x.log", "x.log.1", "x.log.2"])
            self.assertTrue(all(p.stat().st_size <= 2200 for p in Path(d).iterdir()))
            self.assertNotIn(self.SECRET, "".join((Path(d) / n).read_text(encoding="utf-8") for n in names))

    def test_the_shipped_log_caps_are_read_from_storage_json(self):
        from stockify import log as slog
        self.assertEqual(slog.configured_caps(), (20 * 1024 * 1024, 5))


if __name__ == "__main__":
    unittest.main()
