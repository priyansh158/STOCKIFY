# Holdings desk: live charts, option chains and exit suggestions for the stocks you hold

Date: 2026-11-17. Owner-approved approach: A (nightly backtests, live signals, chain polled once a minute).

## Intent

The owner swing-trades the stocks in their real Kite account and wants help deciding when and how to get out.
For each holding: a chart that keeps moving during the session, that stock's option chain, the well-known swing
strategies run on it, and a plain exit thesis built from what those tests say, with option ideas (hedges and
plain option trades) where the stock's movement makes one worth considering. Everything is a suggestion; the
owner decides and places any trade themselves in Kite.

## Goals

1. Only the stocks in the owner's Kite holdings (the Portfolio page's list). Nothing else is analysed.
2. Per holding, a page with:
   - a candlestick chart (daily, plus intraday for today) that updates live during the session, with the
     strategies' levels drawn on it (stops, bands, breakout lines);
   - the option chain (F&O stocks only), refreshed about once a minute in the session;
   - a table of the strategies: what each says now (hold / exit / re-enter), at what level, and how well it has
     done on this stock historically;
   - the exit thesis: one verdict, the levels that would change it, and the reasons in plain sentences;
   - option ideas, each priced from the live chain, with cost, max loss, max gain, breakeven and margin.
3. The Portfolio list shows each holding's verdict at a glance and links to its page.
4. The well-known swing strategies exist as ordinary STOCKIFY strategies, usable on the Tests page like the
   existing ones (owner: "build the strategies as well").

## Non-goals

- No orders of any kind (platform invariant: the Kite client is read-only). No "place this" buttons.
- No new tick recording: the recorder, its universe and its connection are untouched (approach A).
- No stocks outside the holdings, no screener, no watchlist.
- Not a forecast: the page reports what tested rules say, never a predicted price.

## Anti-goals (would make it a failure even if it works)

- A confident verdict that hides how weak the evidence is. Every verdict shows the evidence behind it, and a
  strategy that has not beaten buy-and-hold on this stock out of sample carries little weight and says so.
- Option ideas the owner can't actually do: wrong lot size, a covered call on fewer shares than a lot, a
  strike with no quotes, a margin they don't have.
- Jargon. Plain names everywhere ("RSI Pullback", "protective put"), each with one plain sentence.
- Slowing or endangering the recorder.

## Constraints (from the project)

- Windows only; desktop UI with Hallmark; ₹ via Intl en-IN; money int64 paise, time UTC ns, IST at the edges.
- Read-only Kite (`stockify/kite_readonly.py`: quote, ltp, ohlc, historical, instruments, margins).
  Quote calls take up to 500 instruments; keep well under Kite's rate limits.
- No new packages without the owner's word: indicators are written in-house (stdlib, `stockify.numerics`).
- Strategies follow the existing contract (`stockify.strategy.Strategy`, `PARAMS`, `DISPLAY_NAME`,
  `DESCRIPTION`) and the deterministic engine; the backtests use the existing run machinery and bar cache.
- Options pricing uses the existing `stockify.fno.black76`; margins use the existing margin path
  (`basket_order_margins` / live_margin, the NSE SPAN files as fallback).
- Lot sizes and F&O eligibility come from the dated refdata (NSE fo bhavcopy / instruments), never hard-coded.
- Tests: stdlib unittest, offline, the existing sandboxing rules.

## Approaches considered

- **A. Nightly backtests, live signals, chain polled once a minute (picked).** Backtests on years of daily
  candles run once a night; each strategy's current signal is recomputed from the live price through the
  session; the chain comes from Kite quotes about once a minute. Live enough for swing decisions, and it leaves
  the recorder alone.
