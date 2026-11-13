"""Keep Kite login secrets out of the API logs (invariant I-10).

uvicorn's access log prints the full request path, so the Kite redirect
`/api/kite/callback?request_token=…` would land in the log. `install()`
adds a filter to the `uvicorn.access` logger that replaces the value of
any sensitive query parameter with `***` before the line is formatted.

`redact` also blanks the two other forms that exception text from the Kite client and `requests` can carry
(IWB 5.2): a JSON field (`"access_token": "…"`) and Kite's authorization header
(`Authorization: token api_key:access_token`).
"""
from __future__ import annotations

import logging
import re

SENSITIVE_PARAMS = ("request_token", "access_token", "api_secret", "checksum")
_JSON_NAMES = SENSITIVE_PARAMS + ("refresh_token", "api_key", "enctoken")

_PATTERN = re.compile(r"(?i)\b(" + "|".join(SENSITIVE_PARAMS) + r")=[^&\s\"']*")
_JSON_FIELD = re.compile(r"""(?i)(["'])(""" + "|".join(_JSON_NAMES) + r""")\1(\s*:\s*)(["']?)[^&\s'",}]*""")
_AUTH_HEADER = re.compile(r"(?i)\btoken\s+[\w.-]+:[\w.-]+")


def redact(text: str) -> str:
    text = _AUTH_HEADER.sub("token ***", text)
    text = _JSON_FIELD.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}{m.group(4)}***", text)
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
