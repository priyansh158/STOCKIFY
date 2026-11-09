"""Logging setup shared by the STOCKIFY CLI and daemon."""
from __future__ import annotations

import logging
import re

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_URL = re.compile(r"\b(?:wss?|https?)://\S+", re.IGNORECASE)
_SECRET_PARAM = re.compile(r"\b(access_token|api_key|api_secret|request_token|enctoken)=[^&\s'\"]*", re.IGNORECASE)


def redact(text: object) -> str:
    """Text fit for a log (I-10): URLs (a KiteTicker's carries the access token) and token parameters blanked."""
    return _SECRET_PARAM.sub(r"\1=***", _URL.sub("<url>", str(text)))


def setup(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=_FORMAT,
    )


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"stockify.{name}")
