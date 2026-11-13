"""M4 (MEZ 4.6): a streaming reader for NSE's CME SPAN 4.00 risk parameter files (nsccl.YYYYMMDD.iN.spn / .s).

The input is a downloaded file and therefore untrusted data. It is parsed with the stdlib only
(`xml.etree.ElementTree.iterparse`: expat, which resolves no external entities and fetches nothing), nothing is
evaluated, a zip member is size-checked before it is read, and each portfolio / commodity element is cleared as soon as
it is processed so the ~54 MB file never exists as one tree. Every number is returned as the decimal string found in
the file; `stockify.numerics` converts them exactly later (Task 3). Only strikes become integers (paise), because the
contract key needs one.

Layout of the file (verified against the 2026-11-09 i1 file) and what is used here:

    spanFile
      fileFormat            "4.00"; anything else is refused
      created               YYYYMMDDHHMM the file was produced (SpanFile.created)
      pointInTime
        date, isSetl        business date; 0 = intraday (iN) file, 1 = final settlement (.s) file
        clearingOrg
          pointDef
            scanPointDef*   the 16 scan scenarios: point, priceScanDef / volScanDef (mult, numerator, denominator),
                            weight, pairedPoint  (SpanFile.scan_points; the order of a risk array's 16 values)
          exchange
            phyPf*          underlying: phy/p is the spot or index level (CombinedCommodity.spot)
            futPf*          futures: fut* each with pe (expiry YYYYMMDD), p, d, v, t (years to expiry), setlDate,
                            intrRate/val, scanRate (priceScan, volScan: the scan ranges, in rupees and vol points)
                            and ra
            oopPf*          options on the futures/underlying: series* each with pe (expiry), v (series vol),
                            t, setlDate, intrRate/val, scanRate and opt*; opt has cId, o (C|P), k (strike, rupees),
                            p (settlement price), d (delta), v (vol) and ra
              (every portfolio also has pfId, pfCode, cvf = contract value factor)
              ra            the risk array of one contract: r (rate class; only r = 1 is kept), 16 x a (the loss of
                            the contract, per unit, in each scan scenario, in file order), d (composite delta)
          ccDef*            one combined commodity (cc), written AFTER its portfolios:
            somMeth         short option minimum method (CombinedCommodity.som_method)
            pfLink*         pfId + pfType (PHY|FUT|OOP): links the portfolios above to the commodity
            scanTiers, intraTiers, interTiers, somTiers
                            tier* with tn, sPe / ePe (expiry range, YYYYMMDD) and, for som, rate* (r, val)
            dSpread*        calendar spread charge: spread, chargeMeth, rate* (r, val: the per-spread charge),
                            pLeg* (pe expiry, rs side A|B, i weight)
          interSpreads      inter-commodity spreads; not read (none involves an index underlying)

Because portfolios precede the ccDef that names them, portfolios are filtered by pfCode against the wanted
underlyings and buffered by pfId until their ccDef arrives (pfCode equals the cc code for every NSE commodity).
A contract is keyed (kind, expiry "YYYY-MM-DD", strike in paise or None): kind is FUT, CE or PE.
"""
from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterator

from stockify import numerics

ContractKey = tuple[str, str, int | None]
SeriesKey = tuple[str, str]  # (portfolio type "FUT" | "OOP", expiry "YYYY-MM-DD")

SCENARIOS = 16
MAX_MEMBER_BYTES = 1 << 30  # a zip member larger than this once unpacked is refused (the real file is ~54 MB)
_SUPPORTED_FORMAT = "4.00"


class SpanFileError(ValueError):
    """The file is not a SPAN 4.00 file this reader understands."""


@dataclass(frozen=True)
class Contract:
    price: str
    delta: str
    vol: str | None
    risk_array: tuple[str, ...]
    composite_delta: str


