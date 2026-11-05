"""Kite WebSocket binary frames (K-2, K-3; A1_schemas.md §3.1).

A binary frame is `u16 count` followed by `count` × (`u16 length`, packet),
big-endian. A 1-byte frame is a heartbeat. Packet layouts by length:

* 8    LTP mode: token, ltp
* 28   index, quote mode: token, ltp, high, low, open, close, change
* 32   index, full mode: the 28 bytes + exchange timestamp (epoch s)
* 44   quote mode: token, ltp, last qty, avg price, volume, buy qty,
       sell qty, open, high, low, close
* 184  full mode: the 44 bytes + last trade ts, oi, oi day high, oi day low,
       exchange ts, then 10 depth entries of 12 bytes (qty, price, u16 orders,
       2 pad), bids 1–5 then asks 1–5

Prices stay as sent: integer paise (index values × 100). The recorder stores
the raw packet bytes too, so a parser bug can be fixed by re-parsing (D-08).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

PARSER_VERSION = "1"

MODE_LTP, MODE_QUOTE, MODE_FULL = 1, 2, 3
LENGTHS = {8: MODE_LTP, 28: MODE_QUOTE, 32: MODE_FULL, 44: MODE_QUOTE, 184: MODE_FULL}
INDEX_LENGTHS = (28, 32)
INDICES_SEGMENT = 9                     # instrument_token & 0xff for indices

_U16 = struct.Struct(">H")
_I32 = struct.Struct(">I")


class PacketError(ValueError):
    """A frame or packet does not match the documented layout."""


@dataclass(frozen=True)
class Frame:
    heartbeat: bool
    packets: tuple[bytes, ...]


def split_frame(payload: bytes) -> Frame:
    """Split a binary frame into raw packets. Strict: lengths must add up."""
    if len(payload) == 1:
        return Frame(heartbeat=True, packets=())
    if len(payload) < 2:
        raise PacketError(f"frame too short ({len(payload)} bytes)")
    (count,) = _U16.unpack_from(payload, 0)
    pos, packets = 2, []
    for _ in range(count):
        if pos + 2 > len(payload):
            raise PacketError("frame ends inside a packet length")
        (length,) = _U16.unpack_from(payload, pos)
        pos += 2
        if pos + length > len(payload):
            raise PacketError(f"packet of {length} bytes runs past the frame end")
        packets.append(bytes(payload[pos:pos + length]))
        pos += length
    if pos != len(payload):
        raise PacketError(f"{len(payload) - pos} trailing bytes after {count} packets")
    return Frame(heartbeat=False, packets=tuple(packets))


def build_frame(packets: list[bytes] | tuple[bytes, ...]) -> bytes:
    """Inverse of split_frame (used by the replay ticker and tests)."""
    out = bytearray(_U16.pack(len(packets)))
    for p in packets:
        out += _U16.pack(len(p))
        out += p
    return bytes(out)


def _u(packet: bytes, offset: int) -> int:
    return _I32.unpack_from(packet, offset)[0]


def _s(packet: bytes, offset: int) -> int:
    return struct.unpack_from(">i", packet, offset)[0]


def parse_packet(packet: bytes) -> dict:
    """Core fields of one packet, as the landing table stores them."""
    n = len(packet)
    if n not in LENGTHS:
        raise PacketError(f"unknown packet length {n}")
    token = _u(packet, 0)
    row = {"instrument_token": token, "mode": LENGTHS[n], "pkt_len": n,
           "ltp": _u(packet, 4), "last_qty": None, "atp": None, "volume": None,
           "buy_qty": None, "sell_qty": None, "open": None, "high": None, "low": None,
           "close": None, "last_trade_ts_s": None, "oi": None, "oi_high": None,
           "oi_low": None, "exch_ts_s": None}
    if n in INDEX_LENGTHS:
        row.update(high=_u(packet, 8), low=_u(packet, 12), open=_u(packet, 16), close=_u(packet, 20))
        if n == 32:
            row["exch_ts_s"] = _u(packet, 28)
    elif n in (44, 184):
        row.update(last_qty=_u(packet, 8), atp=_u(packet, 12), volume=_u(packet, 16),
                   buy_qty=_u(packet, 20), sell_qty=_u(packet, 24), open=_u(packet, 28),
                   high=_u(packet, 32), low=_u(packet, 36), close=_u(packet, 40))
        if n == 184:
            row.update(last_trade_ts_s=_u(packet, 44), oi=_u(packet, 48), oi_high=_u(packet, 52),
                       oi_low=_u(packet, 56), exch_ts_s=_u(packet, 60))
    return row


def index_change(packet: bytes) -> int | None:
    """The signed 'change' field of an index packet (bytes 24–28)."""
    return _s(packet, 24) if len(packet) in INDEX_LENGTHS else None


def parse_depth(packet: bytes) -> list[tuple[int, int, int]]:
    """Ten (qty, price_paise, orders) levels of a 184-byte packet: bids 1–5, asks 1–5."""
    if len(packet) != 184:
        raise PacketError("depth exists only in 184-byte full packets")
    out = []
    for p in range(64, 184, 12):
        out.append((_u(packet, p), _u(packet, p + 4), _U16.unpack_from(packet, p + 8)[0]))
    return out


def is_index_token(token: int) -> bool:
    return (token & 0xFF) == INDICES_SEGMENT
