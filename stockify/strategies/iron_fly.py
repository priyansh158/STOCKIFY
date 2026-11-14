"""Intraday iron fly (M4.13): the short straddle of short_straddle_stop plus long wings a set number of strikes
out on each side, bought first, to show the margin and risk difference.

Same entry, stops (on the two short legs) and time exit as short_straddle_stop; the wings are closed at the time
exit. The margin model is the run's choice (`margin.model`): the flat model does not credit the wings, SPAN-lite
(M4.8) will. Mechanics: stockify/strategies/_option_legs.py.

Params: those of short_straddle_stop, plus `wing_strikes` (2: the wings are the 2nd strike above and below ATM).
"""
from __future__ import annotations

from decimal import Decimal

from stockify.strategies._option_legs import OptionLegs


class IronFly(OptionLegs):
    DISPLAY_NAME = "Iron Fly"   # what the UI shows (stockify.display); the catalogue test fails without one
    DESCRIPTION = 'Sells the at-the-money call and put, buys protective wings a few strikes out, and closes everything at the exit time.'   # one plain sentence for the New test form (the module docstring is for developers)
    PARAMS = [
        {'name': 'underlying', 'label': 'Index for the options', 'type': 'underlying', 'default': 'NIFTY', 'required': False, 'help': 'the index whose options are traded'},
        {'name': 'entry_hhmm', 'label': 'Entry time (IST)', 'type': 'hhmm', 'default': '09:30', 'required': False, 'help': 'when to buy the wings and sell the at-the-money call and put'},
        {'name': 'exit_hhmm', 'label': 'Exit time (IST)', 'type': 'hhmm', 'default': '15:15', 'required': False, 'help': 'when to close every leg still open'},
        {'name': 'stop_pct', 'label': 'Stop above entry (fraction)', 'type': 'decimal', 'default': '0.3', 'required': False, 'help': 'buy a short leg back when it costs this much more than it was sold for (0.3 = 30 %)',
         'max': '1', 'unit': 'a fraction ("0.3" = 30 %)'},
        {'name': 'wing_strikes', 'label': 'Wings out (strikes)', 'type': 'int', 'default': 2, 'required': False, 'help': 'how many strikes above and below the at-the-money strike the bought wings are'},
        {'name': 'lots', 'label': 'Lots per option', 'type': 'int', 'default': 1, 'required': False, 'help': 'lots of each option traded'},
        {'name': 'min_days', 'label': 'Skip expiries closer than (days)', 'type': 'int', 'default': 0, 'required': False, 'help': 'skip expiries fewer than this many days away (0 trades the nearest, even on expiry day)'},
        {'name': 'protection_bp', 'label': 'Price band (basis points)', 'type': 'int', 'default': 5000, 'required': False, 'help': 'price band for the market and stop orders, in basis points (5000 = 50 %)', 'min': '1', 'max': '10000', 'unit': 'basis points (5000 = 50 %)'},
    ]

    def on_start(self, ctx):  # noqa: ANN001
        p = ctx.params
        self.underlying = p.get("underlying", "NIFTY")
        self.entry_hhmm = p.get("entry_hhmm", "09:30")
        self.exit_hhmm = p.get("exit_hhmm", "15:15")
        self.stop_pct = Decimal(str(p.get("stop_pct", "0.3")))
        self.wing_strikes = int(p.get("wing_strikes", 2))
        if self.wing_strikes < 1:
            raise ValueError("wing_strikes must be 1 or more")
        self.lots = int(p.get("lots", 1))
        self.min_days = int(p.get("min_days", 0))
        self.protection_bp = int(p.get("protection_bp", 5000))
        self.begin(ctx)