@dataclass(frozen=True)
class SeriesInfo:
    """Per expiry of a futures or options portfolio."""
    time_to_expiry: str | None  # t, years
    vol: str | None  # v of an option series; None for futures (each future carries its own v)
    interest_rate: str | None  # intrRate/val
    settle_date: str | None  # setlDate, YYYY-MM-DD


@dataclass(frozen=True)
class Tier:
    number: int
    start: str | None  # YYYY-MM-DD (sPe)
    end: str | None  # YYYY-MM-DD (ePe)
    rates: dict[str, str]  # rate class -> val (som tiers)


@dataclass(frozen=True)
class SpreadLeg:
    expiry: str
    side: str  # rs: A or B
    weight: str  # i


@dataclass(frozen=True)
class CalendarSpread:
    number: int
    charge_method: str
    rates: dict[str, str]  # rate class -> charge (decimal string)
    legs: tuple[SpreadLeg, ...]


@dataclass(frozen=True)
class ScanPoint:
    point: int
    price_mult: str
    price_numerator: str
    price_denominator: str
    vol_mult: str
    vol_numerator: str
    vol_denominator: str
    weight: str
    paired_point: int | None


@dataclass
class CombinedCommodity:
    code: str
    contracts: dict[ContractKey, Contract] = field(default_factory=dict)
    price_scan: dict[SeriesKey, str] = field(default_factory=dict)
    vol_scan: dict[SeriesKey, str] = field(default_factory=dict)
    series: dict[SeriesKey, SeriesInfo] = field(default_factory=dict)
    spot: str | None = None
    cvf: dict[str, str] = field(default_factory=dict)  # portfolio type -> contract value factor
    som_method: str = ""
    scan_tiers: tuple[Tier, ...] = ()
    intra_tiers: tuple[Tier, ...] = ()
    inter_tiers: tuple[Tier, ...] = ()
    som_tiers: tuple[Tier, ...] = ()
    calendar_spreads: tuple[CalendarSpread, ...] = ()


@dataclass
class SpanFile:
    created: str  # YYYYMMDDHHMM
    business_date: str = ""  # YYYY-MM-DD
    is_settlement: bool = False
    commodities: dict[str, CombinedCommodity] = field(default_factory=dict)
    scan_points: dict[int, ScanPoint] = field(default_factory=dict)


