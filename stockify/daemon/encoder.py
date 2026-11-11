"""The pack encoder for live frames (DM 5.2; M5.1 fan-out).

The recorder's tap hands every stamped frame to the paper daemon's dispatcher. This turns a data frame's
payload into the same event-pack records `packs.build` writes for a recorded day, so a paper session sees
byte-identical records live and in replay. Pure: no clock, no I/O, no threads; the caller owns the stamp.

A record built here carries `seq` 0: the session journal numbers what it writes. LTP-mode packets (8 bytes)
have no pack record kind and are skipped and counted, as is any token outside the day's map.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from stockify.daemon import kite_packets
from stockify.data import packs

log = logging.getLogger("stockify.daemon.encoder")


@dataclass
class EncoderStats:
    frames: int = 0
    records: int = 0
    ltp_skipped: int = 0
    unknown_tokens: int = 0
    bad_frames: int = 0
    bad_packets: int = 0


class FrameEncoder:
    def __init__(self, token_map: dict[int, int]):
        self.token_map = dict(token_map)
        self.stats = EncoderStats()

    def encode(self, payload: bytes, recv_ns: int) -> list[tuple[int, bytes]]:
        """`(instrument_id, record)` for each data packet of a binary frame, in packet order.

        `recv_ns` (required) is the frame's receive time: the record's `t_event`, as in a recorded pack. Heartbeats
        and frames that do not split give `[]`. A payload that is not bytes (a text frame's `str`) is a caller
        error: the dispatcher filters on the tap item's `is_binary` first."""
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError(f"payload must be bytes, got {type(payload).__name__} (filter text frames on is_binary)")
        payload = bytes(payload)
        try:
            frame = kite_packets.split_frame(payload)
        except kite_packets.PacketError as e:
            self.stats.bad_frames += 1
            log.warning("bad frame (%d bytes): %s", len(payload), e)
            return []
        if frame.heartbeat:
            return []
        self.stats.frames += 1
        out: list[tuple[int, bytes]] = []
        for raw in frame.packets:
            if len(raw) == 8:
                self.stats.ltp_skipped += 1
                continue
            iid = self.token_map.get(int.from_bytes(raw[:4], "big"))
            if iid is None:
                self.stats.unknown_tokens += 1
                continue
            try:
                _, record = packs.enc_tick({"raw": raw}, iid, recv_ns)
            except (kite_packets.PacketError, packs.PackError) as e:
                self.stats.bad_packets += 1
                log.warning("bad packet of %d bytes: %s", len(raw), e)
                continue
            out.append((iid, record))
            self.stats.records += 1
        return out
