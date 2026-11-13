# How to write and run an options strategy (M4)

This follows on from [HOWTO_strategy.md](HOWTO_strategy.md), which covers the basics: the `Strategy` hooks, money
in paise, the load check, run folders, re-runs and C++ ports. This page covers what is different for options:

- reading the option chain;
- the margin models (the staircase, flat and SPAN-lite);
- what happens at expiry;
- comparing strategies side by side;
- calibrating the margin model against Kite.

Every command below that works offline was run on 2026-11-08 and gave the output shown. The calibration command
needs a Kite login during market hours, so it is shown but was not run.

Everything is simulated. No order ever reaches a broker.

## 1. Reading the chain: `ctx.chain`

`ctx.chain(underlying, expiry)` gives every call and put of one underlying and expiry at the current moment. The
example is X's intraday short straddle, `stockify/strategies/short_straddle_stop.py`, which shares its mechanics
with the iron fly in `stockify/strategies/_option_legs.py`. In words:

- At `entry_hhmm` (09:30 IST), it picks the nearest expiry at least `min_days` away.
- It reads the chain and takes the at-the-money (ATM) strike.
- At the first moment both the ATM call and put have a fresh two-sided quote, it sells one lot of each.
- Each leg gets a stop: a buy stop `stop_pct` (30 %) above its sell price.
- At `exit_hhmm` (15:15 IST) it cancels the stops and buys back whatever is still open.

This is the part that reads the chain (from `_option_legs.py`):

```python
        view = ctx.chain(self.underlying, expiry)
        atm = view.atm_strike_paise
        if atm is None:
            return f"no forward for {self.underlying} {expiry}"
        strikes = sorted({r["strike_paise"] for r in view})
        i = strikes.index(atm)
        by = {(r["strike_paise"], r["kind"]): r for r in view}
        legs = [("SELL", by.get((atm, "CE"))), ("SELL", by.get((atm, "PE")))]
```

and this is the check that it never trades on a missing or old price:

```python
        missing = [side for side, r in legs if r is None or r["bid_paise"] is None or r["ask_paise"] is None
                   or r["stale"]]
        if missing:
            return "a leg has no fresh two-sided quote"
```

### What the view holds

The view is a list with one row per option, sorted by strike and then call (`CE`) before put (`PE`). It also has
three attributes:

| Attribute | Meaning |
|---|---|
| `forward_paise` | the price the market expects the index to be at on expiry; the IVs and Greeks are computed from it. `None` when there is no price to work from. |
| `forward_source` | where the forward came from: `future` (the same-expiry future's mid price), `synthetic` (worked out from call and put prices around the money), or `forward_from_index` (the index value itself, the roughest) |
| `atm_strike_paise` | the strike nearest the forward (the lower one on a tie); `None` when there is no forward |

Each row:

| Key | Meaning |
|---|---|
| `instrument_id`, `kind`, `strike_paise` | which option: pass `instrument_id` to `ctx.place` |
| `bid_paise`, `ask_paise`, `mid_paise`, `ltp_paise` | best bid, best ask, their average, last traded price; `None` when there is none |
| `stale` | `True` when the option's last quote is more than 5 seconds old (`stale_after_ms` in `pricing.json`) |
| `oi` | open interest from the last quote |
| `iv_bid`, `iv_ask`, `iv_mid` | implied volatility (Black-76) at the bid, the ask and the mid, as a fraction: `0.14` is 14 % a year |
| `iv_reason` | why `iv_mid` is `None`, when it is: `BELOW_INTRINSIC`, `ABOVE_FORWARD`, `ABOVE_STRIKE`, `BELOW_TICK`, `ABOVE_MAX_VOL`, `LOW_VEGA` |
| `delta`, `gamma`, `vega`, `theta` | the Greeks at `iv_mid`, per unit of the underlying, in rupees: `vega` is per 1.00 of volatility (divide by 100 for one point), `theta` is per year (divide by 365 for a day) |

Things to know:

- **Nothing is made up.** A row with no quote has `None` prices, no IV and no Greeks. A strategy that needs a
  price must check for `None` and `stale`, as the straddle does, and wait or skip.
- **The view is the same within one moment**: calling `ctx.chain` twice at the same time costs nothing extra.
- **On end-of-day data** (`bhav_eod` packs) there are no quotes, only daily closes, so the view has prices but no
  bids or asks. The end-of-day straddle, `eod_short_straddle.py`, picks its ATM strike from the index close instead.
- A plain list of rows still works everywhere a list did before M4: old strategies need no change.

### The run config

`examples/runs/short_straddle_nifty_recorded.json` runs the straddle on one recorded day:

```json
"strategy": { "kind": "python", "ref": "stockify.strategies.short_straddle_stop:ShortStraddleStop",
              "params": { "underlying": "NIFTY", "entry_hhmm": "09:30", "exit_hhmm": "15:15", "stop_pct": "0.3",
                          "lots": 1, "min_days": 0, "protection_bp": 5000 } },
"data": { "packs": [ { "spec": { "dataset": "ticks", "from": "2026-11-11", "to": "2026-11-11",
          "instruments": [ "NSE:NIFTY 50",
                           { "chain": { "underlying": "NIFTY", "expiry": "nearest", "strikes": 10 } } ] } } ] },
"fill_model": { "kind": "snapshot_quote" },
"margin": { "model": "spanlite" },
"mark": "mid",
```

- `chain` picks the options for the pack: the nearest expiry, 10 strikes either side of the money.
- `snapshot_quote` fills each order against the recorded bid and ask, the closest thing to a real order book.
- `mark: mid` values open options at the mid of bid and ask at the end of each day.

It needs recorded ticks for 11 Nov 2026, so it can only run once the recorder has recorded that day:

```
.\.venv\Scripts\python.exe -m stockify run --config examples\runs\short_straddle_nifty_recorded.json
```

To try another day, change `from` and `to` to a recorded trading day.

## 2. Margin: the staircase, flat or SPAN-lite

Margin is the money the broker blocks while a position is open. A run picks its model with `"margin": {"model":
"staircase"}` (the default), `"span_exact"`, `"spanlite"` or `"flat"`:

- **staircase** uses the best margin it can work out each day, per underlying, and says which one it used (below).
- **span_exact** uses NSE's own SPAN file and nothing else. If a contract is missing from the file, the run stops
  with an error instead of guessing.
- **spanlite** and **flat** are estimates, not NSE's own calculation, and are labelled as such.

### The staircase

SPAN is the exchange's margin method, and NSE publishes its risk file every day. The staircase tries the steps from
the top and takes the first one it can price. If a contract is missing, the whole underlying steps down to the first
step that can price every one of its legs (a contract missing from the SPAN file skips B1 and B2 together). It never
charges ₹0 for a missing contract. Each day's step is saved in the `step` column of `margin_daily.csv` and shown in
the UI with these words:

| Step | UI words | What it used |
|---|---|---|
| B1 | exact NSE margin | NSE's SPAN file and NSE's own exposure-margin (ELM) file for that contract |
| B2 | NSE margin, exposure from rules | NSE's SPAN file, with the exposure part from the dated rule, plus the expiry-day extra (X-9) |
| B3 | estimate (calibrated) | SPAN-lite with settings fitted to Kite's margins |
| B4 | estimate (not calibrated) | SPAN-lite with the placeholder settings |
| B5 | simple estimate | a fixed share of position value (flat) |

A run never looks ahead. A SPAN file counts only from the time NSE created it; during a day the run uses the previous
trading day's end-of-day file and then that day's intraday files as they appear, never that day's own end-of-day file.
A file older than one trading day is not used. The pre-trade margin check compares "before" and "after" at one common
step, so a change of step cannot make an order look free.

Against 405 stored Kite answers, B1 is off by a median of ₹14.17; B4 by ₹14,150 and B5 by ₹1,84,640.

### Checking a basket: `margin check`

`margin check` prices a basket offline from the archived NSE files. It writes nothing.

```
python -m stockify margin check --leg "SELL 65 NIFTY26N1724950CE" --leg "SELL 65 NIFTY26N1724950PE" --date 2026-11-09 --at 10:00
```

It prints the SPAN file in force at that time, the step, and the parts. For that basket:

```
NIFTY: step B1_span_nse_elm (exact SPAN + NSE's ELM file)
  SPAN                Rs 1,58,589.00
  exposure (ELM)      Rs 64,965.00
  total               Rs 2,23,554.00
```

`--date` defaults to the newest archived day and `--at` to 15:30 IST. Exit code 2 means a bad leg or unknown symbol;
1 means no SPAN file was in force at that time. Live prices move the exposure part a little.

### How good is each step: `margin scorecard`

`margin scorecard` prices every stored Kite basket-margin answer with every step and prints the median and 90th
percentile error, overall and per basket type. It also says when a less exact step beats a more exact one (an
inversion), notes that B3 is not scored until a calibrated row is adopted (section 6), and warns that the expiry-day
extra has not been tested against NSE.

```
python -m stockify margin scorecard
```

Its first rows read: B1 priced 405, median ₹14.17, 90th percentile ₹40.90, median relative error 0.01 % (the 90th
percentile relative error is 0.04 %).

**Flat** (`margin_params.json` → `flat`) is a fixed share of each position's value:

- 12 % of the contract value (the index price × quantity) for each future and short option on an index; 20 % for
  stocks;
- nothing for a long option: its premium is already paid;
- on its expiry day, a short index option adds 2 % of the index price × quantity (the exchange's expiry-day
  extra margin, X-9);
- in the expiry week of a stock contract, the broker's delivery-margin schedule (Z-4, below).

It is simple and predictable, but it charges a hedged position (an iron fly) almost the same as a naked one.

**SPAN-lite** works the way the exchange's SPAN does, in a smaller form. For each underlying it asks "how much could
this whole position lose in a bad day?":

- It moves the price up and down by up to a set range (PSR) and the volatility up and down by a set share (VSR), 14
  scenarios in all, plus two extreme moves of 3 × PSR counted at 35 %. Futures can have their own, separate range
  (`psr_futures_pct`, D-62): Kite charges futures a bigger share of their value than options imply.
- Settings with `net_option_value` (D-63, as SPAN does) take the options' current value off that loss: options
  you bought count in your favour, options you sold count against you.
- Options you only bought need no SPAN-lite margin: you paid their premium in cash, and that is all they can lose.
- It revalues every leg in every scenario (Black-76, at each option's own volatility) and takes the worst loss.
- A floor for short options: at least `somc_per_lot` per lot sold.
- Plus exposure margin: 2 % of the contract value for index futures and short options (3.5 % for stocks), and the
  2 % expiry-day extra.
- Each part is rounded up to the next whole rupee. The breakdown is saved per day in `margin_daily.csv`.

Because the loss is worked out for the whole position, the long wings of an iron fly cut its margin, as they do at
the broker. **Today's SPAN-lite settings are placeholders** until the calibration in section 5 is done.

For one day of the comparison in section 4 (2026-10-05, one short NIFTY option), SPAN-lite's
`margin_daily.csv` reads: worst scenario loss ₹92,130, short-option floor ₹2,000, exposure margin ₹29,323, total
₹1,21,453.

### What the labels mean

Labels are printed after a run's summary and shown on its page in `/tests`. They say how far to trust the numbers.

| Label | Meaning |
|---|---|
| `margin:flat(approx)` | margin is a fixed share of each position's value, not the exchange's SPAN calculation |
| `margin:spanlite(approx)` | margin is a SPAN-like estimate (price and volatility scenarios plus exposure margin), not the exchange's own figures |
| `margin:staircase` | margin from the staircase: NSE's own SPAN file where it can price a position, else the next step down; the step labels below say which steps were used |
| `margin:span_exact` | margin from NSE's SPAN file only; the run stops if a position is missing from it |
| `margin:<step>`, `margin_steps:<step>=<days>` | that staircase step priced at least one check, and on how many run days (steps B1–B5 above) |
| `margin_span:no_derivatives` | the run traded no futures or options, so no NSE margin file was read |
| `margin_span:no_section` | the bundle had no SPAN section at all (a hand-made bundle), so every check used a lower step |
| `margin_span:skipped_files` | at least one archived SPAN file could not be used (unreadable, misnamed, or an implausible created time) and was skipped |
| `margin_elm:skipped_files` | at least one archived ELM file could not be read, so those days used the dated exposure rule (B2) |
| `margin_spanlite:placeholder` | the SPAN-lite settings are placeholders, not yet calibrated against Kite, so margin amounts are rough |
| `margin_spanlite:calibration-<date>` | the SPAN-lite settings fitted to Kite's margins on that date (section 6) |
| `margin_spanlite:none` | the run chose SPAN-lite but had no futures or options, so no SPAN-lite settings were used |
| `margin_spanlite:iv_fallback` | for at least one option, the margin had to assume a volatility because none could be read from its quotes (its last one today, else the ATM option's, else `default_iv`: 15 % for indices, 30 % for stocks) |
| `delivery_margin_approx` | the extra margin brokers block before stock F&O delivery was applied approximately |
| `settlement_fallback` | NSE had not published a final settlement price for an expiry, so the last market price was used |
| `short_delivery` | a short stock F&O position ended in delivering shares the account did not hold; a real account would face an auction penalty |
| `eod_approx`, `fill:eod_close` | orders fill at the next day's close from NSE's end-of-day files, so moves during the day are not seen |
| `fill:snapshot_quote` | orders fill against the recorded bid and ask |
| `charges:unverified_rows` | some charge rates have not been checked against a real Kite contract note yet |

The other labels (`fill:bar`, `fill:approximate`, `specs:equity_tick_assumed`) are in HOWTO_strategy.md §5.

A run on end-of-day files with SPAN-lite always carries `margin_spanlite:iv_fallback`: daily files have no bids or
asks to read a volatility from, so every option uses `default_iv`. Use SPAN-lite on recorded chains for margin you
can rely on.

## 3. Expiry

What happens to an option or future still open at the end of its last trading day depends on the underlying
(`contract_rules.json` → `settlement`).

**Index contracts (NIFTY) settle in cash** at NSE's final settlement price (FSP), published at 16:00 IST:

- A future, or an option in the money, pays or receives the difference in cash; an option out of the money
  expires worth nothing.
- An option bought and still in the money at expiry pays STT on its intrinsic value: 0.15 % from 2026-04-01
  (0.125 % before), Z-2.
- **The expiry fee**: every option that leaves the books at expiry (exercised, assigned or expired worthless) is
  charged ₹20 brokerage plus 18 % GST, ₹23.60 per contract (D-59; still to be confirmed from a real contract note,
  so it is `verified: false`).

You can see both in a run folder. From the October comparison run (section 4), `events_log.txt`:

```
1791282600000000000 INFO settled 707289 NRML at FSP 2277610 (settle)
```

(the option settled against a NIFTY FSP of ₹22,776.10), and `charges.csv`:

```
row_no,kind,order_id,instrument_id,trade_date,turnover_paise,brokerage,stt,exchange,sebi,stamp,gst,dp,other,total
2,settlement,0,707289,2026-10-06,0,2000,0,0,0,0,360,0,0,2360
```

(₹20.00 brokerage and ₹3.60 GST: the ₹23.60 expiry fee.)

If the FSP is not in the lab, the last underlying price is used and the run is labelled `settlement_fallback`.

**Stock contracts settle by delivery of the shares** (Z-4):

- A future, or an option in the money, becomes a holding of the stock (CNC). A long call or short put buys the
  shares; a long put or short call sells them. Futures are delivered at the settlement price, options at the
  strike. The cash moves on the delivery day.
- Charges: STT of 0.1 % of the contract value on both sides, plus the exercise STT above; brokerage of 0.25 % of
  the delivery value, or 0.1 % for positions that cancel out in the same stock. Equal buy and sell quantities in
  the same stock and expiry deliver nothing.
- The broker blocks extra margin in the run-up (`broker_policy.json` → `delivery_margin_schedule`):
  - long in-the-money options: 10 %, 25 % and 45 % of the usual margin four, three and two trading days before
    expiry (labelled `delivery_margin_approx`), then 25 % of the contract value the day before and 50 % on expiry
    day;
  - long out-of-the-money options within 1 % of the stock price: 25 % of the contract value on expiry day;
  - futures and short options on expiry day: the smaller of 50 % of the contract value and 1.5 × their usual margin.
- New out-of-the-money long stock options cannot be bought on the Monday and Tuesday of expiry week.
- Delivering shares the account does not hold is labelled `short_delivery`: at the exchange it would end in an
  auction and a penalty.

## 4. Comparing strategies side by side

`python -m stockify tools compare-strategies --config FILE` runs two or more configs and writes one report:
money at the start and end, charges, trades, the most margin tied up at any moment, the worst fall, the caveats
of each run and the gain or loss day by day.

`examples/runs/eod_straddle_flat_vs_spanlite.json` runs the end-of-day NIFTY straddle on NSE's files for October
2026 twice: once with flat margin and once with SPAN-lite, so the only difference is the margin model. It works
offline:

```
.\.venv\Scripts\python.exe -m stockify tools compare-strategies --config examples\runs\eod_straddle_flat_vs_spanlite.json
```

```
C:\Users\priya\AppData\Local\STOCKIFY\kite\lab\runs\compare-eod_straddle_flat_vs_spanlite\compare.md
```

The report, cut down:

```
## End-of-day straddle, flat margin
- Money: ₹10,00,000.00 → ₹10,12,051.84, so it made ₹12,051.84
- Charges paid: ₹535.41; trades: 6
- Margin: at most ₹3,59,629.00 tied up at any moment

## End-of-day straddle, SPAN-lite margin
- Money: ₹10,00,000.00 → ₹10,12,051.84, so it made ₹12,051.84
- Charges paid: ₹535.41; trades: 6
- Margin: at most ₹1,54,342.00 tied up at any moment
- Caveats: …; some option volatilities for margin had to be filled in (no usable quote); SPAN-lite margin
  settings are placeholders, not yet calibrated against Kite
```

Same trades, same money: the margin model only changes how much is blocked (and so whether an order would be
refused for lack of funds). Here SPAN-lite blocks less than half of what flat does, with placeholder settings and
assumed volatilities, so do not read much into the exact figure yet.

The main comparison is `examples/runs/straddle_vs_ironfly_recorded.json`. It runs the same days three ways:

- as the end-of-day straddle on NSE's files;
- as the intraday straddle with stops on recorded chains;
- as the intraday iron fly on recorded chains.

Give the runs the roles `eod`, `straddle` and `iron_fly`, as that file does, and the report adds two comparisons:

- end-of-day against intraday, which shows how much end-of-day data distorts option results;
- the iron fly's margin against the straddle's.

For the intraday runs, "margin at most" is the peak during the day, read after every fill. It needs five recorded
days, set to 9–16 Nov 2026: five trading days, since 10 Nov is a holiday. Run it once those days are recorded.

## 5. The calibration session (the owner, during market hours)

SPAN-lite's settings (PSR, VSR and the short-option floor) are fitted to Kite's own margin figures:

- The tool reads the live NIFTY chain.
- It makes up a few hundred sample positions: single options, straddles, strangles, spreads, iron flies and condors,
  futures, and futures hedged with options.
- It asks Kite what margin each one needs. It uses Kite's margin calculator (`basket_order_margins`), which places
  no order.
- It finds the settings that bring SPAN-lite closest to Kite's answers.

It only reads quotes and margins: it never places an order.

**When.** It needs live prices, so run it during the trading session on a normal day, not an expiry day. Expiring
options and the expiry-day extra margin would skew the fit. `python -m stockify calendar` shows the sessions and
the Kite login window in your time zone:

```
.\.venv\Scripts\python.exe -m stockify calendar
```

```
NSE F&O sessions from 2026-11-09 (IST trade dates; your time zone America/Los_Angeles, PST)
IST date        session IST   session, your time             Kite login window, your time
2026-11-09 Mon  09:00-15:40   Sun 08 Nov 19:30 -> Mon 02:10  Sun 08 Nov 16:30 - 19:30
2026-11-10 Tue  closed: Diwali-Balipratipada
2026-11-11 Wed  09:00-15:40   Tue 10 Nov 19:30 -> Wed 02:10  Tue 10 Nov 16:30 - 19:30
2026-11-12 Thu  09:00-15:40   Wed 11 Nov 19:30 -> Thu 02:10  Wed 11 Nov 16:30 - 19:30
…
```

With 10 Nov a holiday, NIFTY's weekly expiry moves forward to Mon 9 Nov, so the first good day is
**Wed 11 Nov IST**. Run it between 10:00 and 15:00 IST, which is Tue 10 Nov 20:30 to Wed 01:30 PST. That avoids
the opening and closing minutes.

**On an expiry day, add `--skip-expiring`.** It leaves out every contract whose last trading day is today, using
NSE's moved date when a holiday moved it, and prices the next two option expiries and the next future instead. The
expiry-day extra margin only applies to contracts expiring that day, so those positions price as on any other day.
The report's header lists the expiries priced and the ones left out, and so does each sample saved in `lab.sqlite`:

```
.\.venv\Scripts\python.exe -m stockify margin calibrate --underlying NIFTY --samples 200 --skip-expiring
```

**What to do.**

1. Log in to Kite in the login window (`http://127.0.0.1:3000/portfolio` → **Login to Kite**), as for recording.
2. During the session, from the repo folder:

   ```
   .\.venv\Scripts\python.exe -m stockify margin calibrate --underlying NIFTY --samples 200
   ```

   It makes 2 quote requests (at most one a second) and about 200 margin requests (at most ten a second): under a
   minute of Kite time; the fit itself is quick. Without a fresh login it stops before calling Kite, with exit
   code 2.
3. It prints the report's path, `%LOCALAPPDATA%\STOCKIFY\kite\lab\margin_calibration\NIFTY-<date>.md`, and says
   that the fitted settings were added to `stockify\config\margin_params.json` under `spanlite_pending`.

   Every sample and Kite's answer are also kept in `lab.sqlite` (`margin_calibration`).

Running it again the same day replaces that day's pending row; rows from other days are kept.

The fit compares SPAN-lite with Kite's margin **without** the premium of bought options. Kite's "total" also counts
that premium, because the broker takes it upfront. In a run that premium is paid as cash when the order fills, so
it is not margin.

**Fitting again without Kite.** To refit a day that is already stored, for example after the model changes, run:

```
.\.venv\Scripts\python.exe -m stockify margin calibrate --underlying NIFTY --refit --date 2026-11-09
```

It makes no Kite call. It rebuilds the market from the stored samples, fits again, writes
`NIFTY-<date>-refit.md` beside the live report, and replaces that day's pending row.

## 6. Reviewing and adopting the fitted settings

The fitted settings are **not used** until you move them into place. No margin model reads `spanlite_pending`.

1. **Read the report.** For each kind of position it gives the typical and the worst-10 % error against Kite for
   three models: flat, SPAN-lite as it is now, and SPAN-lite with the fitted settings. Adopt the fit if it is clearly
   closer to Kite for the positions you trade (short straddles and iron flies above all). If some kind is still far
   off, keep the fit pending and say so: exact SPAN (M4.10) exists for that case.
2. **Move the row.** In `stockify/config/margin_params.json`, cut the row out of `spanlite_pending` and paste it
   into the `spanlite` list. Delete its `"pending_review": true` line. Leave `"verified": false`, because these are
   fitted numbers, not NSE's. The row looks like this:

   ```json
   {
     "applies": { "underlying": "NIFTY" },
     "effective_from": "2026-11-11",
     "psr_pct": "…", "psr_futures_pct": "…", "vsr_pct": "…", "extreme_multiple": 3, "extreme_weight": "0.35",
     "somc_per_lot_paise": …, "elm_pct": "0.02", "default_iv": "0.15",
     "calibration_id": "calibration-2026-11-11",
     "source": "calibration 2026-11-11", "samples": 200, "mean_relative_error": "…",
     "verified": false
   }
   ```

3. **Decide from when it applies.** A row naming the underlying (`"underlying": "NIFTY"`) takes over from the
   index placeholder for NIFTY, but only for run dates on or after its `effective_from`. Older dates keep the
   placeholder. To use the fit for back-tests of earlier days as well, move `effective_from` back, and say so in
   `source`, for example `"calibration 2026-11-11, applied from 2025-09-01"`. Two rows for the same underlying
   cannot share an `effective_from`.
4. **Check the file.**

   ```
   .\.venv\Scripts\python.exe -m stockify config
   ```

   ```
   config OK
   ```

5. **Commit the change.** Old runs stay reproducible either way: each run's `bundle.json` keeps the settings it used.

New SPAN-lite runs on NIFTY from that date then carry `margin_spanlite:calibration-2026-11-11`, and the run's page
no longer warns that the settings are uncalibrated. The label names only the settings a run could use, for its
underlyings and its days:

- a run that starts before the row's date and ends after it carries `margin_spanlite:calibration-2026-11-11+placeholder`;
- a run on BANKNIFTY, or on any other underlying without its own row, still carries `margin_spanlite:placeholder`.

Re-run the calibration about once a month, and after any NSE margin circular. Each run adds a new pending row with
its own date, and the history stays in `lab.sqlite`.
