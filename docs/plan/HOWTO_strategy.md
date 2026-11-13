# How to write, run and port a strategy (M3.15)

This walks through one complete strategy, an **opening-range breakout with a stop and a time exit**, from the
Python code to a run you can read, a re-run that proves it is reproducible, and a C++ port that gives the same
result. Every command below was run on 2026-11-06 against the lab's data and gave the output shown.

Everything is simulated. No order ever reaches a broker.

For options (the chain view, margin models, expiry, comparisons and margin calibration), read this page first, then
[HOWTO_options.md](HOWTO_options.md).

## 1. The strategy

The code is `stockify/strategies/opening_range_breakout.py` (the M2 reference strategy). In words:

- The **opening range** is the highest high and lowest low of the first `range_minutes` minute bars after 09:15.
- A later bar that **closes above the range** buys `qty` shares (MIS, intraday) with a market order, and each
  time that order fills places a **stop** for exactly the filled quantity: a sell stop-market at the bottom of the range
  (a rejected entry gets none). With `short: true`, a close **below** the
  range sells short instead, with a buy stop at the top.
- At `exit_hhmm` (default 15:10) a timer cancels the stop and closes whatever is still open. If the strategy
  forgets, the broker's MIS square-off at the cut-off would do it, with a fee.
- At most one trade a day.

```python
from stockify import timeutil
from stockify.strategy import Strategy, encode_state, fmt_price, fmt_qty


class OpeningRangeBreakout(Strategy):
    # The parameter schema for the catalogue and the /tests form (data only; on_start reads ctx.params).
    PARAMS = [
        {'name': 'symbol', 'label': 'Stock', 'type': 'symbol', 'default': None, 'required': True, 'help': 'the stock to trade intraday, e.g. NSE:RELIANCE'},
        {'name': 'qty', 'label': 'Shares per trade', 'type': 'int', 'default': None, 'required': True, 'help': 'shares per trade'},
        {'name': 'range_minutes', 'label': 'Opening range (minutes)', 'type': 'int', 'default': 15, 'required': False, 'help': 'minutes after 09:15 that set the range'},
        {'name': 'short', 'label': 'Also sell short', 'type': 'bool', 'default': False, 'required': False, 'help': 'also sell short when price breaks below the range'},
        {'name': 'exit_hhmm', 'label': 'Exit time (IST)', 'type': 'hhmm', 'default': '15:10', 'required': False, 'help': 'time to close the position, IST'},
    ]

    def on_start(self, ctx):
        p = ctx.params                                   # the run config's strategy.params
        self.symbol = p["symbol"]                        # "NSE:RELIANCE"
        self.iid = ctx.resolve(self.symbol)              # -> instrument id
        self.minutes = int(p.get("range_minutes", 15))
        self.qty = int(p["qty"])
        self.short = bool(p.get("short", False))
        self.exit_hhmm = p.get("exit_hhmm", "15:10")
        self.day = None
        ctx.subscribe_bars(self.iid, 60)                 # minute bars

    def _new_day(self, ctx, bar) -> None:
        self.day = ctx.today()
        self.open_ns = timeutil.ist_ns(self.day, "09:15")
        self.hi = self.lo = None
        self.traded = False
        self.entry_id = None
        self.stop_side = None
        self.stop_trigger = 0
        self.stop_ids = []
        exit_ns = timeutil.ist_ns(self.day, self.exit_hhmm)
        if exit_ns > ctx.now():
            ctx.schedule_at(exit_ns)                     # the time exit

    def on_bar(self, ctx, bar):
        if ctx.today() != self.day:
            self._new_day(ctx, bar)
        if bar.start_ns < self.open_ns:
            return
        if bar.start_ns < self.open_ns + self.minutes * 60 * 1_000_000_000:
            self.hi = bar.high if self.hi is None else max(self.hi, bar.high)
            self.lo = bar.low if self.lo is None else min(self.lo, bar.low)
            return
        if self.hi is None:
            return
        ctx.plot("range_high", self.hi, instrument=self.iid)     # paise: the range as the bars after it see it
        ctx.plot("range_low", self.lo, instrument=self.iid)
        if self.traded:
            return
        tick = ctx.spec(self.iid)["tick_paise"]
        if bar.close > self.hi:
            self.entry_id = ctx.place(
                self.iid, "BUY", self.qty, "MIS", "MARKET", tag="orb_long",
                note=f"Bought {fmt_qty(self.qty)} {self.symbol}: price {fmt_price(bar.close)} broke above the "
                     f"{self.minutes}-minute opening range high of {fmt_price(self.hi)}")
            self.stop_side, self.stop_trigger = "SELL", self.lo // tick * tick
            self.traded = True
        elif self.short and bar.close < self.lo:
            self.entry_id = ctx.place(
                self.iid, "SELL", self.qty, "MIS", "MARKET", tag="orb_short",
                note=f"Sold short {fmt_qty(self.qty)} {self.symbol}: price {fmt_price(bar.close)} broke below the "
                     f"{self.minutes}-minute opening range low of {fmt_price(self.lo)}")
            self.stop_side, self.stop_trigger = "BUY", -(-self.hi // tick) * tick
            self.traded = True

    def on_fill(self, ctx, fill):
        # The stop exists only for what the entry has filled: one SL-M of exactly each fill's quantity. A rejected or
        # cancelled entry fills nothing, so it never gets a stop (and cannot open a reverse position).
        if self.entry_id is not None and fill.order_id == self.entry_id:
            qty = fmt_qty(fill.qty)
            trigger = fmt_price(self.stop_trigger)
            note = (f"Stop-loss for the {qty} bought: sells if price falls to {trigger}" if self.stop_side == "SELL"
                    else f"Stop-loss for the {qty} sold short: buys back if price rises to {trigger}")
            self.stop_ids.append(ctx.place(self.iid, self.stop_side, fill.qty, "MIS", "SL-M",
                                           trigger=self.stop_trigger, tag="orb_stop", note=note))

    # A paper session's day-to-day state (A2 §4.1): the default, with the date `day` as ISO text.
    def save_state(self) -> dict:
        return encode_state(self, {**self.state_attrs(), "day": self.day.isoformat() if self.day else None})

    def load_state(self, state: dict) -> None:
        from datetime import date
        super().load_state(state)
        self.day = date.fromisoformat(self.day) if self.day else None

    def on_timer(self, ctx, timer_id):
        for stop_id in self.stop_ids:
            ctx.cancel(stop_id)
        pos = ctx.position(self.iid, "MIS")
        if pos and pos.qty:
            side = "SELL" if pos.qty > 0 else "BUY"
            ctx.place(self.iid, side, abs(pos.qty), "MIS", "MARKET", tag="orb_exit",
                      note=f"Closed {fmt_qty(abs(pos.qty))} {self.symbol} at the {self.exit_hhmm} time exit")
```

