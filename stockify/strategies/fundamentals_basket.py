"""FS 6: the Fundamentals basket. Holds the best-placed stocks of the fundamentals screen in equal weights, CNC,
rebalanced on each month's first daily bar (spec "Calls made", 1).

Params: `ranking` ({balance label: [{"date", "symbols" best first}]}, set by stockify.fundamentals.history: the screen's
order on the first trading day of each month, from figures usable that day), `balance` (which label to follow,
"0.5"), `basket_size` (20).

On the first daily bar of a month (the bar's own IST date) the strategy takes the newest list dated on or before that
day, keeps the first `basket_size` of its symbols that are in the run's data (a symbol that is not is skipped and
logged once), and after every bar of that moment:
* sells all of each held stock no longer in the basket, and trims one held above its share;
* buys each basket stock up to its share, from the funds available. A share is 95 % of the value (cash plus held stocks
  at their last close) over the basket's size, bought with the 3 % headroom of the CNC sizing (sma_cross), so a fill
  at up to 3 % above the close still fits. A list with fewer usable stocks than the basket's size keeps the same share
  per stock, and the money of the empty places stays in cash (ruling R11).
Sale proceeds are not all usable on the day of the sale, so on the next daily bar it buys once more, to fill any
share still short. A change smaller than 5 % of a share is not traded.
"""
from __future__ import annotations

from stockify import timeutil
from stockify.strategy import Strategy, fmt_qty

INVEST_PCT = 95
HEADROOM_PCT = 103
BAND_PCT = 5


