"""MEZ 5.1 (D-69, M5.6): the live margin model, in the oracle. The engine's twin is engine/src/margin/live.cpp.

Kite's `/margins/basket` can price the book the session actually holds. Its answer reaches an engine only as a
journaled REFDATA record, `margin_requirement` (A1 §5), never as a call made from the engine path, so a replay of the
journal gives the same bytes. The payload is integers only:

    {"portfolio_sha256": str, "legs": [[iid, units], ...] (sorted), "span_paise": int, "exposure_paise": int,
     "additional_paise": int, "option_premium_paise": int, "requested_ns": int, "source": "kite_basket"}

`portfolio_sha256` is the hash of exactly the legs the staircase prices (`StaircaseMargin._group`: NRML, kind FUT/CE/PE,
with an underlying, units not 0; MIS futures and options are in the book as NRML, D-80). The model is the staircase
(B1..B5, D-64) except that the current book's requirement, which funds and so every pre-trade check use, is L1 when a
fresh answer for that very portfolio exists: span + exposure + additional (D-62: the option premium is not margin).
The pre-trade increment (`pair`) always uses the staircase. At DAY_END a fresh L1 answer is one margin_daily row:
`underlying_id` 0, `step` L1_kite, `scan` = span, `somc` 0, `elm` = exposure, `addons` = additional.

An answer is as old as its record: `LiveAnswers.put(payload, t)` is called with the journal time of the record, and it
is fresh while `0 <= now - t <= fresh_ns`.
"""
from __future__ import annotations

import hashlib

from stockify import numerics
from stockify.fno.spanlite import UnderlyingMargin
from stockify.fno.staircase import StaircaseMargin

SOURCE = "kite_basket"
L1_STEP = "L1_kite"
LIVE_FRESH_S = 360                                  # sessionconfig.LIVE_FRESH_S: how old an answer may be
MAX_IID = 2**32 - 1                                 # the engine's instrument ids are 32 bits
MAX_I64 = 2**63 - 1
MAX_PAISE = 10**18                                  # three of them still fit an int64
KEYS = ("additional_paise", "exposure_paise", "legs", "option_premium_paise", "portfolio_sha256", "requested_ns",
        "source", "span_paise")
_HEX = frozenset("0123456789abcdef")


class PayloadError(ValueError):
    """A margin_requirement payload that is refused; the runner words it `margin_requirement: <message>`."""


def portfolio_sha256(legs: list[tuple[int, int]]) -> str:
    """The SHA-256 of the canonical JSON of the sorted [[iid, units], ...]."""
    text = numerics.dumps_canonical([[iid, units] for iid, units in sorted(legs)])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _int(v) -> bool:  # noqa: ANN001
    return type(v) is int


def _whole(v) -> bool:  # noqa: ANN001
    """What the engine's JSON reader calls a whole number: an int from -2^63 to 2^64 - 1 (beyond that it reads a float)."""
    return type(v) is int and -2**63 <= v <= 2**64 - 1


