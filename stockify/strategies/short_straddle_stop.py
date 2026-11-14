"""Intraday short straddle with per-leg stops (M4.13): sell the ATM call and put at a set time, stop each leg out
at a set percentage above its entry price, close what is left at a set time.

The ATM strike is the one nearest the chain view's forward (ctx.chain, M4.4). Run it on recorded chains with the
quote-based fill model (`snapshot_quote`); the margin model is the run's choice (`margin.model`, flat for now).
Mechanics: stockify/strategies/_option_legs.py.

Params: `underlying` ("NIFTY"), `entry_hhmm` ("09:30"), `exit_hhmm` ("15:15"), `stop_pct` ("0.3" = 30 % above
entry), `lots` (1), `min_days` (0: the nearest expiry, even on its expiry day), `protection_bp` (5000).
"""
from __future__ import annotations

from decimal import Decimal

from stockify.strategies._option_legs import OptionLegs


class ShortStraddleStop(OptionLegs):
    DISPLAY_NAME = "Short Straddle with Stop"   # what the UI shows (stockify.display); the catalogue test fails without one
    PARAMS = [
        {'name': 'underlying', 'label': 'Index for the options', 'type': 'underlying', 'default': 'NIFTY', 'required': False, 'help': 'the index whose options are sold'},
        {'name': 'entry_hhmm', 'label': 'Entry time (IST)', 'type': 'hhmm', 'default': '09:30', 'required': False, 'help': 'when to sell the at-the-money call and put'},
        {'name': 'exit_hhmm', 'label': 'Exit time (IST)', 'type': 'hhmm', 'default': '15:15', 'required': False, 'help': 'when to buy back whatever is still open'},
        {'name': 'stop_pct', 'label': 'Stop above entry (fraction)', 'type': 'decimal', 'default': '0.3', 'required': False, 'help': 'buy a leg back when it costs this much more than it was sold for (0.3 = 30 %)',
         'max': '1', 'unit': 'a fraction ("0.3" = 30 %)'},
        {'name': 'lots', 'label': 'Lots per option', 'type': 'int', 'default': 1, 'required': False, 'help': 'lots of each option sold'},
        {'name': 'min_days', 'label': 'Skip expiries closer than (days)', 'type': 'int', 'default': 0, 'required': False, 'help': 'skip expiries fewer than this many days away (0 trades the nearest, even on expiry day)'},
        {'name': 'protection_bp', 'label': 'Price band (basis points)', 'type': 'int', 'default': 5000, 'required': False, 'help': 'price band for the market and stop orders, in basis points (5000 = 50 %)', 'min': '1', 'max': '10000', 'unit': 'basis points (5000 = 50 %)'},
    ]

    def on_start(self, ctx):  # noqa: ANN001
        p = ctx.params
        self.underlying = p.get("underlying", "NIFTY")
        self.entry_hhmm = p.get("entry_hhmm", "09:30")
        self.exit_hhmm = p.get("exit_hhmm", "15:15")
        self.stop_pct = Decimal(str(p.get("stop_pct", "0.3")))
        self.lots = int(p.get("lots", 1))
        self.min_days = int(p.get("min_days", 0))
        self.protection_bp = int(p.get("protection_bp", 5000))
        self.wing_strikes = 0
        self.begin(ctx)