`save_state` / `load_state` matter only in paper trading, where each trading day runs a fresh engine and `on_start`
runs on the first day only: everything the strategy keeps on `self` must come back the next morning. The default
`save_state` saves every attribute that is an int, bool, str, None, `Decimal`, tuple, list or str-keyed dict; this
strategy keeps a `date` (`self.day`), so it writes it as text and reads it back (A2 §4.1).

Things to know when you write your own (the full API is A2 §4):

- **Money is whole paise** (`int`): ₹1,315.56 is `131556`. Use `ctx.rupees(p)` / `ctx.paise("1315.56")` to
  convert. Times are UTC nanoseconds; `timeutil.ist_ns(day, "09:15")` gives an IST wall time.
- **A stop price must be on the tick grid**: round down for a sell stop (`lo // tick * tick`), up for a buy stop.
- **`type` is required** in `ctx.place`. A MARKET order becomes a protected limit order (the broker's
  market-protection band), as on Kite.
- **The load check (A2 §4.4)** refuses strategy modules that import `time`, `random`, `os`, `pathlib`, `io`,
  `socket`, `requests`, `threading`, `sqlite3` and the like, or call `open`, `eval`, `exec`, `datetime.now()` or
  `date.today()`. Use `ctx.now()`, `ctx.today()`, `ctx.rng()` and `ctx.log()` instead. This is what makes a run
  reproducible.
