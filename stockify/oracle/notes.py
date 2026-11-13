"""CH 6.1: the plain-English note a strategy attaches to an order. Twin of engine/src/notes.cpp.

Rules, in order: a lone surrogate becomes U+FFFD; each Unicode Cc character and U+2028/U+2029 becomes one ASCII
space; ASCII spaces are trimmed from both ends (nothing else, no inner collapsing); past NOTE_MAX code points the
first NOTE_MAX - 1 are kept and an ellipsis (U+2026) is appended."""
from __future__ import annotations

NOTE_MAX = 200
_ELLIPSIS = "…"
_REPLACEMENT = "�"


def _clean_char(c: str) -> str:
    o = ord(c)
    if 0xD800 <= o <= 0xDFFF:
        return _REPLACEMENT
    if o < 0x20 or 0x7F <= o <= 0x9F or o in (0x2028, 0x2029):
        return " "
    return c


def clean_note(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError(f"note must be a str, not {type(text).__name__}")
    out = "".join(map(_clean_char, text)).strip(" ")
    if len(out) > NOTE_MAX:
        out = out[:NOTE_MAX - 1] + _ELLIPSIS
    return out
