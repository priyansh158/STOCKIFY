# Candlestick charts with trade reasons: design

Date: 2026-11-12. Owner-approved direction (chat, 2026-11-12). Builds into M6 (branch `stockify/m6-research`).
Calls marked **[controller's call]** were not discussed with the owner; everything else was.

## Intent

The owner wants to look at any backtest or paper session and see, on a price chart, **exactly when the strategy
bought and sold and why**, in plain words, without reading code or logs. "It should clearly indicate when bought
and give us a comment why the strategy chose to buy at that moment, and the same for the sell." The owner said the
app's look needs to be "way better", and functionality comes first; these charts are both.

## Goals

1. **Candlestick chart** of the bars the strategy actually traded on (minute or daily), with volume, zoom, pan and a
   crosshair, built with **TradingView Lightweight Charts, pinned exactly at `5.3.0`** (D-110: the owner moved the pin from 5.2.1 to 5.3.0 on 2026-11-13) (owner chose the library;
   supply-chain audit `.superpowers/audits/lightweight-charts-supply-chain.md`: OK with conditions).
2. **Buy and sell markers** on the bar where each fill happened: buy below the bar, sell above it, sized and coloured
   from the design tokens.
3. **A plain-English note per order**, written by the strategy at the moment it decides, with its numbers filled in,
   e.g. "Bought 50 RELIANCE: price 2,418.40 broke above the morning's high of 2,412.50" and
   "Sold 50: stop-loss at 2,401.00 hit". Hover or click a marker → a card with the note, side, quantity, fill price,
   costs of that fill, and, for a closing fill, the trade's profit or loss.
4. **The strategy's own lines on the chart** (indicator overlays): values the strategy records while it runs (the
   opening-range high and low, moving averages), drawn as lines over the candles, so the reason can be *seen*.
5. **Everywhere the owner chose:** the run page, the paper session page (live), the walk-forward page (stitched
   out-of-sample trades, test windows shaded), and the sweep page (the chart of the selected trial).

## Non-goals

- No drawing tools, no user-placed lines, no saved chart layouts.
- No chart of an arbitrary symbol outside a run or session (that is a market-data viewer, not this).
- No new data source: the chart only shows data STOCKIFY already holds.
- No change to how fills are simulated.

## Anti-goals (a failure even if it "works")

- **A chart showing prices the strategy did not see.** The run chart reads the bars from the run's own event pack,
  never a later re-fetch (Kite adjusts history retroactively, K-6, I-5). A run on `adjusted` basis shows adjusted
  bars; a `raw` run shows raw ones; never mixed.
- **A note that lies.** A note is the strategy's own sentence from decision time; the UI never invents one. A strategy
  that gives none shows its short `tag` (e.g. `orb_stop`), labelled as a code, and a plain fallback such as
  "Sold 50 (no note from the strategy)".
- **Breaking determinism or parity.** Notes and plotted values are part of a run's outputs: identical across repeated
  runs and identical between the oracle (Python) and the engine (C++) for the same strategy.
- Implying real money: the session chart says paper on its face.

## Constraints