- **Imports come from an allowlist (LCB 5.3, 5.4).** A strategy module may import only `__future__`, `datetime`,
  `decimal`, `re`, `math`, `dataclasses`, `typing`, `enum`, `collections`, `itertools`, `functools`, `statistics`,
  `stockify.strategy`, `stockify.engine`, `stockify.timeutil`, and modules of its own package (for the reference
  strategies, `stockify.strategies.*`; a platform package such as `stockify.tools` does not count). Names imported
  from those modules are checked too (LCB 5.5): no `*`, no `_`-prefixed names, no module-like names (`sys`,
  `time`, `importlib`, `ast`, `inspect`, …), and nothing that is itself a module, so `from stockify.strategy import
  importlib` is refused while `from decimal import Decimal` is fine. `import X.Y` must name an allowed module. The
  check also refuses:
  - attributes starting with `_` other than on `self`/`cls` (and dunders such as `self.__class__` even there); the
    one exception is the context's instrument table `ctx._rows()`, which the option helpers read;
  - attributes named `importlib`, `sys`, `modules`, `kite_client`, `kiteconnect`, `os`, `subprocess`, `socket`,
    `time`, `ast`, `inspect`, `builtins`, or a frame's `f_globals`/`f_back`/… (allowed modules hold some of these:
    `stockify.timeutil.time` is the wall clock);
  - format strings that reach a private attribute (`'{0.__class__}'.format(x)`);
  - calls to `getattr`, `setattr`, `delattr`, `globals`, `locals`, `vars`, `breakpoint`, `open`, `input`,
    `__import__`, `exec`, `eval` and `compile`, and the names `__builtins__`, `__loader__` and `__spec__`;
  - the wall clock: the attributes `now`, `today`, `utcnow`, `time_ns`, `monotonic` and `perf_counter` on anything
    but the strategy context (`ctx.now()` and `ctx.today()` are the API), and `fromtimestamp` without a time and an explicit `tz`;
  - the wall-clock functions of `stockify.timeutil` (LCB 5.14): `now_ns`, `now_ist` and `ist_today` may not be
    imported (under any name), nor used as an attribute of any object, the context included. The rest of
    `stockify.timeutil` (`ist_ns`, `ns_to_ist`, `parse_hhmm`, …) only converts times and is fine;
  - the attributes `exec`, `eval`, `compile`, `bltns` and `builtins` on any object, and anything that evaluates a
    string as code: `get_type_hints`, `ForwardRef`, `evaluate_forward_ref`, `get_annotations`, `.register`, and any
    string inside an annotation (`def f(a: "X")`, `-> "X"`, `x: "int"`); write annotations as plain names (with
    `from __future__ import annotations` they are never evaluated anyway) (LCB 5.7);
  - rebinding `ctx`: it may only be a parameter of a `def` (no assignment, loop, `with ... as`, walrus,
    comprehension, `except ... as`, import alias, lambda parameter or default value), and a call to a function of
    the same module may pass only `ctx` itself into a parameter named `ctx`;
  - attribute chains that start at an imported module are followed over the real objects (LCB 5.6): reaching a
    module that is not on the list (`enum.bltns` is builtins, `dataclasses.types`), a dangerous builtin (`eval`,
    `open`, `getattr`, …) or a frame or code object is refused, and so is a step that does not resolve or indexing
    into a module's names. `bar.open` is fine: `open` is refused only along such a chain.

  The paper daemon runs strategies in a process that holds a live Kite token, so a strategy must never be able to
  reach it. The source is parsed **before** the module is imported, so a refused file's top-level code never runs;
  modules the strategy imports from its own package (such as `stockify/strategies/_option_legs.py`) are checked the
  same way. Defining `__init__` or other dunder methods in a class is fine; private helper methods on `self`
  (`self._new_day(...)`) are fine. This guards against accidents; it is not a sandbox.
- To make your own, put the file in `stockify/strategies/` and name it in the config as
  `"stockify.strategies.<module>:<Class>"`. The `PARAMS` list is only a description, for the strategy catalogue
  and the `/tests` form (labels, types, defaults, help text); the strategy itself reads `ctx.params`.

