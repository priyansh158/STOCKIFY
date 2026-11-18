"""Canonical Windows-local paths for STOCKIFY.

All mutable application state lives below ``%LOCALAPPDATA%\\STOCKIFY``.
Keeping this authority separate from Kite prevents unrelated stores from
depending on a private broker-client helper and gives tests one safe seam to
sandbox.
"""
from __future__ import annotations

import logging
import os
import re
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
    try:
        port = parsed.port                      # None without one; ValueError for a non-number or out of range
    except ValueError:
        port = -1
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port == -1
        or parsed.netloc.endswith(":")
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
    # The origin as a browser sends it: lower-case host, and no port when it is http's default.
    return f"http://{parsed.hostname}" + (f":{port}" if port not in (None, 80) else "")


# The one host name, besides loopback, the API and the web server answer: the laptop's private Tailscale
# address (docs/superpowers/specs/2026-11-18-phone-login-tailscale-design.md). `<machine>.<tailnet>.ts.net`,
# lower case, no scheme, no port, no wildcard, no trailing dot; each label 1-63 letters, digits or hyphens,
# not starting or ending with a hyphen. web/src/lib/remoteHost.ts applies the same rule.
_TS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_REMOTE_HOST_RULE = re.compile(rf"(?:{_TS_LABEL}\.){{2,}}ts\.net")
_remote_host_warned: set[str] = set()


def is_remote_host(value: str) -> bool:
    """True when `value` is exactly one valid Tailscale host name (no surrounding space)."""
    return isinstance(value, str) and len(value) <= 253 and _REMOTE_HOST_RULE.fullmatch(value) is not None


def remote_host(env_name: str = "STOCKIFY_REMOTE_HOST") -> str | None:
    """The Tailscale host name in `env_name`, or None when it is unset, empty or not a valid one.

    A value that does not pass the rule is ignored (logged once per value): it never relaxes the guards.
    """
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if is_remote_host(value):
        return value
    if value not in _remote_host_warned:
        _remote_host_warned.add(value)
        logging.getLogger("stockify.paths").warning(
            "%s is ignored: it must be one Tailscale host name such as laptop.tailnet.ts.net "
            "(lower case, no scheme, no port, no wildcard); only loopback is served.", env_name)
    return None
