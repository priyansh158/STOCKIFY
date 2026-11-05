"""Keep Kite login secrets out of the API logs (invariant I-10).

uvicorn's access log prints the full request path, so the Kite redirect
`/api/kite/callback?request_token=…` would land in the log. `install()`
adds a filter to the `uvicorn.access` logger that replaces the value of
any sensitive query parameter with `***` before the line is formatted.
"""
from __future__ import annotations

import logging
import re

SENSITIVE_PARAMS = ("request_token", "access_token", "api_secret", "checksum")

_PATTERN = re.compile(r"(?i)\b(" + "|".join(SENSITIVE_PARAMS) + r")=[^&\s\"']*")


def redact(text: str) -> str:
    return _PATTERN.sub(lambda m: f"{m.group(1)}=***", text)


class RedactQueryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def install(logger_name: str = "uvicorn.access") -> None:
    logger = logging.getLogger(logger_name)
    if not any(isinstance(f, RedactQueryFilter) for f in logger.filters):
        logger.addFilter(RedactQueryFilter())