class FundamentalsBasket(Strategy):
    DISPLAY_NAME = "Fundamentals basket"   # what the UI shows (stockify.display); the catalogue test fails without one
    DESCRIPTION = "Holds the best-placed stocks from the fundamentals screen, rebalanced each month."
    BAR_INTERVAL_S = 86400           # the one bar interval it subscribes to (the chart reads it)
    # The parameter schema for the catalogue and the /tests form (data only; on_start reads ctx.params).
    PARAMS = [
        {'name': 'ranking', 'label': 'Monthly lists', 'type': 'ranking', 'default': None, 'required': True, 'help': "Set by the fundamentals screener: each month's stocks, best first."},
        {'name': 'balance', 'label': 'Valuation against quality', 'type': 'str', 'default': '0.5', 'required': False, 'help': 'Which of the screener\'s lists to follow, by the weight it gives valuation, e.g. 0.5.'},
        {'name': 'basket_size', 'label': 'Stocks held', 'type': 'int', 'default': 20, 'required': False, 'help': 'How many of the best-placed stocks to hold.', 'min': '5', 'max': '50', 'unit': 'stocks, e.g. 20'},
    ]

    def on_start(self, ctx):  # noqa: ANN001
        p = ctx.params
        ranking = p["ranking"]
        self.balance = str(p.get("balance", "0.5"))
        self.size = int(p.get("basket_size", 20))
        if self.balance not in ranking:
            raise ValueError(f"balance {self.balance} is not one of the ranking's lists ({', '.join(sorted(ranking))})")
        if self.size < 1:
            raise ValueError(f"basket_size must be at least 1, not {self.size}")
        entries = sorted(ranking[self.balance], key=lambda e: e["date"])
        ids: dict[str, int] = {}
        missing: list[str] = []
        for e in entries:
            for s in e["symbols"]:
                if s in ids or s in missing:
                    continue
                try:
                    ids[s] = ctx.resolve(s if ":" in s else "NSE:" + s)
                except KeyError:
                    missing.append(s)
        for s in sorted(missing):
            ctx.log("WARN", f"{s} is not in this run's data: left out of every basket")
        self.lists = [[e["date"], [ids[s] for s in e["symbols"] if s in ids][:self.size]] for e in entries]
        self.names = {str(i): s for s, i in ids.items()}
        self.month = None            # "YYYY-MM" of the last rebalance
        self.day = None              # ISO date of the bars last seen
        self.stage = None            # "rebalance" | "top_up" pending for this moment's timer; "bought" after a rebalance
        self.basket: list[int] = []
        self.share = 0               # paise per stock at the last rebalance
        self.list_day = None
        for i in sorted(ids.values()):
            ctx.subscribe_bars(i, self.BAR_INTERVAL_S)

    def on_bar(self, ctx, bar):  # noqa: ANN001
        if bar.interval_s != self.BAR_INTERVAL_S:
            return
        d = timeutil.ns_to_ist(bar.start_ns).date().isoformat()
        if d == self.day:
            return                                    # this moment's timer is already set (or nothing to do)
        self.day = d
        if d[:7] != self.month:
            self.month, self.stage = d[:7], "rebalance"
        elif self.stage == "bought":
            self.stage = "top_up"
        else:
            self.stage = None
            return
        ctx.schedule_at(ctx.now() + 1)                # after every bar published at this moment

    def on_timer(self, ctx, timer_id):  # noqa: ANN001
        if self.stage == "rebalance":
            self.stage = "bought" if self._rebalance(ctx) else None
        elif self.stage == "top_up":
            self.stage = None
            self._buy(ctx)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _close(self, ctx, iid: int, today_only: bool) -> int | None:  # noqa: ANN001
        bars = ctx.bars(iid, self.BAR_INTERVAL_S, 1)
        if not bars:
            return None
        if today_only and timeutil.ns_to_ist(bars[-1].start_ns).date().isoformat() != self.day:
            return None                               # no bar for it today
        return bars[-1].close

    def _held(self, ctx) -> dict[int, tuple[int, int]]:  # noqa: ANN001
        return {q.instrument: (q.qty, q.avg_price) for q in ctx.positions() if q.product == "CNC" and q.qty > 0}

    def _rebalance(self, ctx) -> bool:  # noqa: ANN001
        chosen = None
        for entry in self.lists:
            if entry[0] <= self.day:
                chosen = entry
        if chosen is None:
            return False                              # no list yet: stay in cash
        self.list_day, self.basket = chosen[0], list(chosen[1])
        held = self._held(ctx)
        value = ctx.funds().cash
        for iid, (qty, avg) in held.items():
            last = self._close(ctx, iid, False)
            value += qty * (avg if last is None else last)
        self.share = value * INVEST_PCT // 100 // self.size     # R11: a short list keeps equal slots; the rest is cash
        band = self.share * BAND_PCT // 100
        for iid in sorted(held):
            qty = held[iid][0]
            name = self.names.get(str(iid), str(iid))
            if iid not in self.basket:
                ctx.place(iid, "SELL", qty, "CNC", "MARKET", tag="exit",
                          note=f"Sold {fmt_qty(qty)} {name}: no longer among the best {self.size} of the "
                               f"fundamentals list of {self.list_day}")
                continue
            close = self._close(ctx, iid, True)
            if close is None:
                continue
            over = qty - self.share // close
            if over > 0 and over * close >= band:
                ctx.place(iid, "SELL", over, "CNC", "MARKET", tag="trim",
                          note=f"Sold {fmt_qty(over)} {name}: back to an equal share of the basket")
        self._buy(ctx)
        return True

    def _buy(self, ctx) -> None:  # noqa: ANN001
        held = self._held(ctx)
        budget = ctx.funds().available
        band = self.share * BAND_PCT // 100
        for iid in self.basket:
            close = self._close(ctx, iid, True)
            if close is None:
                continue
            price = close * HEADROOM_PCT // 100
            have = held.get(iid, (0, 0))[0]
            short = self.share // price - have
            if short <= 0 or (have > 0 and short * close < band):
                continue
            qty = min(short, budget // price)
            if qty <= 0:
                continue
            budget -= qty * price
            ctx.place(iid, "BUY", qty, "CNC", "MARKET", tag="entry",
                      note=f"Bought {fmt_qty(qty)} {self.names.get(str(iid), str(iid))}: among the best {self.size} "
                           f"of the fundamentals list of {self.list_day}")
