"""HD 5.1: the desk's verdict on one holding, from what each strategy says now and how it has done on this stock.

A pure function, no I/O. The rule is the spec's "Verdict table": every tested strategy votes In or Out, each vote
weighs by the strategy's grade on this stock (none 0, weak 1, some 2, strong 3), and the share of weight that says
Out picks the label. The owner's cost and profit play no part, and no sentence here mentions them.
"""
from __future__ import annotations

from dataclasses import dataclass

from stockify.desk.replay import Signal
from stockify.strategy import fmt_price

WEIGHTS = {"none": 0, "weak": 1, "some": 2, "strong": 3}
EXIT_SHARE = 0.6
TRIM_SHARE = 0.4


@dataclass(frozen=True)
class Verdict:
    label: str                         # "Hold" | "Tighten stop" | "Trim" | "Exit" | "No reliable signal"
    exit_share: float                  # weight of the rules saying exit / total weight
    nearest_exit_paise: int | None     # highest weighted exit level below the price, among rules still in
    reasons: list[str]
    for_: list[str]                    # rules saying stay in
    against: list[str]                 # rules saying get out
    counts: str


def _rupees(paise: int) -> str:
    return "₹" + fmt_price(paise)


def decide(signals: list[Signal], grades: dict[str, str], price_paise: int, atr_paise: float) -> Verdict:
    rows = []
    for s in signals:
        w = WEIGHTS.get(grades.get(s.ref, "none"), 0)
        out = not s.in_position and not s.buying_today
        rows.append((s, w, out))
    weighted = [r for r in rows if r[1] > 0]
    total = sum(w for _s, w, _o in rows)
    out_rows = [r for r in weighted if r[2]]
    in_rows = [r for r in weighted if not r[2]]
    for_ = [s.display_name for s, _w, _o in in_rows]
    against = [s.display_name for s, _w, _o in out_rows]

    if total == 0:
        return Verdict(
            "No reliable signal", 0.0, None,
            ["None of the tested rules has a good enough record on this stock to be trusted, so the desk "
             "gives no call."],
            for_, against, "0 of 0 tested rules say exit; none has a record on this stock")

    share = sum(w for _s, w, _o in out_rows) / total
    best = max(w for _s, w, _o in weighted)
    best_rows = [r for r in weighted if r[1] == best]
    best_out = sum(1 for r in best_rows if r[2])
    nb = len(best_rows)
    if best_out == nb:
        tail = ("the 1 with the best record on this stock is among them" if nb == 1
                else f"the {nb} with the best record on this stock are among them")
    elif best_out:
        tail = f"{best_out} of the {nb} with the best record on this stock are among them"
    else:
        tail = "none of the best-record rules is among them"
    counts = f"{len(out_rows)} of {len(weighted)} tested rules say exit; {tail}"

    # The nearest exit level: only rules still in, with a say, whose level sits below the price.
    levels = [s.exit_level_paise for s, w, o in rows
              if s.in_position and w > 0 and s.exit_level_paise is not None
              and s.exit_level_paise < price_paise]
    nearest = max(levels) if levels else None

    strong_out = any(w >= WEIGHTS["some"] for _s, w, _o in out_rows)
    pct = round(share * 100)
    if share >= EXIT_SHARE and strong_out:
        label = "Exit"
        reasons = [f"Rules carrying {pct}% of the weight say to get out of this stock.",
                   "At least one of them has a real record on this stock."]
    elif share >= TRIM_SHARE:
        label = "Trim"
        if share >= EXIT_SHARE:
            reasons = [f"Rules carrying {pct}% of the weight say to be out, but only rules with a weak record "
                       "on this stock, so the desk stops at trimming."]
        else:
            reasons = [f"Rules carrying {pct}% of the weight say to be out, so the desk suggests selling part."]
    elif nearest is not None and price_paise - nearest <= atr_paise:
        label = "Tighten stop"
        reasons = [f"The price {_rupees(price_paise)} is within one day's typical move ({_rupees(round(atr_paise))}) "
                   f"of the nearest exit level, {_rupees(nearest)}.",
                   "Move your stop up to that level."]
    else:
        label = "Hold"
        reasons = [f"Only {pct}% of the weight says to be out, and the price {_rupees(price_paise)} is not "
                   "close to an exit level."]
    return Verdict(label, share, nearest, reasons, for_, against, counts)
