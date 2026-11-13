"""Logging setup shared by the STOCKIFY CLI and daemon."""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_URL = re.compile(r"\b(?:wss?|https?)://\S+", re.IGNORECASE)
# key=value, "key": "value" and key: value forms of the Kite secrets.
_SECRET_PARAM = re.compile(r"""(["']?)\b(access_token|api_key|api_secret|request_token|refresh_token|enctoken)\b\1"""
                           r"""(\s*[:=]\s*)(["']?)[^&\s'",}]*""", re.IGNORECASE)
_AUTH_HEADER = re.compile(r"\btoken\s+[\w.-]+:[\w.-]+", re.IGNORECASE)     # Kite's "Authorization: token key:tok"
DEFAULT_MAX_BYTES, DEFAULT_BACKUPS = 20 * 1024 * 1024, 5


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


class RedactFormatter(logging.Formatter):
    """Redacts the whole formatted record: the message, and also the traceback (`exc_info`, its chained causes) and
    the stack text, which a Filter never sees because the formatter appends them after the filters ran."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configured_caps() -> tuple[int, int]:
    """(bytes a log file may reach before it rotates, rotated files kept) from storage.json `log_max_mb` and
    `log_backups`. A config that cannot be read never stops logging: the defaults apply."""
    try:
        from stockify.config import loader
        doc = loader.load_json("storage.json")
        return int(doc["log_max_mb"]) * 1024 * 1024, int(doc["log_backups"])
    except Exception:  # noqa: BLE001
        return DEFAULT_MAX_BYTES, DEFAULT_BACKUPS


def _open_append_shared(path: str, encoding: str):  # noqa: ANN202
    """A text stream appending to `path` with FILE_SHARE_READ | WRITE | DELETE (LCM 6.4, D-129). Python's `open`
    does not share DELETE, so a second process rotating (renaming) a log this process has open fails on Windows.
    Append-only access (FILE_APPEND_DATA) keeps each write at the end of the file whoever else writes."""
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    handle = k32.CreateFileW(path, 0x0004, 0x7, None, 4, 0x80, None)     # FILE_APPEND_DATA, share all, OPEN_ALWAYS
    if handle in (None, ctypes.c_void_p(-1).value):
        code = ctypes.get_last_error()
        raise OSError(0, ctypes.FormatError(code).strip(), path, code)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_APPEND | os.O_WRONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        k32.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, "a", encoding=encoding)
    except BaseException:
        os.close(fd)
        raise


class SharedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A RotatingFileHandler for a file several processes log to. The stream is opened sharing DELETE, so another
    process can rename it; and a rotation that still fails (a rename onto a file another process holds) is skipped
    and retried on a later record instead of raising out of `handleError`: the file may pass its cap for a moment,
    no line is lost, and the handler goes on writing."""

    def _open(self):  # noqa: ANN202
        if os.name != "nt":
            return super()._open()
        return _open_append_shared(os.fspath(self.baseFilename), self.encoding or "utf-8")

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                self.stream = self._open()


def file_handler(path: Path, *, max_bytes: int, backups: int) -> logging.Handler:
    """A size-capped rotating log file whose every line is redacted, tracebacks included."""
    handler = SharedRotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(RedactFormatter(_FORMAT))
    handler.addFilter(RedactFilter())
    return handler


def _excepthook(exc_type, exc, tb) -> None:  # noqa: ANN001
    """An uncaught error goes to the log (and so through the redaction) as well as to stderr."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    logging.getLogger("stockify").critical("uncaught %s", exc_type.__name__, exc_info=(exc_type, exc, tb))


def setup(verbose: bool = False, *, file: str | None = None) -> None:
    """Console logging, redacted. With `file` (a name under the logs folder) the same lines also go to that rotating
    file; the launcher no longer captures the process's output into a log (differential review M2)."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=_FORMAT,
    )
    if file:
        try:                                       # a log file that cannot be opened must never stop the recorder
            from stockify import paths
            max_bytes, backups = configured_caps()
            root = logging.getLogger()
            target = paths.logs_dir() / file
            if not any(getattr(h, "baseFilename", None) == str(target) for h in root.handlers):
                root.addHandler(file_handler(target, max_bytes=max_bytes, backups=backups))
        except (OSError, ValueError) as e:
            logging.getLogger("stockify.log").warning(
                "log file %s could not be opened (%s); logging to the console only", file, type(e).__name__)
        sys.excepthook = _excepthook
    for handler in logging.getLogger().handlers:
        if not isinstance(handler.formatter, RedactFormatter):
            handler.setFormatter(RedactFormatter(handler.formatter._fmt if handler.formatter else _FORMAT))
        if not any(isinstance(f, RedactFilter) for f in handler.filters):
            handler.addFilter(RedactFilter())


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"stockify.{name}")
