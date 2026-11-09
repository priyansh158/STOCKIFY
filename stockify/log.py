"""Logging setup shared by the STOCKIFY CLI and daemon."""
from __future__ import annotations

import logging
import re

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_URL = re.compile(r"\b(?:wss?|https?)://\S+", re.IGNORECASE)
# key=value, "key": "value" and key: value forms of the Kite secrets.
_SECRET_PARAM = re.compile(r"""(["']?)\b(access_token|api_key|api_secret|request_token|refresh_token|enctoken)\b\1"""
                           r"""(\s*[:=]\s*)(["']?)[^&\s'",}]*""", re.IGNORECASE)
_AUTH_HEADER = re.compile(r"\btoken\s+[\w.-]+:[\w.-]+", re.IGNORECASE)     # Kite's "Authorization: token key:tok"


def redact(text: object) -> str:
    """Text fit for a log (I-10): URLs (a KiteTicker's carries the access token), token parameters and JSON
    fields, and Kite authorization headers blanked."""
    out = _URL.sub("<url>", str(text))
    out = _AUTH_HEADER.sub("token ***", out)
    return _SECRET_PARAM.sub(r"\1\2\1\3\4***", out)


class RedactFilter(logging.Filter):
    """Redacts every record a handler emits, including the kiteconnect SDK's own ticker errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:                       # a malformed record: leave it to the handler's own error path
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        return True


def setup(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=_FORMAT,
    )
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactFilter) for f in handler.filters):
            handler.addFilter(RedactFilter())


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"stockify.{name}")