- Windows only; no new package except `lightweight-charts@5.3.0` (exact pin, no caret) and its one dependency
  `fancy-canvas@2.1.0`; a D-row records the owner's approval and the audit. Licence: Apache-2.0 with TradingView
  attribution: every chart shows the attribution link (`attributionLogo: true`) and the NOTICE line appears once on
  an "About / credits" spot (**[controller's call]**: the app footer).
- The library touches the DOM and canvas: one client-only component (`"use client"`, chart created in `useEffect`,
  disposed on unmount); pages stay server components and pass data down.
- Design: `web/design.md` tokens only (candle up/down, marker, line colours come from tokens; dark theme), Hallmark
  app-screen flow, desktop ≥ 1024 px. Money ₹ via `format.ts`.
- Engine rules: deterministic, no wall clock, C++ never touches SQLite or the network.
- Money int64 paise, time UTC ns, IST only at display.

## How it works (decided)

### 1. A note on every order (strategy side)

- `ctx.place(...)` gains an optional keyword **`note: str = ""`** (Python strategies; the oracle context and the
  engine's Python bridge) and the C++ strategy API gets the same optional field. `tag` stays as it is (a short code).
- Stored as a new last column **`note`** in `orders.csv` (A1 §6 table, blockwatch-tied) and in the order records the
  runner keeps; `""` when none, never null. Max **200 characters** after trimming; longer is cut at 200 with "…"
  **[controller's call]**; control characters are replaced by spaces.
- Number formatting inside notes: a helper `fmt_price(paise)` / `fmt_qty(units)` available to strategies (Python and
  C++), giving "2,418.40" in Indian grouping, so notes are deterministic and identical on both runners.
- The six shipped strategies (`buy_and_hold`, `sma_cross`, `opening_range_breakout`, `eod_short_straddle`,
  `short_straddle_stop`, the option-legs helper) and the C++ `orb_cpp` / `sma_cross_cpp` get real notes with their
  numbers (entry trigger, stop level, time exit, flip).
- System orders (auto square-off, `AUTO_SQOFF`, expiry settlement) get engine-written notes in plain words
  ("Closed by the broker's 15:20 auto square-off").
- `HOWTO_strategy.md` documents `note=` with two examples.

### 2. Plotted values (indicator overlays)

- `ctx.plot(name: str, value: int | float, *, kind: "price" | "level" = "price")` records one value per bar for a named
  series; `price` values are paise (drawn on the price scale), `level` values are plain numbers (drawn on their own
  scale under the candles, e.g. RSI) **[controller's call]**.
- Written to a new run artifact **`plots.csv`** (`t_ns,instrument_id,name,kind,value`), deterministic order; at most
  **8 series** and **200,000 points** per run, beyond which further `plot` calls are ignored and the run's
  `manifest.json` records `plots_truncated: true` **[controller's call]**.
- Both runners write identical `plots.csv`; the parity tests compare it like the other artifacts.
- `opening_range_breakout` plots its range high and low; `sma_cross` its two averages.

### 3. API (read-only, loopback, sync)

- `GET /api/tests/{run_id}/chart?instrument=&from_ns=&to_ns=` → `{bars: [{t, o, h, l, c, v}], markers: [...],
  plots: [{name, kind, points}], basis, interval_s, truncated}`. Bars are read from the run's event pack (the exact
  bars it saw); for tick-driven runs, bars are built from the pack's snapshots at the strategy's bar interval (or 1 minute; a caller's `interval_s` rebuilds them at that interval and the chart says so: D-112)
  **[controller's call]**. Without `from/to` the whole run; the response is capped at **50,000 bars**: the client
  asks for a range when the run is longer (the chart loads more as you pan) **[controller's call]**.
- A marker = one fill: `{t, side, qty, price_paise, order_id, note, tag, charges_paise, trade_pnl_paise | null}`.
- `GET /api/trade/sessions/{id}/chart?instrument=` → today's bars from the recording plus the session's paper fills;
  the page polls it every **5 s** while the market is open **[controller's call]** (no new WebSocket).
- `GET /api/research/walkforward/{job_id}/chart` → the stitched out-of-sample fills over the windows' bars, with
  window boundaries; the sweep page reuses the run endpoint for the selected trial.
- DTOs appended to `server/schemas.py` and `web/src/lib/types.ts` (blockwatch `dto-fields`).

### 4. Web

- One client component `web/src/components/PriceChart.tsx` (candles + volume + markers + plot lines + a
  marker card), dynamically imported with SSR off; an instrument picker when a run trades more than one.
- Placed on: run page (above the trades table; clicking a trade row scrolls the chart to it), session page (live),
  walk-forward page (windows shaded), sweep page (selected trial).
- Every region has loading, empty ("this run made no trades" still shows the candles), error and full states.
- A visible text fallback for accessibility: the trades table already lists every fill with its note.

## Settled after the builder check (all **[controller's call]**)

1. **Costs and trade P&L on a marker.** Costs come from `charges.csv` (rows of `kind = order` for that `order_id`; a
   multi-fill order's charges are shown once, on its last fill). Trade P&L reuses the FIFO matching that
   `oracle.metrics.compute` already uses to count trades (`metrics.py` ~99–137): that loop is extracted into a public
   `metrics.fifo_trades(fills, closes)` that both `compute` and the chart API call (shared helper, not a copy;
   `compute` unchanged byte for byte). No new artifact.
2. **What a trade is.** A trade is one FIFO-matched closing chunk, exactly what `metrics.json` counts: a partial exit
   is a trade for the closed quantity; a flip (long → short in one order) closes the old position (a trade with P&L)
   and opens a new one; expiry settlements close trades too (D-45). The card of a closing fill lists each chunk it
   closed with its P&L.
3. **Note propagation.** Child and sliced orders inherit their parent's note when they have none; each fill of a
   multi-fill order repeats its order's note. The note is part of the order record, so it is carried in the
   cross-day state (`final_orders`, `sod_state` orders, A1 §6.3–6.4), defaulting to `""` when an older state has none
   (D-row). **System-order notes** live in one file, `stockify/config/system_notes.json` (`AUTO_SQOFF`, settlement,
   and so on), which the bundle passes to the engine, so both runners write the same words from one source.
4. **`plot` semantics.** A call stamps the engine's current event time and may be made from any handler; a repeat
   call for the same series at the same `t_ns` replaces the earlier one (last wins). Values are **int64 only**:
   `price` in paise, `level` in units of 1/10,000 (so 54.3217 is `543217`), so both runners write byte-identical
   files. `plots.csv` is a **deterministic artifact (✔)**, added to the A1 §6 block and to the parity and golden
   comparisons. Caps (8 series, 200,000 points) apply **per run**; on hitting a cap, later calls are dropped and one
   deterministic line goes to `events_log.txt` ("plot limit reached: …"); `manifest.json` is not used for it.
5. **Where chart bars come from.** For a bar dataset: the `BAR` events in the run's own pack, exactly what the
   strategy received. For a tick dataset: display bars built from the pack's snapshots by the **same** bucketing
   function the runners use for `BAR_BUILT` (A4 §2.2), called as a shared helper at the strategy's bar interval (its `interval_s` param, else that param's default in
   its schema, read from the run's bundle: D-112) or 1 minute when the strategy has none; never a re-fetch, never `bars.sqlite`. A run with several instruments has an instrument picker (F&O legs
   included, each showing its own contract's bars); a daily-only run shows daily candles. A run whose pack is gone
   (pruned) shows the markers on an empty time axis with the line "the price data for this run is no longer on disk".
6. **Old runs and fixtures.** Readers accept an `orders.csv` without a `note` column (all notes `""`). Adding the
   column changes deterministic hashes: the golden and parity fixtures are regenerated in the same commit, with a D-row
   saying so; old run folders are never rewritten.
7. **Walk-forward chart.** Each window's test run is a real run (role `oos`) whose id is in the job's `state_json`
   (M6 Task 20); the endpoint reads those runs' packs and fills. All windows use the same base config, so the same
   instruments.
8. **Paper sessions.** A session's input is the live recording, so "the bars it saw" are built from that day's
   recorded snapshots with the same shared bucketing function; its orders and notes come from the session day's own
   order records (the same `note` field). The chart shows one session day at a time (default today) with a day
   picker for earlier days.
9. **Long runs.** `GET …/chart` takes `from_ns`/`to_ns`; a response holds at most 50,000 bars and returns
   `truncated: true` with `next_from_ns`. The client opens on the window around the first trade (or the last
   5,000 bars) and fetches neighbouring ranges as you pan. This is the contract the first version ships with.

## References (owner pointed at Mathieu2301/TradingView-API, 2026-11-12)

That project is an unofficial client of TradingView's servers (ISC); it is **not** used, installed or called: it is
a different data source (anti-goal: the chart shows only what the strategy saw), it needs TradingView session
cookies (a new secret), and TradingView's terms restrict automated access. What it informs:

- **Data shapes the client needs no reshaping for.** The chart API returns bars as `{time, open, high, low, close,
  volume}` with `time` in UTC seconds (Lightweight Charts' `CandlestickData`; prices sent as rupees in decimal strings
  converted from paise at the edge, never floats in storage), and markers in the shape of Lightweight Charts'
  `SeriesMarker` (`time, position: belowBar | aboveBar, shape: arrowUp | arrowDown, color, text`) plus our own fields
  (`note`, `order_id`, costs, trade P&L) for the card.
- **Indicator output as named series per bar** (a study's plots): the same idea as our `plots.csv` series.
- **A trade list in the style of a strategy report** (entry and exit pairs with time, price, quantity, P&L and running
  total, and the signal's name): the run page's trades table follows that layout, with the strategy's `note` in the
  signal column, so the table and the chart tell the same story.

## Left to the builder

- Exact marker shapes and sizes within the tokens; card layout; whether the volume pane is on by default.
- How the client pages through long runs (range requests while panning).
- The internal shape of the plot buffer in the engine.
- Where in the M6 task order these tasks go, subject to: the engine change (§1–§2) shares the one engine rebuild and
  c-review with M6 Task 14 if possible; the web parts come after Task 29 (web foundation).

## Skills the work uses (owner rule)

modern-cpp while writing the C++ parts and c-review after; property-based-testing method (note sanitising,
formatting round-trips, plot caps); differential-review over the strategy API change; sharp-edges and
insecure-defaults for the new endpoints and the `note`/`plot` API; dimensional-analysis for paise/units in markers;
Hallmark + design:design-critique, design:accessibility-review, design:ux-copy (the notes of the shipped strategies
are copy, too); dataviz for the chart; supply-chain-risk-auditor already run for the package; blockwatch for the DTO and
A1 ties; no-agentic-coauthor on every commit.
