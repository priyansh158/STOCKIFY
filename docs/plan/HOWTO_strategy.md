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
- A later bar that **closes above the range** buys `qty` shares (MIS, intraday) with a market order, and at once
  places a **stop**: a sell stop-market at the bottom of the range. With `short: true`, a close **below** the
  range sells short instead, with a buy stop at the top.
- At `exit_hhmm` (default 15:10) a timer cancels the stop and closes whatever is still open. If the strategy
  forgets, the broker's MIS square-off at the cut-off would do it, with a fee.
- At most one trade a day.

```python
from stockify import timeutil
from stockify.strategy import Strategy


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
        self.iid = ctx.resolve(p["symbol"])              # "NSE:RELIANCE" -> instrument id
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
        self.stop_id = None
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
        if self.traded or self.hi is None:
            return
        tick = ctx.spec(self.iid)["tick_paise"]
        if bar.close > self.hi:
            ctx.place(self.iid, "BUY", self.qty, "MIS", "MARKET", tag="orb_long")
            self.stop_id = ctx.place(self.iid, "SELL", self.qty, "MIS", "SL-M", trigger=self.lo // tick * tick,
                                     tag="orb_stop")
            self.traded = True
        elif self.short and bar.close < self.lo:
            ctx.place(self.iid, "SELL", self.qty, "MIS", "MARKET", tag="orb_short")
            self.stop_id = ctx.place(self.iid, "BUY", self.qty, "MIS", "SL-M", trigger=-(-self.hi // tick) * tick,
                                     tag="orb_stop")
            self.traded = True

    def on_timer(self, ctx, timer_id):
        if self.stop_id is not None:
            ctx.cancel(self.stop_id)
        pos = ctx.position(self.iid, "MIS")
        if pos and pos.qty:
            side = "SELL" if pos.qty > 0 else "BUY"
            ctx.place(self.iid, side, abs(pos.qty), "MIS", "MARKET", tag="orb_exit")
```

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
- To make your own, put the file in `stockify/strategies/` and name it in the config as
  `"stockify.strategies.<module>:<Class>"`. The `PARAMS` list is only a description, for the strategy catalogue
  and the `/tests` form (labels, types, defaults, help text); the strategy itself reads `ctx.params`.

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
  0.05 % slippage against you.

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
engine\build\win-msvc-release\stocksim_run.exe --bundle %LOCALAPPDATA%\STOCKIFY\kite\lab\runs\orb_reliance_minute_cpp-6133f45a5b44\bundle.json --out C:\temp\orb_cpp
```

```
stocksim_run: 3 orders, 2 fills in 1 ms -> C:\temp\orb_cpp
```

Its `determinism.json` equals the run folder's.