- B. Everything on the tick feed (each holding's chain subscribed and recorded). Freshest, builds an archive,
  but ties a new feature to the recorder, works only while it runs, and multiplies daily data.
- C. After-the-close report only. Simplest; fails "real time during the session".

## The strategies (new, each an ordinary strategy file in `stockify/strategies/`)

Long-only on the stock (CNC), daily bars by default, the usual defaults for each, all parameters editable on the
Tests page:

| Name | Rule (defaults) | Its exit level |
|---|---|---|
| RSI Pullback | buy when RSI(14) crosses up through 30, sell when it crosses down through 70 | the RSI 70 price |
| MACD Crossover | in while MACD(12,26) is above its 9-day signal line | the price where MACD would cross |
| Bollinger Reversion | buy a close below the lower band (20, 2σ), sell at the middle band | the middle band |
| Donchian Breakout (Turtle) | buy a 20-day high, sell a 10-day low | the 10-day low |
| Supertrend | in while the price is above Supertrend(10, 3) | the Supertrend line |
| EMA Trend | in while EMA(20) is above EMA(50) | the EMA(50) |
| Chandelier Exit | trailing stop at the highest high of 22 days minus 3×ATR(22) | the stop |

Plus the existing SMA Crossover (as the 50/200 golden cross) and Buy and Hold (the baseline everything is
compared with). Each rule lives in one place: the strategy's own code is what the backtest runs and what the
desk asks for "what do you say now, and at what level" (a pure function of the bars that both use), so the live
signal can never disagree with the tested rule.

## Evidence per strategy and holding (nightly, after the close)

- Daily candles from Kite (as far back as Kite gives, at least 5 years where the stock has them), through the bar
  cache.
- Each strategy is run on the stock with the engine's realistic charges and fills, plus a walk-forward
  (the existing `stockify.research.walkforward`) so the score is out of sample.
- Kept per strategy: out-of-sample return vs buy-and-hold, hit rate, average win/loss, worst drawdown, number
  of trades, and a plain reliability grade (strong / some / weak / none) from those numbers.
- Stored per holding and day, so the page loads instantly and the owner can see yesterday's view.

## Live signals and the thesis (during the session)

- Today's candle is built from the live price (the app's existing price feed for the holdings, falling back to
  a Kite quote poll). Each strategy's "now" function runs on history plus today's partial candle about once a
  minute and on demand.
- The thesis is one verdict per holding: **Hold**, **Tighten stop**, **Trim**, or **Exit**, with
  - the levels that would change it ("exit if it closes below ₹2,412; that's the Supertrend line"),
  - the agreeing and disagreeing strategies, each weighted by its reliability grade on this stock,
  - the option chain gauge in words (below),
  - nothing about the owner's cost or profit (owner, 2026-11-17: "verdict should be rule based not really
    focusing on how much I'll actually make because that compromises logical decisions on a psychological
    level"). The desk never shows average cost, gain/loss, or ₹ outcomes of a level; quantity appears only where
    an option idea needs it (lots). The Portfolio list keeps its existing P&L columns, unchanged.
- Weighting is my call, to be shown on the page: a strategy graded "none" counts for nothing, "weak" a little,
  "strong" fully; the verdict needs agreement from strategies holding most of the weight. The page always shows
  the count ("4 of 7 tested rules say exit; the 3 with the best record on this stock are among them").

## The option chain

- F&O stocks only (from refdata); a holding without options says so and skips the section.
- Near and next monthly expiries, about 10 strikes either side of the price, polled through `quote` once a
  minute during the session (one or two calls a minute for all holdings together); after the close, the last
  snapshot and the day's bhavcopy.
- As a gauge: put/call open interest ratio, where open interest piles up (call walls above, put walls below,
  read as resistance and support), max pain, implied volatility from black76 and how it compares with the
  stock's past year (from the archived fo bhavcopies).
