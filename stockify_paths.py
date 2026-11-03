"""Canonical Windows-local paths for STOCKIFY.

All mutable application state lives below ``%LOCALAPPDATA%\\STOCKIFY``.
Keeping this authority separate from Kite prevents unrelated stores from
depending on a private broker-client helper and gives tests one safe seam to
sandbox.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parent


def env_file() -> Path:
    return REPO_ROOT / ".env"


def local_appdata() -> Path:
    raw = (os.environ.get("LOCALAPPDATA") or "").strip()
    if not raw:
        raise RuntimeError(
            "LOCALAPPDATA is not set. STOCKIFY requires a Windows user profile."
        )
    return Path(raw)


def app_dir() -> Path:
    path = local_appdata() / "STOCKIFY"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_dir() -> Path:
    path = app_dir() / "kite"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = app_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def loopback_origin(env_name: str, default: str) -> str:
    """Return a normalized HTTP origin restricted to local loopback."""
    value = (os.environ.get(env_name) or default).strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            f"{env_name} must be an HTTP loopback origin, for example "
            f"http://127.0.0.1:3000 (received {value!r})."
        )
    return value
