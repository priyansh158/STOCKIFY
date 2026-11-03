"""Logging setup shared by the STOCKIFY CLI and daemon."""
from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=_FORMAT,
    )


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"simlab.{name}")