- As ideas, each with premium, max loss, max gain, breakeven, margin and the lot size:
  - protecting the shares: protective put, collar;
  - earning on the shares: covered call, only when the holding is at least one lot;
  - following the stock's move: bull call spread or long call when the tested rules turn up, bear put spread or
    long put when they turn down;
  - each idea says why it fits now, and what makes it a bad idea (cost vs the stock's typical move, IV high).
- Ideas that can't be done are left out with the reason (no quotes, too few shares for a lot).

## Where it shows

- Portfolio page: a verdict chip per holding, linking to its page.
- New page per holding (`/portfolio/<symbol>`): chart on top, thesis beside it, then the strategies table, the
  chain gauge and the option ideas.
- The Tests page lists the new strategies like the existing ones.

## Calls made after the builder check (mine, marked so the owner can check them)

- **Verdict table.** Weights: none 0, weak 1, some 2, strong 3. Each strategy's "now" is In (holding) or Out.
  Exit share = weight of Out / total weight. Exit when share ≥ 0.6 and at least one strong-or-some strategy is
  Out; Trim when 0.4 ≤ share < 0.6; Tighten stop when share < 0.4 but the price is within 1 ATR(14) of the
  nearest weighted exit level; otherwise Hold. A strategy that is Out but would buy today counts as In ("its rule
  is buying here"). If total weight is 0 the verdict is "No reliable signal" and the page says why. The table is
  shown on the page.
- **"Now" is a replay.** The desk runs each strategy's own `on_bar` over the bars through a light replay context
  (instant fills at the close, the `exit_level` plot read back), so backtest and desk run the same code. (Revised
  while planning: no second `state()` function per strategy is needed.)
- **Holdout (found while planning).** research.json keeps the months from 2026-08-01 aside for research and
  refuses backtests that reach them. The nightly grading backtests therefore end on 2026-07-31 (or yesterday,
  whichever is earlier) and the page says so; the live "now" replay is not a test and uses every bar. The
  grading backtests use the Python engine directly, so they add no rows to the research registry. "Now" replays the rule over
  all history plus today's partial candle; it does not depend on when the owner bought (every exit level in the
  table is entry-independent: Chandelier uses the 22-day high, Donchian the 10-day low). The owner's cost and
  profit play no part in the verdict and are not shown on the desk (owner's decision, above).
- **Evidence = defaults across time, not a fitted sweep.** Each strategy runs with its defaults over the whole
  history; the score uses yearly windows (each window's result vs buy-and-hold in that window), so a rule that
  only worked in one year is visible. Grade: none if fewer than 8 trades or less than 3 years; strong if it beat
  buy-and-hold on risk-adjusted return in ≥ 70% of windows and overall; some ≥ 55%; weak otherwise. No parameter
  fitting, so nothing is fitted to its own history.
- **Holdings in scope:** NSE equities and ETFs in Kite holdings with at least one year of daily candles; others
  are listed with the reason (BSE-only, SGB, too little history). Quantity = Kite's total including T1;
  "at least one lot" uses the quantity that isn't pledged. Average price = Kite's.
- **Live prices and today's candle:** the app's own price feed (the API's ticker and tick buffer, a separate
  connection from the recorder's, which stays untouched), seeded on page open with today's minute candles from
  Kite `historical` (one call per holding per page open, cached for the day). Daily history comes from the bar
  cache.
- **Who polls the chain:** the API process, during the session only, while the app runs (so the Portfolio chips
  stay fresh); about one to two quote calls a minute for all holdings together. Nothing runs when the app is off;
  the page then shows the last snapshot with its time.
- **Margin:** each idea shows the margin Kite says it needs (`basket_order_margins`) beside the owner's
  available cash (account margins); ideas above it are marked "needs more margin than you have". Covered calls
  and collars note that pledging the shares lowers the margin.
- **Stored views** are kept per holding per day indefinitely; they are derived (rebuildable) data, so not under
  the tick-data never-drop rule, but nothing deletes them.

## Left to the builder

- Exact storage shape (tables in the lab store, the run registry for the nightly runs), API routes, and how the
  nightly job joins the daemon's after-close schedule (it must not run beside the live recorder; the existing
  `HELD_DURING_SESSION` rule applies).
- The reliability grade's exact thresholds, stated on the page.
- Chart overlays' styling, within web/design.md and Hallmark.
