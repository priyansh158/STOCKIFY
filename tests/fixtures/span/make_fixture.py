r"""One-off: cut tests/fixtures/span/nsccl.20261109.i1.trim.spn from NSE's real SPAN file of 2026-11-09 (issue 1).

    .\.venv\Scripts\python.exe -I tests\fixtures\span\make_fixture.py [SOURCE.zip|SOURCE.spn] [OUT.spn]

The default source is the archived nsccl.20261109.i1.zip in the lab (read only). The cut keeps the real XML
structure and the real values, and drops everything a margin test of NIFTY and BANKNIFTY does not need:

* every combined commodity except NIFTY and BANKNIFTY (their phyPf / futPf / oopPf / ccDef);
* futures other than the 23-Nov-2026 and 29-Dec-2026 months;
* option series other than 17-Nov-2026 (NIFTY only) and 23-Nov-2026, and in each series every contract but the
  10 strikes nearest the money (both calls and puts, so 20 contracts);
* calendar spreads (dSpread) that use any other expiry; interSpreads; currency, account-type and pb-rate
  definitions; the scan and delta point definitions are kept whole (16 + 7 small elements).

The file is parsed streaming (iterparse, elements cleared) because the source is ~54 MB. Not run by the tests.
"""
from __future__ import annotations

import io
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

KEEP = ("NIFTY", "BANKNIFTY")
FUT_EXPIRIES = {"20261123", "20261229"}
SERIES_EXPIRIES = {"20261117", "20261123"}
SPREAD_EXPIRIES = {"20261117", "20261123", "20261229"}
STRIKES_EACH_SIDE = 10  # nearest strikes kept per series: this many, CE and PE each
DEFAULT_SOURCE = Path.home() / "AppData/Local/STOCKIFY/kite/lab/nse/margin/span/2026/nsccl.20261109.i1.zip"
DEFAULT_OUT = Path(__file__).with_name("nsccl.20261109.i1.trim.spn")


def _open(path: Path):
    if path.suffix.lower() == ".zip":
        zf = zipfile.ZipFile(path)
        name = next(n for n in zf.namelist() if n.lower().endswith(".spn"))
        return io.BufferedReader(zf.open(name))
    return open(path, "rb")


def _trim_portfolio(pf: ET.Element, spot: float) -> None:
    for fut in pf.findall("fut"):
        if fut.findtext("pe") not in FUT_EXPIRIES:
            pf.remove(fut)
    for series in pf.findall("series"):
        if series.findtext("pe") not in SERIES_EXPIRIES:
            pf.remove(series)
            continue
        strikes = sorted({float(o.findtext("k")) for o in series.findall("opt")}, key=lambda k: (abs(k - spot), k))
        keep = set(strikes[:STRIKES_EACH_SIDE])
        for opt in series.findall("opt"):
            if float(opt.findtext("k")) not in keep:
                series.remove(opt)


def cut(source: Path, out: Path) -> None:
    root = ET.Element("spanFile")
    pit = ET.Element("pointInTime")
    clearing = ET.SubElement(pit, "clearingOrg")
    ET.SubElement(clearing, "ec").text = "NSCCL"
    exchange = ET.Element("exchange")
    ET.SubElement(exchange, "exch").text = "NSE"
    spot: dict[str, float] = {}
    depth = 0
    for event, el in ET.iterparse(_open(source), events=("start", "end")):
        if event == "start":
            depth += 1
            continue
        depth -= 1  # now the depth of el's parent (spanFile = 1)
        tag = el.tag
        if depth == 1 and tag in ("fileFormat", "created"):
            ET.SubElement(root, tag).text = el.text
        elif depth == 2 and tag in ("date", "isSetl", "setlQualifier"):
            pit.insert(len(pit) - 1, el)
        elif depth == 3 and tag == "pointDef":
            clearing.append(el)
        elif depth == 4 and tag in ("phyPf", "futPf", "oopPf"):
            if el.findtext("pfCode") in KEEP:
                if tag == "phyPf":
                    spot[el.findtext("pfCode")] = float(el.findtext("phy/p"))
                else:
                    _trim_portfolio(el, spot[el.findtext("pfCode")])
                exchange.append(el)
            else:
                el.clear()
        elif depth == 3 and tag == "ccDef":
            if el.findtext("cc") in KEEP:
                for sp in el.findall("dSpread"):
                    if {leg.findtext("pe") for leg in sp.findall("pLeg")} - SPREAD_EXPIRIES:
                        el.remove(sp)
                if exchange is not None:
                    clearing.append(exchange)  # the portfolios precede the ccDefs in the file
                    exchange = None
                clearing.append(el)
            else:
                el.clear()
    root.append(pit)
    ET.indent(root)
    out.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    cut(src, dst)
    print(f"wrote {dst} ({dst.stat().st_size} bytes)")