def check_payload(p) -> dict:  # noqa: ANN001
    """The payload as given, or a PayloadError: exactly the keys above, integers only, the hash that of the legs."""
    if not isinstance(p, dict):
        raise PayloadError("payload must be an object")
    if sorted(p) != list(KEYS):
        raise PayloadError("payload must have exactly the keys " + ", ".join(KEYS))
    sha = p["portfolio_sha256"]
    if not (isinstance(sha, str) and len(sha) == 64 and set(sha) <= _HEX):
        raise PayloadError("portfolio_sha256 must be 64 lowercase hex digits")
    legs = p["legs"]
    if not (isinstance(legs, list) and all(isinstance(x, list) and len(x) == 2 and _whole(x[0]) and _whole(x[1])
                                           for x in legs)):
        raise PayloadError("legs must be a list of [instrument, units] pairs of whole numbers")
    if any(not 1 <= x[0] <= MAX_IID or x[1] == 0 or not -MAX_I64 <= x[1] <= MAX_I64 for x in legs):
        raise PayloadError(f"a leg needs an instrument id from 1 to {MAX_IID} and units other than 0")
    if any(a[0] >= b[0] for a, b in zip(legs, legs[1:])):
        raise PayloadError("legs must be sorted by instrument, one per instrument")
    for key in ("span_paise", "exposure_paise", "additional_paise", "option_premium_paise"):
        if not _int(p[key]) or not 0 <= p[key] <= MAX_PAISE:
            raise PayloadError(f"{key} must be a whole number of paise from 0 to {MAX_PAISE}")
    if not _int(p["requested_ns"]) or not 0 <= p["requested_ns"] <= MAX_I64:
        raise PayloadError("requested_ns must be a whole number of nanoseconds, at least 0")
    if p["source"] != SOURCE:
        raise PayloadError(f"source must be {SOURCE!r}")
    if sha != portfolio_sha256([(a, b) for a, b in legs]):
        raise PayloadError("portfolio_sha256 is not the hash of legs")
    return p


class LiveAnswers:
    """The journaled answers an engine holds: the latest per portfolio hash, aged from the record's own time."""

    def __init__(self) -> None:
        self._by_sha: dict[str, tuple[int, dict]] = {}

    def put(self, payload: dict, t: int) -> None:
        self._by_sha[payload["portfolio_sha256"]] = (t, payload)

    def fresh(self, sha: str, now: int, fresh_ns: int) -> dict | None:
        held = self._by_sha.get(sha)
        if held is None:
            return None
        t, payload = held
        return payload if 0 <= now - t <= fresh_ns else None


class LiveWithFallbackMargin(StaircaseMargin):
    """The staircase, with a fresh journaled Kite answer pricing the current book (L1)."""

    def __init__(self, bundle: dict, engine, answers: LiveAnswers, fresh_s: int | None = None):  # noqa: ANN001
        super().__init__(bundle, engine)
        if fresh_s is None:
            fresh_s = (getattr(engine, "run_cfg", None) or {}).get("margin", {}).get("live_fresh_s", LIVE_FRESH_S)
        self._answers = answers
        self._fresh_ns = fresh_s * 10**9
        self._applied: set[tuple] = set()               # the distinct answers that priced something

    @property
    def l1_answers(self) -> int:
        return len(self._applied)

    @property
    def labels(self) -> set[str]:
        return super().labels | {f"margin_live:L1_answers={self.l1_answers}"}

    def _apply(self, a: dict) -> None:
        """Note that this answer priced the book: a label that counts answers, not how often a book is priced."""
        self._applied.add((a["portfolio_sha256"], a["span_paise"], a["exposure_paise"], a["additional_paise"],
                           a["option_premium_paise"], a["requested_ns"]))

    def _live(self, legs, inst) -> dict | None:  # noqa: ANN001
        """The fresh answer for exactly this book's staircase legs, or None."""
        flat = [(row["id"], units) for rows in self._group(legs, inst).values() for row, units in rows]
        if not flat:
            return None
        return self._answers.fresh(portfolio_sha256(flat), self._e.now, self._fresh_ns)

    def breakdown(self, legs, inst, price, date: str) -> list[UnderlyingMargin]:  # noqa: ANN001
        a = self._live(legs, inst)
        if a is None:
            return super().breakdown(legs, inst, price, date)
        self._apply(a)
        return [UnderlyingMargin(0, a["span_paise"], 0, a["exposure_paise"], a["additional_paise"], step=L1_STEP)]

    def requirement(self, legs, inst, price, date: str) -> int:  # noqa: ANN001
        a = self._live(legs, inst)
        if a is None:
            return super().requirement(legs, inst, price, date)
        self._apply(a)
        return a["span_paise"] + a["exposure_paise"] + a["additional_paise"]
