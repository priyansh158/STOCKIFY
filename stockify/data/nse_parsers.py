"""Parsers for NSE end-of-day files (M1.3, S-1…S-6).

Every parser takes the raw downloaded bytes (zip or CSV) and returns plain
dicts in one normalised shape per dataset, so a legacy file and a UDiFF file
for the same day give identical rows (U-03).

Columns are mapped **by header name**. A missing column, or a column the
parser does not know, raises `ParseError`: a format change must be seen, not
silently half-read (S-6 note). Decimal strings become integers exactly
through `stockify.numerics`; nothing goes through float.

Units, as published (checked on 2024-07-05, where both formats exist):

* F&O `volume` is **contracts** in both formats; `oi` and `oi_change` are
  units (shares); turnover is rupees in UDiFF and lakhs in legacy, stored
  as paise either way. Legacy F&O has no lot size (`lot_size` = None).
* CM `volume` is shares.
* Index values are points × 100. `ind_close_all` gives India VIX open, high
  and low with four decimals; those are rounded half-up to two decimals
  (the raw file keeps the exact value).
"""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from stockify import numerics


class ParseError(ValueError):
    pass


FO_KEYS = ("trade_date", "underlying", "kind", "index_derivative", "expiry", "strike_paise",
           "open_paise", "high_paise", "low_paise", "close_paise", "last_paise", "prev_close_paise",
           "settle_paise", "underlying_paise", "oi", "oi_change", "volume", "turnover_paise", "lot_size")
CM_KEYS = ("trade_date", "symbol", "series", "isin", "open_paise", "high_paise", "low_paise",
           "close_paise", "last_paise", "prev_close_paise", "volume", "turnover_paise", "trades")
IDX_KEYS = ("trade_date", "name", "open_paise", "high_paise", "low_paise", "close_paise")


# ── Reading ──────────────────────────────────────────────────────────────────

def _text(raw: bytes) -> str:
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            if len(names) != 1:
                raise ParseError(f"expected one file in the zip, found {names}")
            raw = z.read(names[0])
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ParseError(f"not UTF-8: {e}") from e


