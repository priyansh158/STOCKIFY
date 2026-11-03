# 02 — Concepts behind the design

Each concept explains an idea from first principles, then says exactly how SimLab handles it. Tasks refer to these by ID (C-1 … C-17). If a design choice in a milestone file looks arbitrary, the reason is here.

---

## C-1 Snapshot feeds: what Kite actually tells you

**The idea.** An exchange's matching engine produces a stream of individual trades and order-book changes, often hundreds per second for a liquid contract. Kite does not forward that stream. It forwards **snapshots**: about one or two per second per instrument, each saying "as of now, the last trade was at X, day volume is V, the best five bids and asks are …" (`01_facts.md` K-4).

Between two snapshots, many trades can happen at prices you never see. A Nifty option can trade at 101, 99, 103 and 100 between snapshots that both show LTP 100.

**What you can still infer** (this is where a careful simulator gains realism for free):

- **Volume traded between snapshots**: `ΔV = V₂ − V₁` (day volume is cumulative). If `ΔV = 0`, nothing traded, so a resting order could not have filled.
- **The average price of those trades**: Kite sends the day's average traded price (ATP). Since `ATP × V` is the day's traded value, the value traded between snapshots is `ATP₂·V₂ − ATP₁·V₁`, and the VWAP of just those trades is that value divided by `ΔV`. That interval VWAP is a much better fill price for a market order than either LTP. One catch: ATP arrives rounded to the paisa, and multiplying a rounded number by a day volume of millions magnifies the rounding. With volume going from 9,999,000 to 10,000,000 and ATP from ₹100.00 to ₹100.01, the formula gives ₹200 for trades that clearly happened near ₹100. So SimLab uses the interval VWAP only when it falls between the prices actually observed, and discards it otherwise (`A4_engine_semantics.md` §2.1).
- **New extremes**: if the day high rose from H₁ to H₂ between snapshots, some trade printed at H₂, even if no snapshot shows it. A resting sell limit at or below H₂ was touched. This only helps when the price makes a new day extreme, but those are exactly the moments stop and target orders trigger.
- **Pressure**: total buy vs sell quantity in the book (quote mode) is a crude imbalance signal.

**What you cannot know**: the exact path, the exact time of each trade, or your place in the queue.

**How SimLab handles it.**
- The recorder stores every field plus the raw packet bytes (D-08), so these inferences can be computed later and improved without re-recording.
- Fill models are explicit about which evidence they use (`A4_engine_semantics.md` §2 and §4). "LTP crossing" models are labelled approximate (I-14).
- Event time for recorded data is the **receive time** on our machine, not the exchange timestamp (which only has 1-second resolution and is missing in quote mode). The same timeline is used live and in replay, which is what makes parity possible (C-10).

## C-2 The bid-ask spread, and why options need quote-based fills

**The idea.** The best bid is what someone will pay you right now; the best ask is what you must pay right now. The LTP is just the last price two other people agreed on. A market buy fills at (or above) the ask, a market sell at (or below) the bid. Crossing the spread costs half the spread on each side.

For a liquid stock the spread is a few hundredths of a percent. For options it can be several percent of the premium, and much wider for strikes away from the money or near expiry. A strategy that sells a ₹20 option and buys it back at ₹10 "makes ₹10" on LTPs, but if the spread is ₹1 wide it actually sells at the bid (say 19.5) and buys at the ask (10.5): ₹9, a 10% difference before fees. For high-turnover option strategies, spread cost is often larger than all fees combined.

**How SimLab handles it.** Option strategies default to the quote-based fill model, which crosses the recorded spread and walks the recorded depth (D-10). The recorder records traded strikes in FULL mode so depth exists (D-08). When a quote is missing, there is no fill (I-7), rather than a fill at a stale LTP.

## C-3 Look-ahead bias and event time

**The idea.** Look-ahead bias means using information in a decision that did not exist at the moment of the decision. It is the most common reason backtests look better than reality. Three classic forms:

1. **Bar timestamps.** Kite labels a 1-minute candle with its *start* time: the bar stamped 09:15:00 covers 09:15:00–09:15:59 and is only *complete* at 09:16:00. A strategy that "sees" the 09:15 bar at 09:15:00 knows its close a minute early.
2. **Filling inside the bar that triggered the signal.** If the 09:15 bar's close triggers a buy, the earliest realistic fill is the *next* bar's open. Filling at the triggering bar's close, or anywhere within its high-low range, uses the future.
3. **"The close" is not a tradable price.** NSE's official close is an average over the last 30 minutes (or, since 2026-08-03, an auction price for F&O stocks). You cannot know it before you trade, and for F&O contracts you cannot trade at it.

**How SimLab handles it.**
- Every event has one time `t_event` = the moment it became knowable: bar start + interval for bars, receive time for snapshots, publication time for end-of-day files.
- An order becomes active at `t_activate` (submit time + latency) and may only use market information created after that moment: it can fill on a bar only if it was already active when that bar *started*, and it can use the trades between two snapshots only if it was active at the first of them (A4 §4.0). The subtle case this catches: an order placed by a timer at 09:16:30 must not fill at the 09:16 bar's open price, because that price happened 30 seconds before the order existed. These rules live in one place (the matcher), and golden scenarios G01–G03 plus property test P-07 prove them (`A3_testing.md`).
- Daily-bar strategies trade at the next day's open; end-of-day option strategies trade at a configurable spread around the close and are labelled approximate.

## C-4 Survivorship bias and point-in-time universes

**The idea.** If you backtest "the Nifty 50" using *today's* list of members, you have silently dropped every company that was removed for doing badly, and added companies *because* they did well. The universe itself contains future information.

**How SimLab handles it.** Universes are resolved *as of* each date. The F&O bhavcopy gives a clean point-in-time list for free: a stock was F&O-eligible on day *d* if stock futures on it appear in that day's file. Index membership history is not in the data sources used here; strategies that need "Nifty 50 members as of d" get a 🛑 in M6 until a source is chosen.

## C-5 Corporate actions and price basis

**The idea.** When a company splits 1:2, its price halves overnight. Nothing economic happened, but a raw price chart shows a 50% crash. Data vendors handle this two ways:

- **Raw (unadjusted)**: prices as traded. Correct for fills, tick sizes, circuit limits, lot sizes and option strikes, which all refer to raw prices.
- **Adjusted**: all prices *before* the ex-date are divided by the split ratio, so the series is continuous. Correct for signals like moving averages over long windows.

Kite's historical candles are adjusted, and the adjustment is applied retroactively on the ex-date (K-6). Two consequences: a cache of bars fetched before an ex-date becomes inconsistent with bars fetched after it, and adjusted bars cannot be stitched to raw recorded ticks or to bhavcopy prices, which are raw.

**How SimLab handles it.** I-5 (one basis per run) is enforced by the run validator. The bar cache stores `fetched_on` per row, and a detector re-fetches an overlap window to catch adjustments (M1.4). Raw data (recorder, bhavcopy) is the basis for fills; long-horizon signal research on adjusted bars is fine as long as the run is labelled `adjusted`.

## C-6 Instrument identity and token reuse

**The idea.** Kite identifies instruments by a numeric `instrument_token`. Zerodha says a token is unique only for the current day and is reused after a contract expires (K-9). If you key recorded history by token, two different contracts months apart can collide.

**How SimLab handles it.** A permanent surrogate `instrument_id` in `refdata.sqlite`. Derivatives are identified by what they *are* (exchange, underlying, type, expiry, strike); equities and indices by exchange and symbol. Tokens live in a mapping table with validity dates. The recorder resolves token → `instrument_id` at write time using that day's map (D-06).

## C-7 As-of reference data

**The idea.** Many "constants" are only constant for a while: lot sizes, freeze quantities, tax rates, session times, the weekday contracts expire. A backtest of March 2025 must use March-2025 rules even if you run it in 2027. The clean pattern is an **as-of join**: every rule row has `effective_from`; for a date *d*, use the latest row with `effective_from ≤ d`.

A subtle case from real data: on 2025-12-30 NSE resized *existing* Nifty quarterly contracts from 75 to 65 units per lot (X-3). So contract specs need history per contract, not just per new contract.

**How SimLab handles it.** I-1 plus dated tables (`contract_specs`, `charges.json`, `sessions.json`, `contract_rules.json`, `broker_policy.json`). Python resolves all of them for a run's date range into the run bundle, so the C++ engine never does an as-of lookup against a database (D-22). Positions are held in units, not lots, so a mid-life lot change never alters a position; it only changes which new order quantities are valid.

