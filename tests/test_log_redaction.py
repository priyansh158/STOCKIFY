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


if __name__ == "__main__":
    unittest.main()