### Say why: `note=` and `ctx.plot`

A trade the chart shows should say why it happened, in the strategy's own words, and the lines a strategy
decides on should be drawn on the chart too. Two calls do that, and both are written into the run folder, so a
re-run gives the same bytes:

- **`note=`** on `ctx.place` is one sentence of plain English for that order (`orders.csv`, column `note`; at
  most 200 characters, longer is cut with `…`). Name the quantity and the number that decided it, and give a reason,
  not just a side. Format numbers with `fmt_price(paise)` (₹ without the sign, Indian grouping: `241840` is
  `2,418.40`) and `fmt_qty(units)` (`125000` is `1,25,000`) from `stockify.strategy`, so the text reads like the
  rest of the lab. A note changes no fill.
- **`ctx.plot(name, value, *, kind="price", instrument=0)`** adds a point to a named line at the current engine
  time (`plots.csv`). `value` is a whole number: paise for `kind="price"`, and for `kind="level"` an indicator
  times 10,000 (`to_level(54.3217)` is `543217`). Never pass a float. `instrument` is the instrument id the line
  belongs to (0: the whole run). A run keeps at most 8 lines and 200,000 points; beyond that a call is ignored and
  one warning is logged.

```python
from stockify.strategy import Strategy, fmt_price


class BuyBelow(Strategy):
    def on_start(self, ctx):
        self.iid = ctx.resolve(ctx.params["symbol"])
        self.limit = int(ctx.params["below"])             # paise
        self.done = False
        ctx.subscribe_bars(self.iid, 86400)

    def on_bar(self, ctx, bar):
        if not self.done and bar.close < self.limit:
            ctx.place(self.iid, "BUY", 1, "CNC", "MARKET", tag="entry",
                      note=f"Bought 1: the close {fmt_price(bar.close)} fell under {fmt_price(self.limit)}")
            self.done = True
```

```python
from stockify.strategy import Strategy


class DrawAverage(Strategy):
    def on_start(self, ctx):
        self.iid = ctx.resolve(ctx.params["symbol"])
        ctx.subscribe_bars(self.iid, 86400)

    def on_bar(self, ctx, bar):
        avg = ctx.sma(self.iid, 86400, 3)                 # a float; NaN until three bars exist
        if avg == avg:
            ctx.plot("sma3", int(avg), instrument=self.iid)   # whole paise: int() truncates
```

The shipped strategies do both; read `sma_cross.py` (two lines, `fast` and `slow`) and
`opening_range_breakout.py` (the range high and low, and a note on the entry, the stop and the exit).

## 2. The run config

`examples/runs/orb_reliance_minute.json` runs it on RELIANCE's minute bars for 2026-11-04 (the one day of minute
bars in the lab so far):

```json
"strategy": { "kind": "python", "ref": "stockify.strategies.opening_range_breakout:OpeningRangeBreakout",
              "params": { "symbol": "NSE:RELIANCE", "range_minutes": 15, "qty": 10, "short": true,
                          "exit_hhmm": "15:10" } },
"data": { "packs": [ { "spec": { "dataset": "bars", "interval": "minute",
          "instruments": ["NSE:RELIANCE"], "from": "2026-11-04", "to": "2026-11-04" } } ] },
"price_basis": "adjusted", "capital_paise": 10000000,
"fill_model": { "kind": "bar", "participation": "0.10", "slippage": { "kind": "bps", "x": "0.0005" } },
```

- `data.packs[].spec` says which recorded data to replay; the pack is built once from the lab and reused.
- `price_basis` must match the data: Kite bars are stored adjusted for splits and bonuses, so this is `adjusted`.
- `capital_paise` is the starting money: ₹1,00,000.
- `fill_model` `bar` fills an order on the next bar (A4 §4.1), taking at most 10 % of that bar's volume, with
  0.05 % slippage against you. `bar.volume` is in units (shares; for F&O, contracts × lot size) whatever the
  data source: NSE's daily F&O file counts contracts and is converted when the pack is built (A1 §5, D-79).
  Known data hole: 2023-12-28 and 2023-12-29 carry no sourced lot size, so an F&O `bhav_eod` pack over those two
  days fails to build; start such a pack on 2024-01-01 or later.