## C-8 Order types, products and validity (Kite semantics)

- **MARKET**: fill now at the best available prices. Since 2026-04-01 Kite converts API market orders into a **limit at a protection price** (LTP ± p%); if the price runs beyond it, the rest waits as a limit order (K-12).
- **LIMIT**: fill at the given price or better; otherwise rest in the book.
- **SL (stop-limit)**: dormant until the market trades through the trigger, then becomes a LIMIT at the given price. Can fail to fill if the market gaps past the limit.
- **SL-M (stop-market)**: dormant until the trigger, then becomes a MARKET (with protection).
- **Validity**: DAY (rest until session end) or IOC (fill what you can immediately, cancel the rest).
- **Products**: **CNC** = delivery equity (you hold the shares, full cash), **MIS** = intraday (leveraged; the broker squares you off at a cut-off time and charges a fee), **NRML** = carry-forward F&O (full exchange margin).

SimLab implements these exactly (`A4_engine_semantics.md` §3–§4; M3.5–M3.6), because strategy behaviour around stops and gaps is where backtests most often diverge from reality.

## C-9 Determinism

**The idea.** A deterministic program gives the same output for the same input, every time. A backtester must be deterministic or you cannot tell a code change's effect from noise, and you cannot test that live and replay agree.

Common sources of hidden non-determinism and the cure SimLab uses:

| Source | Cure |
|---|---|
| Reading the wall clock | Engine time comes only from events (I-2) |
| Random numbers | One seeded generator per run (PCG32, implemented in-house) |
| Standard-library distributions (`std::uniform_int_distribution`, `std::normal_distribution`) | **Not used**: their algorithms differ between MSVC and libstdc++, so the same seed gives different numbers on Windows and Linux. SimLab implements its own transforms |
| Iterating `std::unordered_map` | Never in output paths; use sorted containers or sort by id first |
| Equal timestamps | Explicit tie-break: (t_event, source rank, sequence number) |
| Floating-point contraction / fast-math | `/fp:precise` on MSVC, never `/fp:fast` (Windows only, D-26) |
| Printing floats | `std::to_chars` (shortest round-trip form), never locale-dependent streams |
| Threads | The engine is single-threaded; parallelism is across runs, never inside one |

## C-10 Parity and differential testing

**The idea.** You have two implementations that should agree (a backtest driver and a live driver; a C++ engine and a Python oracle). Feed both the same inputs and compare outputs. Any disagreement is a bug in one of them, and you find it without knowing the "right" answer in advance. This is *differential testing*, and it finds the subtle bugs unit tests miss: event ordering, off-by-one times, rounding.

SimLab has three parity gates (D-11):

1. **Determinism**: the same run twice ⇒ identical artifact hashes.
2. **Journal parity**: a paper session records exactly what its engine saw (its journal). Replaying the journal through a fresh engine must reproduce the live orders and fills exactly. This proves the live path has no hidden inputs (wall clock, threads, races).
3. **Oracle parity**: the C++ engine and the Python oracle run the same golden and randomly generated scenarios and must agree to the paisa.

A fourth comparison is reported but not required to be exact: replaying the *recorder's* copy of a day versus what the live session did. The difference measures how much batching and timing change results, which tells you how far to trust backtests on recorded data.

## C-11 Transaction costs, worked

Using Zerodha's charges as read on 2026-10-29 (`01_facts.md` Z-1), computing each component per order and rounding it to the paisa (A4 §5):

