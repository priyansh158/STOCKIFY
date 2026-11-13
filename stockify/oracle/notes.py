"""CH 6.1: the plain-English note a strategy attaches to an order. Twin of engine/src/notes.cpp.

Rules, in order: a lone surrogate becomes U+FFFD; each Unicode bidi control character (U+202A-202E, U+2066-2069,
U+200E, U+200F, U+061C) is removed outright (CH 6.25, D-127); each Unicode Cc character and U+2028/U+2029 becomes one ASCII
space; ASCII spaces are trimmed from both ends (nothing else, no inner collapsing); past NOTE_MAX code points the
first NOTE_MAX - 1 are kept and an ellipsis (U+2026) is appended."""
from __future__ import annotations

NOTE_MAX = 200
_ELLIPSIS = "…"
_REPLACEMENT = "�"
_BIDI = frozenset(map(chr, [*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C]))


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
    out = "".join(map(_clean_char, (c for c in text if c not in _BIDI))).strip(" ")
    if len(out) > NOTE_MAX:
        out = out[:NOTE_MAX - 1] + _ELLIPSIS
    return out