def _table(raw: bytes, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> list[dict]:
    rows = list(csv.reader(io.StringIO(_text(raw))))
    if not rows:
        raise ParseError("empty file")
    header = [h.strip() for h in rows[0]]
    # Legacy files end every line with a comma: one empty trailing column.
    while header and header[-1] == "":
        header.pop()
    known = set(required) | set(optional)
    unknown = [h for h in header if h not in known]
    missing = [h for h in required if h not in header]
    if unknown or missing:
        raise ParseError(f"header mismatch: unknown {unknown}, missing {missing}")
    if len(set(header)) != len(header):
        raise ParseError(f"duplicate columns in {header}")
    out = []
    for n, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        cells = [c.strip() for c in row]
        extra = cells[len(header):]
        if any(extra):
            raise ParseError(f"line {n}: more values than columns")
        out.append(dict(zip(header, cells + [""] * (len(header) - len(cells)))))
    return out


# ── Field conversions ────────────────────────────────────────────────────────

def _paise(text: str, where: str) -> int | None:
    if text in ("", "-"):
        return None
    try:
        return numerics.to_paise(text)
    except numerics.NumericError as e:
        raise ParseError(f"{where}: {e}") from e


def _paise_from_lakh(text: str, where: str) -> int | None:
    if text in ("", "-"):
        return None
    try:
        return numerics.to_paise(str(Decimal(text) * 100_000))
    except (InvalidOperation, numerics.NumericError) as e:
        raise ParseError(f"{where}: {text!r}: {e}") from e


def _points_x100(text: str, where: str) -> int | None:
    if text in ("", "-"):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as e:
        raise ParseError(f"{where}: {text!r} is not a number") from e
    return numerics.check_int64(int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def _int(text: str, where: str) -> int | None:
    if text in ("", "-"):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as e:
        raise ParseError(f"{where}: {text!r} is not a number") from e
    if value != value.to_integral_value():
        raise ParseError(f"{where}: {text!r} is not a whole number")
    return numerics.check_int64(int(value))


def _date(text: str, fmt: str, where: str) -> str:
    try:
        return datetime.strptime(text, fmt).date().isoformat()
    except ValueError as e:
        raise ParseError(f"{where}: {text!r} is not a {fmt} date") from e


def _check_day(found: str, day: date, where: str) -> None:
    if found != day.isoformat():
        raise ParseError(f"{where}: file is for {found}, expected {day}")


# ── F&O ──────────────────────────────────────────────────────────────────────

UDIFF_COLUMNS = ("TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN", "TckrSymb",
                 "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric", "OptnTp", "FinInstrmNm",
                 "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric", "PrvsClsgPric", "UndrlygPric",
                 "SttlmPric", "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal",
                 "TtlNbOfTxsExctd", "SsnId", "NewBrdLotQty", "Rmks", "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4")
UDIFF_FO_TYPES = {"IDF": ("FUT", True), "STF": ("FUT", False), "IDO": (None, True), "STO": (None, False)}


def fo_udiff(raw: bytes, day: date) -> list[dict]:
    out = []
    for n, r in enumerate(_table(raw, UDIFF_COLUMNS), start=2):
        w = f"fo_udiff line {n}"
        _check_day(r["TradDt"], day, w)
        if r["Sgmt"] != "FO":
            raise ParseError(f"{w}: segment {r['Sgmt']!r}")
        if r["FinInstrmTp"] not in UDIFF_FO_TYPES:
            raise ParseError(f"{w}: unknown instrument type {r['FinInstrmTp']!r}")
        kind, is_index = UDIFF_FO_TYPES[r["FinInstrmTp"]]
        if kind is None:
            if r["OptnTp"] not in ("CE", "PE"):
                raise ParseError(f"{w}: option type {r['OptnTp']!r}")
            kind = r["OptnTp"]
        if r["XpryDt"] != r["FininstrmActlXpryDt"]:
            raise ParseError(f"{w}: expiry {r['XpryDt']} differs from actual expiry {r['FininstrmActlXpryDt']}")
        out.append({
            "trade_date": r["TradDt"], "underlying": r["TckrSymb"].upper(), "kind": kind,
            "index_derivative": is_index, "expiry": _date(r["XpryDt"], "%Y-%m-%d", w),
            "strike_paise": _paise(r["StrkPric"], w) if kind != "FUT" else None,
            "open_paise": _paise(r["OpnPric"], w), "high_paise": _paise(r["HghPric"], w),
            "low_paise": _paise(r["LwPric"], w), "close_paise": _paise(r["ClsPric"], w),
            "last_paise": _paise(r["LastPric"], w), "prev_close_paise": _paise(r["PrvsClsgPric"], w),
            "settle_paise": _paise(r["SttlmPric"], w), "underlying_paise": _paise(r["UndrlygPric"], w),
            "oi": _int(r["OpnIntrst"], w), "oi_change": _int(r["ChngInOpnIntrst"], w),
            "volume": _int(r["TtlTradgVol"], w), "turnover_paise": _paise(r["TtlTrfVal"], w),
            "lot_size": _int(r["NewBrdLotQty"], w),
        })
    return out


LEGACY_FO_COLUMNS = ("INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP", "OPEN", "HIGH", "LOW",
                     "CLOSE", "SETTLE_PR", "CONTRACTS", "VAL_INLAKH", "OPEN_INT", "CHG_IN_OI", "TIMESTAMP")
LEGACY_FO_TYPES = {"FUTIDX": ("FUT", True), "FUTSTK": ("FUT", False),
                   "OPTIDX": (None, True), "OPTSTK": (None, False)}


def fo_legacy(raw: bytes, day: date) -> list[dict]:
    out = []
    for n, r in enumerate(_table(raw, LEGACY_FO_COLUMNS), start=2):
        w = f"fo_legacy line {n}"
        _check_day(_date(r["TIMESTAMP"].title(), "%d-%b-%Y", w), day, w)
        if r["INSTRUMENT"] not in LEGACY_FO_TYPES:
            raise ParseError(f"{w}: unknown instrument {r['INSTRUMENT']!r}")
        kind, is_index = LEGACY_FO_TYPES[r["INSTRUMENT"]]
        if kind is None:
            if r["OPTION_TYP"] not in ("CE", "PE"):
                raise ParseError(f"{w}: option type {r['OPTION_TYP']!r}")
            kind = r["OPTION_TYP"]
        out.append({
            "trade_date": day.isoformat(), "underlying": r["SYMBOL"].upper(), "kind": kind,
            "index_derivative": is_index, "expiry": _date(r["EXPIRY_DT"].title(), "%d-%b-%Y", w),
            "strike_paise": _paise(r["STRIKE_PR"], w) if kind != "FUT" else None,
            "open_paise": _paise(r["OPEN"], w), "high_paise": _paise(r["HIGH"], w),
            "low_paise": _paise(r["LOW"], w), "close_paise": _paise(r["CLOSE"], w),
            "last_paise": None, "prev_close_paise": None,
            "settle_paise": _paise(r["SETTLE_PR"], w), "underlying_paise": None,
            "oi": _int(r["OPEN_INT"], w), "oi_change": _int(r["CHG_IN_OI"], w),
            "volume": _int(r["CONTRACTS"], w), "turnover_paise": _paise_from_lakh(r["VAL_INLAKH"], w),
            "lot_size": None,
        })
    return out


# ── Cash market ──────────────────────────────────────────────────────────────

def cm_udiff(raw: bytes, day: date) -> list[dict]:
    out = []
    for n, r in enumerate(_table(raw, UDIFF_COLUMNS), start=2):
        w = f"cm_udiff line {n}"
        _check_day(r["TradDt"], day, w)
        if r["Sgmt"] != "CM":
            raise ParseError(f"{w}: segment {r['Sgmt']!r}")
        out.append({
            "trade_date": r["TradDt"], "symbol": r["TckrSymb"].upper(), "series": r["SctySrs"],
            "isin": r["ISIN"] or None,
            "open_paise": _paise(r["OpnPric"], w), "high_paise": _paise(r["HghPric"], w),
            "low_paise": _paise(r["LwPric"], w), "close_paise": _paise(r["ClsPric"], w),
            "last_paise": _paise(r["LastPric"], w), "prev_close_paise": _paise(r["PrvsClsgPric"], w),
            "volume": _int(r["TtlTradgVol"], w), "turnover_paise": _paise(r["TtlTrfVal"], w),
            "trades": _int(r["TtlNbOfTxsExctd"], w),
        })
    return out


LEGACY_CM_COLUMNS = ("SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "LAST", "PREVCLOSE",
                     "TOTTRDQTY", "TOTTRDVAL", "TIMESTAMP", "TOTALTRADES", "ISIN")


def cm_legacy(raw: bytes, day: date) -> list[dict]:
    out = []
    for n, r in enumerate(_table(raw, LEGACY_CM_COLUMNS), start=2):
        w = f"cm_legacy line {n}"
        _check_day(_date(r["TIMESTAMP"].title(), "%d-%b-%Y", w), day, w)
        out.append({
            "trade_date": day.isoformat(), "symbol": r["SYMBOL"].upper(), "series": r["SERIES"],
            "isin": r["ISIN"] or None,
            "open_paise": _paise(r["OPEN"], w), "high_paise": _paise(r["HIGH"], w),
            "low_paise": _paise(r["LOW"], w), "close_paise": _paise(r["CLOSE"], w),
            "last_paise": _paise(r["LAST"], w), "prev_close_paise": _paise(r["PREVCLOSE"], w),
            "volume": _int(r["TOTTRDQTY"], w), "turnover_paise": _paise(r["TOTTRDVAL"], w),
            "trades": _int(r["TOTALTRADES"], w),
        })
    return out


# ── Indices and freeze quantities ────────────────────────────────────────────

IDX_COLUMNS = ("Index Name", "Index Date", "Open Index Value", "High Index Value", "Low Index Value",
               "Closing Index Value", "Points Change", "Change(%)", "Volume", "Turnover (Rs. Cr.)",
               "P/E", "P/B", "Div Yield")


def ind_close_all(raw: bytes, day: date) -> list[dict]:
    out = []
    for n, r in enumerate(_table(raw, IDX_COLUMNS), start=2):
        w = f"ind_close_all line {n}"
        _check_day(_date(r["Index Date"], "%d-%m-%Y", w), day, w)
        out.append({
            "trade_date": day.isoformat(), "name": r["Index Name"],
            "open_paise": _points_x100(r["Open Index Value"], w),
            "high_paise": _points_x100(r["High Index Value"], w),
            "low_paise": _points_x100(r["Low Index Value"], w),
            "close_paise": _points_x100(r["Closing Index Value"], w),
        })
    return out


def qtyfreeze(raw: bytes, day: date) -> list[dict]:
    out = []
    for n, r in enumerate(_table(raw, ("S.No.", "SYMBOL", "VOL_FRZ_QTY")), start=2):
        w = f"qtyfreeze line {n}"
        qty = _int(r["VOL_FRZ_QTY"], w)
        if not r["SYMBOL"] or qty is None or qty <= 0:
            raise ParseError(f"{w}: bad row {r}")
        out.append({"symbol": r["SYMBOL"].upper(), "freeze_qty": qty})
    return out


PARSERS = {"fo_udiff": fo_udiff, "fo_legacy": fo_legacy, "cm_udiff": cm_udiff, "cm_legacy": cm_legacy,
           "ind_close_all": ind_close_all, "qtyfreeze": qtyfreeze}