**One Nifty options lot (65 units), buy at ₹100, sell at ₹120.** Gross profit ₹1,300. Brokerage ₹40 (₹20 × 2 orders), STT ₹11.70 (0.15% of the ₹7,800 sell premium), exchange ₹5.08 (0.03553% of each order's premium), SEBI ₹0.02, stamp ₹0.20 (0.003% of the ₹6,500 buy), GST ₹8.12 (18% of brokerage + exchange + SEBI). **Total ₹65.12, about ₹1 per unit.** Every round trip must earn about one rupee of premium per unit before spread costs just to break even.

**One Nifty futures lot (65 units) at 25,000, in and out at the same price.** Turnover ₹32.5 lakh. Brokerage ₹40, **STT ₹812.50** (0.05% of the ₹16.25 lakh sell side), exchange ₹59.48, SEBI ₹3.26, stamp ₹32.50, GST ₹18.50. **Total ₹966.24, about 15 Nifty points.** Under the STT in force until 2026-03-31 (0.02%) the same trade cost ₹478.74 (≈ 7.4 points). The tax hike alone doubled the cost of a futures round trip. (These exact figures are golden test G09.)

Two lessons the lab should make visible: STT, not brokerage, dominates futures costs; and a strategy's edge must be measured *net* of these numbers, at the rates in force on each trade date (C-7). SimLab validates its cost engine against Kite's own virtual contract note endpoint (`/charges/orders`, K-10) instead of trusting hand-computed numbers (A3 G08).

## C-12 Margin: why "% of notional" is not enough

**The idea.** Margin is the money the exchange blocks so you can cover losses. Indian exchanges compute it with **SPAN** (scenario analysis) plus **exposure / extreme-loss margin (ELM)**:

- **SPAN scanning risk.** Revalue the whole portfolio on one underlying under 16 standard scenarios: the underlying moving 0, ±⅓, ±⅔ and ±1 times a *price scan range* (PSR), each with volatility up and down, plus two extreme moves (classically ±3 × PSR, counted at 35%). The margin is the worst loss across scenarios. Because the whole portfolio is revalued together, a hedge (long wings on a short straddle) reduces the worst loss and therefore the margin.
- **Short-option minimum.** Far out-of-the-money short options look riskless in the scenarios, so a per-lot minimum charge applies.
- **ELM / exposure.** An extra percentage of notional for tail risk, with special add-ons (e.g., +2% on short index options on expiry day, X-9).
- **Premium.** Option buyers pay the full premium upfront (X-10).

A flat "x% of notional per short leg" model ignores the hedge: it charges an iron fly almost the same as a naked straddle, although the iron fly's maximum loss is capped. Since margin sets how much capital a strategy ties up, a wrong margin model distorts return-on-capital, drawdown percentages and position sizing by multiples for hedged option books.

**How SimLab handles it.** Tiered models (D-14): T0 flat (simple, labelled approximate); T1 SPAN-lite (the 16 scenarios with Black-76 revaluation and configurable PSR/VSR); T2 exact SPAN from NSE's risk-parameter files (stretch); LIVE from Kite's basket-margin endpoint during paper sessions. T1's parameters are **calibrated against LIVE** by sampling portfolios during market hours (M4.9). That calibration is a small, real quant-risk exercise in its own right.

## C-13 Black-76 and implied volatility

**Why Black-76.** NSE index and stock options are European (exercise only at expiry) and their natural hedge is the future of the same expiry. Black-76 prices an option on a forward/futures price *F*, avoiding the need to estimate dividends or the index's carry separately.

With discount factor `D = e^{−rT}`, `d₁ = [ln(F/K) + σ²T/2] / (σ√T)`, `d₂ = d₁ − σ√T`, and N/n the standard normal CDF/PDF:

- Call `C = D·[F·N(d₁) − K·N(d₂)]`; Put `P = D·[K·N(−d₂) − F·N(−d₁)]`
- Delta (w.r.t. F): call `D·N(d₁)`, put `−D·N(−d₁)`
- Gamma: `D·n(d₁) / (F·σ·√T)` · Vega: `D·F·n(d₁)·√T` (per 1.00 of vol; divide by 100 for per vol point)
- Theta (per year): `−D·F·n(d₁)·σ/(2√T) + r·C` for calls, same with `+ r·P` for puts

Worked example (also a test vector, A3 G17): F = 25,000, K = 25,000, T = 7/365, r = 6.5%, σ = 12% ⇒ call = put ≈ ₹165.53, call delta ≈ 0.503, gamma ≈ 0.000959, vega ≈ 1,379 per 1.00 vol (≈ ₹13.79 per vol point).

**Where F comes from.** Monthly expiries have a future of the same expiry. **Nifty weekly options do not**, so SimLab uses the *synthetic forward* from put-call parity: `C − P = D·(F − K)` ⇒ `F = K + (C − P)/D`, computed from mid quotes at the 3–5 strikes nearest the money and taking the median. This needs bid/ask, another reason traded chains are recorded in FULL mode.

**Implied volatility (IV)** is the σ that makes the model price equal the market price. There is no closed form, so it is solved numerically: Brent's method on σ ∈ [0.001, 5.0], after checking the no-arbitrage bounds (a call must be worth between `D·max(F−K, 0)` and `D·F`; a put between `D·max(K−F, 0)` and `D·K`). Prices outside the bounds have **no IV** (return NaN with a reason; never clamp silently). Near expiry, T → 0 makes vega tiny and IV numerically unstable, and far out-of-the-money options can be worth less than one tick (₹0.05); the solver reports these as low-confidence instead of returning noise.

**Time to expiry** uses calendar time (ACT/365) to the option's last trading moment from `sessions.json`, with an intraday fraction. That moment moved from 15:30 to 15:40 IST on 2026-08-03 (X-7): another example of C-7.

## C-14 Research hygiene: why most backtests lie

**The idea.** Try 200 random strategies and the best one will look great by luck alone. The more variants you test, the higher the best in-sample Sharpe you should *expect* even if nothing works. This is the multiple-testing problem, and it is why a backtest's Sharpe must be judged against how many things you tried.

Tools SimLab builds in (M6):

- **Trial registry.** Every run, including the ones you discard, belongs to a *family*. The UI shows "best of N trials", not just the best.
- **Deflated Sharpe Ratio** (Bailey & López de Prado, 2014): the probability that the strategy's true Sharpe beats the best Sharpe that luck alone would produce across your N trials, after accounting for non-normal returns (skew, fat tails). Formula in M6.4.
- **Walk-forward analysis.** Choose parameters on one window, test on the next, roll forward, and stitch only the out-of-sample pieces.
- **A locked holdout.** The most recent months are hidden unless explicitly unlocked, and every unlock is logged.
- **Sensitivity.** Re-run with costs × 1.5 and × 2, with one extra tick of slippage, and with each fill model. A strategy whose profit disappears under small perturbations was probably fitting noise.

## C-15 Settlement: how contracts end

- **Index F&O** settle in **cash** at the final settlement price (FSP): the official closing value of the index on expiry day. An in-the-money long call receives `(FSP − K) × quantity`; out-of-the-money options expire worthless.
- **Stock F&O** settle **physically**: futures and in-the-money options become real share deliveries (futures at the settlement price, options at the strike). Brokers raise margins in the days before expiry (Z-4) and charge more (0.25% brokerage, STT on the full contract value).
- **Exercise STT.** Long options held to expiry and exercised pay STT on intrinsic value (0.15% since 2026-04-01). Selling just before expiry pays 0.15% of the premium instead, which is about the same for deep in-the-money options, so the old "STT trap" is mostly gone, but brokerage and the spread still make the choice matter.
- **Where FSP comes from**: never computed by SimLab (D-13). NSE publishes it (index closing values; settlement prices in the F&O bhavcopy). Since 2026-08-03, closes of F&O stocks come from a closing auction, which Kite's snapshots cannot reproduce anyway.

## C-16 Metrics that mean something

- **Returns on what?** NAV = cash + market value of positions (options at mid or last price, futures at their mark-to-market P&L). Daily return = change in NAV / previous NAV. With leverage (MIS, futures), returns are on *capital*, not on notional; reporting both avoids flattering numbers.
- **Sharpe** = mean(daily return − daily risk-free) / stdev(daily return) × √252. Use 252 consistently (NSE has ~245–250 sessions a year; the convention matters less than consistency). For intraday strategies, compute from *daily* P&L, not per-trade returns.
- **Max drawdown** = worst peak-to-trough NAV fall, with its duration.
- **Turnover** = traded value / average NAV per year. High turnover × costs (C-11) is where strategies die.
- **Cost share** = total costs / gross P&L. Above ~50%, the strategy mostly works for the exchange and the tax office.
- **Exposure** = fraction of time with a position; **capacity** = largest share of market volume the strategy needed.

## C-17 What paper trading cannot tell you

Paper fills assume your order does not change the market. In reality, a large order moves prices (impact), a passive order waits in a queue you cannot see, and the round-trip latency from Seattle to Mumbai is far larger than from a server in Mumbai. Paper trading also removes the emotional side of watching real money. SimLab's job is to make results *honest within these limits* (conservative fills, participation caps, latency settings, labelled approximations), not to pretend the limits are not there.
