"""Hand-built Kite binary packets for tests (layouts in kite_packets.py, K-2/K-3)."""
from __future__ import annotations

import struct

NIFTY_INDEX = 256265          # & 0xFF == 9: indices segment
NIFTY_CE = 12_345_602         # an NFO token


def u32(*values: int) -> bytes:
    return b"".join(struct.pack(">I", v) for v in values)


def ltp(token: int, price: int) -> bytes:
    return u32(token, price)


def index_quote(token: int, ltp_: int, high: int, low: int, open_: int, close: int, change: int) -> bytes:
    return u32(token, ltp_, high, low, open_, close) + struct.pack(">i", change)


def index_full(token: int, ltp_: int, high: int, low: int, open_: int, close: int, change: int,
               exch_ts: int) -> bytes:
    return index_quote(token, ltp_, high, low, open_, close, change) + u32(exch_ts)


def quote(token: int, ltp_: int, qty: int, atp: int, vol: int, buy: int, sell: int,
          open_: int, high: int, low: int, close: int) -> bytes:
    return u32(token, ltp_, qty, atp, vol, buy, sell, open_, high, low, close)


def full(token: int, ltp_: int, *, vol: int = 1000, ltt: int = 1_762_316_100, oi: int = 50_000,
         exch_ts: int = 1_762_316_101, depth: list[tuple[int, int, int]] | None = None) -> bytes:
    head = quote(token, ltp_, 75, ltp_ - 5, vol, 10_000, 12_000, ltp_ - 100, ltp_ + 50, ltp_ - 150, ltp_ - 80)
    head += u32(ltt, oi, oi + 500, oi - 500, exch_ts)
    if depth is None:
        depth = [(75 * (i + 1), ltp_ - 5 * (i + 1), i + 1) for i in range(5)] + \
                [(65 * (i + 1), ltp_ + 5 * (i + 1), i + 2) for i in range(5)]
    body = b"".join(u32(q, p) + struct.pack(">H", o) + b"\x00\x00" for q, p, o in depth)
    return head + body
