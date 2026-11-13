"""The expiry settlements and physical deliveries of a run, read from its `events_log.txt` (CH 6.10).

Both runners log the same lines (A4 §7, identical on both):
    "<t_ns> INFO settled <instrument> <product> at FSP <price> (<source>)"        a cash settlement
    "<t_ns> INFO delivered <stock> CNC <BUY|SELL> <units> at <price> (<source>)"  a physical delivery
Reads only. A folder with no log, or a log with neither line, gives an empty list. Units: `t_ns` UTC epoch ns,
`price_paise` paise, `qty` units. A cash line does not carry the units or the side: those are None here.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

SETTLED = re.compile(r"^(\d+) INFO settled (\d+) (\w+) at FSP (-?\d+) \((\w+)\)$")
DELIVERED = re.compile(r"^(\d+) INFO delivered (\d+) (\w+) (BUY|SELL) (\d+) at (-?\d+) \((\w+)\)$")


class Settlement(NamedTuple):
    t_ns: int
    instrument_id: int
    product: str
    price_paise: int
    kind: str                    # "cash" or "delivery"
    side: str | None             # delivery only
    qty: int | None              # delivery only
    source: str                  # "settle" (NSE's price) or "fallback" (estimated, A4 §7.2)


def read_settlements(folder: Path | str) -> list[Settlement]:
    """Every settlement and delivery of the run folder, in the order the log wrote them."""
    log = Path(folder) / "events_log.txt"
    if not log.is_file():
        return []
    out: list[Settlement] = []
    with log.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if m := SETTLED.match(line):
                out.append(Settlement(int(m[1]), int(m[2]), m[3], int(m[4]), "cash", None, None, m[5]))
            elif m := DELIVERED.match(line):
                out.append(Settlement(int(m[1]), int(m[2]), m[3], int(m[6]), "delivery", m[4], int(m[5]), m[7]))
    return out