- The `x` of `bps` slippage is a fraction of the price, written as a decimal string: `"0.0005"` = 5 bp = 0.05 %.
  It is not a number of basis points: `"5"` is refused, as is anything outside `"0"` to `"0.1"` (0 to 1,000 bp)
  and anything that is not a string. `participation` is a fraction too (`"0.10"` = 10 %).

## 3. Run it from the command line

```
.\.venv\Scripts\python.exe -m stockify run --config examples\runs\orb_reliance_minute.json
```

```
NAV 100,000.00 -> 99,982.21; trades 1; costs 13.59; labels charges:unverified_rows, fill:bar, margin:flat(approx), specs:equity_tick_assumed
C:\Users\priya\AppData\Local\STOCKIFY\kite\lab\runs\orb_reliance_minute-ada883a0319e
```

The last line is the run folder. The run is also recorded in `lab.sqlite`, so it shows up in the `/tests` list.
Add `--no-report` to skip `report.md` and the chart.

## 4. Run it from the UI

Open `http://127.0.0.1:3000/tests`, choose **New test**, and fill in the form:

- **Strategy**: Opening range breakout; its parameters appear below it.
- **Market data**: minute bars; **Symbols** `NSE:RELIANCE`; **First day** and **Last day** `2026-11-04`.
- **How orders fill**, **Prices** (adjusted), **Starting money (₹)** and **Seed**.

**Start test** queues the run. The API runs it in a separate process, so a long run never blocks the page. The
run's page shows the money at the start and end, gain or loss, worst fall from a high, trades, costs, a chart of
money over time, where the costs went, and the details for reproducing the run. A failed run shows what went
wrong and the last lines of its log.

## 5. Read the result

On 2026-11-04 the strategy did this (from `orders.csv` and `fills.csv` in the run folder):

| IST | What happened |
|---|---|
| 09:15–09:29 | the opening range: the high and low of the first 15 minute bars |
| 14:44 | a bar closed above the range: **buy 10** (market) and a **sell stop at ₹1,296.00** |
| 14:45 | the buy filled at **₹1,315.56** on the next bar |
| 15:10 | the time exit: stop cancelled, **sell 10** (market) |
| 15:11 | the sell filled at **₹1,315.14** |

So it lost ₹4.20 on the trade (42 paise a share) and paid ₹13.59 in charges (brokerage, STT, exchange fees, GST).
That leaves ₹1,00,000 → ₹99,982.21, a loss of ₹17.79. One day and one trade prove the plumbing, not the
strategy: judge a strategy over many days.

The files in a run folder:

- `orders.csv`, `order_events.csv` and `fills.csv`: every order, every change to it, every fill and the
  evidence it filled on (`bar_open`: the next bar's open).
- `charges.csv`: the charges for each order, component by component, in paise.
- `positions_eod.csv` and `equity.csv`: what was held and what the account was worth at the end of each day.
- `metrics.json` and `report.md`: the summary numbers. `events_log.txt` holds the engine's and the strategy's
  log lines.
- `bundle.json`: everything the run used (data, calendar, charges, rules), so it can be re-run anywhere.

**Labels** say how far to trust a run. They are printed after the summary and shown on the run's page:

| Label | Meaning |
|---|---|
| `fill:bar` | fills come from bars, not from the order book: the price is plausible, the exact moment is approximate |
| `margin:flat(approx)` | margin is a flat percentage, not the exchange's full calculation (M4) |
| `charges:unverified_rows` | at least one charge rate in force was not yet checked against a real Kite contract note |
| `specs:equity_tick_assumed` | the stock had no recorded lot/tick spec for those dates; a 1-paisa tick was assumed (D-43) |
| `fill:approximate`, `eod_approx` | snapshot or end-of-day fills: rougher still (A4 §4) |
| `settlement_fallback` | an expiring contract had no official settlement price, so the last underlying price was used |
| `costs:scaled` | the run's config set `costs.scale`, so every charge was multiplied for a what-if test: its costs are not the real ones |

**What if costs were different?** Put `"costs": {"scale": "1.5"}` in a run config to pay one and a half times the real
charges (`"0"` for none, `"2"` for double; a decimal string from 0 to 10 with at most 4 decimals). It multiplies the
total cost: brokerage, STT, stamp duty, exchange and SEBI fees and flat fees are scaled, and GST is not scaled itself
because it follows its (scaled) base, so total charges come out at 1.5 times the real ones. A scale the rates cannot
hold exactly is refused rather than rounded. Leave it out (or `"1"`) for a real result; paper sessions refuse it.

## 6. Determinism and re-runs

The same bundle and the same engine build always give the same files, byte for byte (I-8). `determinism.json`
holds their SHA-256 hashes. To prove it for a finished run:

```
.\.venv\Scripts\python.exe -m stockify run --rerun orb_reliance_minute-ada883a0319e
```

```
determinism.json reproduced byte for byte (orb_reliance_minute-ada883a0319e -> orb_reliance_minute-ada883a0319e-rerun)
```

Exit code 0 means identical; 1 means something differs ("not reproduced", followed by a `differs:` line per file),
which is a bug worth reporting. Running the same config again also gives identical `determinism.json` (in a new
folder, `…-2`).

One expected difference: a run saved before D-64 has a `margin_daily.csv` without the `step` column. When that column
is the only difference (every other file byte for byte, and each row the same once the step is left out), the re-run
prints `reproduced (margin_daily.csv gained the step column, D-64)` and exits 0.

## 7. Port it to C++

Port a strategy once it is settled, for speed: a C++ strategy runs without Python. The port of this one is
`engine/strategies/orb_cpp.cpp`. It is a line-for-line translation:

- **Same hooks** on the `stocksim::Strategy` base class: `on_start`, `on_bar`, `on_timer` and so on, with the same
  `Context` methods (`ctx.resolve`, `ctx.subscribe_bars`, `ctx.place` with a `PlaceRequest`, `ctx.cancel`,
  `ctx.position`, `ctx.schedule_at`). Money is `Paise` (int64), times are `TimeNs`.
- **Params** come from the same config through `param_int`, `param_bool` and `param_string`. They read values the
  way Python's `int()` / `bool()` do, so `"qty": "10"` means the same thing in both.
- **Exact integer arithmetic**: `floor_div` / `ceil_div` for the stop's tick rounding, `checked_add` /
  `checked_mul` so an overflow is an error instead of a wrong number.
- **Register it** at the bottom, inside its namespace: `STOCKSIM_STRATEGY(OrbCpp, "orb_cpp")`. Add the file to the
  `stocksim_strategies` list in `engine/CMakeLists.txt` and rebuild with `.\run.ps1 -BuildEngine`.

**Prove the port is exact** before trusting it. `tests/stockify/test_engine_strategies.py`
(`test_opening_range_breakout_and_orb_cpp`) runs the Python strategy on the oracle and the C++ one on the engine
over 40 random minute-bar worlds, and requires identical run folders. Then on real data, with
`examples/runs/orb_reliance_minute_cpp.json` (the same config with `"strategy": { "kind": "cpp", "ref": "orb_cpp",
… }`):

```
.\.venv\Scripts\python.exe -m stockify run --config examples\runs\orb_reliance_minute_cpp.json --no-report
.\.venv\Scripts\python.exe -m stockify compare-artifacts orb_reliance_minute-ada883a0319e orb_reliance_minute_cpp-6133f45a5b44
```

```
NAV 100,000.00 -> 99,982.21; trades 1; costs 13.59; labels charges:unverified_rows, fill:bar, margin:flat(approx), specs:equity_tick_assumed
identical
```

A C++ strategy can also run with no Python at all, from the run's `bundle.json` (for benchmarks and checks):

```
engine\build\win-msvc-release\stocksim_run.exe --bundle $env:LOCALAPPDATA\STOCKIFY\kite\lab\runs\orb_reliance_minute_cpp-6133f45a5b44\bundle.json --out C:\temp\orb_cpp
```

```
stocksim_run: 3 orders, 2 fills in 1 ms -> C:\temp\orb_cpp
```

Its `determinism.json` equals the run folder's.
