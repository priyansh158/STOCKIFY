"""Run folder writer (A1 §6.3, A4 §9).

CSV: fixed column order, `\\n` line ends, UTF-8, integers / enums / plain
strings only. Floats appear only in `metrics.json`, written by
`stockify.numerics.dumps_canonical` (sorted keys, no spaces, `repr` floats).
`determinism.json` holds the SHA-256 of every deterministic file;
`manifest.json` (wall clock, host) is outside that set.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
from pathlib import Path

from stockify import numerics

ORDERS = ("order_id,parent_id,instrument_id,side,product,type,validity,qty,price_paise,trigger_paise,protection_bp,"
          "tag,t_submit_ns,t_activate_ns,status,filled_qty,avg_price_paise,reason")
ORDER_EVENTS = "t_ns,order_id,event,detail"
FILLS = "fill_id,order_id,t_ns,instrument_id,side,qty,price_paise,evidence"
CHARGES = ("row_no,kind,order_id,instrument_id,trade_date,turnover_paise,brokerage,stt,exchange,sebi,stamp,gst,dp,"
           "other,total")
# The positions_eod.csv columns, also written by the C++ engine (artifacts.cpp kPositions; checked by blockwatch).
# <block name="positions-eod-columns" same-as="engine/src/artifacts.cpp:positions-eod-columns" same-as-mode="sequence" same-as-pattern='(?P<value>[a-z][a-z_]*)'>
POSITIONS = ("trade_date,instrument_id,product,qty,avg_price_paise,mark_paise,mark_source,unrealised_paise,"
             "realised_paise,underlying_mark_paise")
# </block>
EQUITY = "trade_date,nav_paise,cash_paise,margin_blocked_paise,gross_pnl_paise,charges_paise"
# The margin_daily.csv columns (A1 §6.3), also written by the C++ engine (artifacts.cpp kMargin; checked by blockwatch).
# <block name="margin-daily-columns" same-as="engine/src/artifacts.cpp:margin-daily-columns" same-as-mode="sequence" same-as-pattern='(?P<value>[a-z][a-z_]*)'>
MARGIN = "trade_date,underlying_id,model,step,scan,somc,elm,addons,total"
# </block>
# The ✔ rows of A1 §6.3, in order (checked by blockwatch).
# <block name="deterministic-files" same-as="docs/plan/A1_schemas.md:deterministic-files" same-as-mode="sequence" same-as-pattern='"(?P<value>[a-z_]+\.[a-z]+)"'>
DETERMINISTIC = ("orders.csv", "order_events.csv", "fills.csv", "charges.csv", "positions_eod.csv", "equity.csv",
                 "margin_daily.csv", "metrics.json", "events_log.txt")
# </block>


def _cell(v) -> str:  # noqa: ANN001
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        raise TypeError("floats are not allowed in CSV artifacts (A4 §9.2)")
    s = str(v)
    if any(c in s for c in ",\"\n"):
        s = '"' + s.replace('"', '""') + '"'
    return s


def _csv(path: Path, header: str, rows) -> None:  # noqa: ANN001
    lines = [header] + [",".join(_cell(v) for v in row) for row in rows]
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def write(folder: Path, engine, metrics: dict, manifest: dict) -> dict:  # noqa: ANN001
    folder.mkdir(parents=True, exist_ok=True)
    b = engine.book
    _csv(folder / "orders.csv", ORDERS, [
        (o.order_id, o.parent_id, o.instrument_id, o.side, o.product, o.type, o.validity, o.qty, o.price, o.trigger,
         o.protection_bp, o.tag, o.t_submit, o.t_activate, o.status, o.filled_qty, o.avg_price, o.reason)
        for o in sorted(engine.day_orders().values(), key=lambda o: o.order_id)])
    _csv(folder / "order_events.csv", ORDER_EVENTS, b.order_events)
    _csv(folder / "fills.csv", FILLS, [(f.fill_id, f.order_id, f.t_ns, f.instrument_id, f.side, f.qty, f.price,
                                        f.evidence) for f in b.fills])
    _csv(folder / "charges.csv", CHARGES, [
        (n, c["kind"], c["order_id"], c["instrument_id"], c["trade_date"], c["turnover"], c["brokerage"], c["stt"],
         c["exchange"], c["sebi"], c["stamp"], c["gst"], c["dp"], c["other"], c["total"])
        for n, c in enumerate(b.charges, start=engine.charge_row_base + 1)])   # rows continue across days (VOL 5.2)
    _csv(folder / "positions_eod.csv", POSITIONS, engine.positions_eod)
    _csv(folder / "equity.csv", EQUITY, engine.equity)
    _csv(folder / "margin_daily.csv", MARGIN, getattr(engine, "margin_daily", []))
    (folder / "metrics.json").write_bytes((numerics.dumps_canonical(metrics) + "\n").encode("utf-8"))
    (folder / "events_log.txt").write_bytes(("\n".join(engine.log_lines) + ("\n" if engine.log_lines else "")).encode())
    hashes = {}
    for name in DETERMINISTIC:
        hashes[name] = hashlib.sha256((folder / name).read_bytes()).hexdigest()
    (folder / "determinism.json").write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                                             newline="\n")
    manifest = {**manifest, "python": sys.version.split()[0], "os": platform.platform(),
                "labels": sorted(engine.labels)}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return hashes