def _date(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if len(text) != 8 or not text.isdigit() or text == "00000000":
        raise SpanFileError(f"bad SPAN date {text!r}")
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def _text(el: ET.Element, path: str) -> str | None:
    value = el.findtext(path)
    return None if value is None else value.strip()


def _need(el: ET.Element, path: str) -> str:
    value = _text(el, path)
    if not value:
        raise SpanFileError(f"<{el.tag}> lacks <{path}>")
    return value


def _contract(el: ET.Element) -> Contract:
    ras = [ra for ra in el.findall("ra") if _text(ra, "r") == "1"]
    if len(ras) != 1:
        raise SpanFileError(f"<{el.tag}> needs exactly one risk array of rate class 1, found {len(ras)}")
    ra = ras[0]
    values = tuple((a.text or "").strip() for a in ra.findall("a"))
    if len(values) != SCENARIOS or not all(values):
        raise SpanFileError(f"a risk array has {len(values)} values, expected {SCENARIOS}")
    return Contract(price=_need(el, "p"), delta=_need(el, "d"), vol=_text(el, "v"),
                    risk_array=values, composite_delta=_need(ra, "d"))


def _series_info(el: ET.Element, with_vol: bool) -> SeriesInfo:
    return SeriesInfo(time_to_expiry=_text(el, "t"), vol=_text(el, "v") if with_vol else None,
                      interest_rate=_text(el, "intrRate/val"), settle_date=_date(_text(el, "setlDate")))


@dataclass
class _Portfolio:
    pf_type: str
    code: str
    contracts: dict[ContractKey, Contract] = field(default_factory=dict)
    price_scan: dict[SeriesKey, str] = field(default_factory=dict)
    vol_scan: dict[SeriesKey, str] = field(default_factory=dict)
    series: dict[SeriesKey, SeriesInfo] = field(default_factory=dict)
    spot: str | None = None
    cvf: str | None = None


def _read_portfolio(el: ET.Element) -> _Portfolio:
    code = _need(el, "pfCode")
    pf_type = {"phyPf": "PHY", "futPf": "FUT", "oopPf": "OOP"}[el.tag]
    pf = _Portfolio(pf_type, code, cvf=_text(el, "cvf"))

    def add(key: ContractKey, contract: Contract) -> None:
        if key in pf.contracts:
            raise SpanFileError(f"{code}: duplicate contract {key}")
        pf.contracts[key] = contract

    def scan(owner: ET.Element, skey: SeriesKey) -> None:
        sr = owner.find("scanRate")
        if sr is not None:
            pf.price_scan[skey] = _need(sr, "priceScan")
            pf.vol_scan[skey] = _need(sr, "volScan")

    if pf_type == "PHY":
        pf.spot = _text(el, "phy/p")
    elif pf_type == "FUT":
        for fut in el.findall("fut"):
            expiry = _date(_need(fut, "pe"))
            skey = ("FUT", expiry)
            add(("FUT", expiry, None), _contract(fut))
            pf.series[skey] = _series_info(fut, with_vol=False)
            scan(fut, skey)
    else:
        for series in el.findall("series"):
            expiry = _date(_need(series, "pe"))
            skey = ("OOP", expiry)
            pf.series[skey] = _series_info(series, with_vol=True)
            scan(series, skey)
            for opt in series.findall("opt"):
                kind = {"C": "CE", "P": "PE"}.get(_need(opt, "o"))
                if kind is None:
                    raise SpanFileError(f"{code}: option side {_text(opt, 'o')!r} is neither C nor P")
                add((kind, expiry, numerics.to_paise(_need(opt, "k"))), _contract(opt))
    return pf


def _tiers(el: ET.Element | None) -> tuple[Tier, ...]:
    if el is None:
        return ()
    out = []
    for tier in el.findall("tier"):
        rates = {_need(r, "r"): _need(r, "val") for r in tier.findall("rate")}
        out.append(Tier(int(_need(tier, "tn")), _date(_text(tier, "sPe")), _date(_text(tier, "ePe")), rates))
    return tuple(out)


def _spreads(cc: ET.Element) -> tuple[CalendarSpread, ...]:
    out = []
    for sp in cc.findall("dSpread"):
        rates = {_need(r, "r"): _need(r, "val") for r in sp.findall("rate")}
        legs = tuple(SpreadLeg(_date(_need(leg, "pe")), _need(leg, "rs"), _need(leg, "i"))
                     for leg in sp.findall("pLeg"))
        out.append(CalendarSpread(int(_need(sp, "spread")), _need(sp, "chargeMeth"), rates, legs))
    return tuple(out)


def _scan_point(el: ET.Element) -> ScanPoint:
    paired = _text(el, "pairedPoint")
    return ScanPoint(int(_need(el, "point")),
                     _need(el, "priceScanDef/mult"), _need(el, "priceScanDef/numerator"),
                     _need(el, "priceScanDef/denominator"),
                     _need(el, "volScanDef/mult"), _need(el, "volScanDef/numerator"),
                     _need(el, "volScanDef/denominator"), _need(el, "weight"),
                     int(paired) if paired else None)


def _commodity(cc: ET.Element, portfolios: dict[str, _Portfolio]) -> CombinedCommodity:
    code = _need(cc, "cc")
    out = CombinedCommodity(code=code, som_method=_text(cc, "somMeth") or "",
                            scan_tiers=_tiers(cc.find("scanTiers")), intra_tiers=_tiers(cc.find("intraTiers")),
                            inter_tiers=_tiers(cc.find("interTiers")), som_tiers=_tiers(cc.find("somTiers")),
                            calendar_spreads=_spreads(cc))
    for link in cc.findall("pfLink"):
        pf = portfolios.get(_need(link, "pfId"))
        if pf is None:
            continue
        if pf.pf_type == "PHY":
            out.spot = pf.spot
        if pf.cvf is not None:
            out.cvf[pf.pf_type] = pf.cvf
        for key in pf.contracts:
            if key in out.contracts:
                raise SpanFileError(f"{code}: duplicate contract {key}")
        out.contracts.update(pf.contracts)
        out.price_scan.update(pf.price_scan)
        out.vol_scan.update(pf.vol_scan)
        out.series.update(pf.series)
    return out


class _ZipMember(io.RawIOBase):
    """A zip member's stream that closes the ZipFile (and its file handle) with it."""

    def __init__(self, zf: zipfile.ZipFile, member: IO[bytes]) -> None:
        super().__init__()
        self._zf, self._member = zf, member

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        return self._member.readinto(b)

    def close(self) -> None:
        try:
            self._member.close()
            self._zf.close()
        finally:
            super().close()


def _open(path: Path) -> IO[bytes]:
    if path.suffix.lower() != ".zip":
        return open(path, "rb")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise SpanFileError(f"{path.name}: not a zip file") from exc
    members = [i for i in zf.infolist() if i.filename.lower().endswith(".spn")]
    if len(members) != 1:
        zf.close()
        raise SpanFileError(f"{path.name}: expected one .spn member, found {len(members)}")
    if members[0].file_size > MAX_MEMBER_BYTES:
        zf.close()
        raise SpanFileError(f"{path.name}: member {members[0].filename} is {members[0].file_size} bytes unpacked")
    return _ZipMember(zf, zf.open(members[0]))


def _events(stream: IO[bytes]) -> Iterator[tuple[str, ET.Element, list[ET.Element]]]:
    stack: list[ET.Element] = []
    for event, el in ET.iterparse(stream, events=("start", "end")):
        if event == "start":
            stack.append(el)
        else:
            stack.pop()
            yield el.tag, el, stack


def parse_span(path: str | Path, underlyings: set[str] | None = None) -> SpanFile:
    """Read a SPAN 4.00 file (plain .spn or a zip holding one). With `underlyings`, keep only those combined
    commodities (the rest are skipped while streaming); otherwise keep them all."""
    path = Path(path)
    out = SpanFile(created="")
    portfolios: dict[str, _Portfolio] = {}
    seen_format = False
    stream = _open(path)
    try:
        for tag, el, stack in _events(stream):
            depth = len(stack)  # depth of el's parent: spanFile = 1
            if depth == 1 and tag == "fileFormat":
                if (el.text or "").strip() != _SUPPORTED_FORMAT:
                    raise SpanFileError(f"{path.name}: SPAN file format {el.text!r}, only {_SUPPORTED_FORMAT} is read")
                seen_format = True
            elif depth == 1 and tag == "created":
                out.created = (el.text or "").strip()
            elif depth == 2 and tag == "date":
                out.business_date = _date(el.text) or ""
            elif depth == 2 and tag == "isSetl":
                out.is_settlement = (el.text or "").strip() == "1"
            elif depth == 4 and tag == "scanPointDef":
                point = _scan_point(el)
                out.scan_points[point.point] = point
            elif depth == 4 and tag in ("phyPf", "futPf", "oopPf"):
                if underlyings is None or _text(el, "pfCode") in underlyings:
                    portfolios[_need(el, "pfId")] = _read_portfolio(el)
                stack[-1].remove(el)
                el.clear()
            elif depth == 3 and tag == "ccDef":
                if underlyings is None or _text(el, "cc") in underlyings:
                    cc = _commodity(el, portfolios)
                    out.commodities[cc.code] = cc
                stack[-1].remove(el)
                el.clear()
    except ET.ParseError as exc:
        raise SpanFileError(f"{path.name}: not well-formed XML: {exc}") from exc
    finally:
        stream.close()
    if not seen_format:
        raise SpanFileError(f"{path.name}: no <fileFormat>; not a SPAN file")
    return out
