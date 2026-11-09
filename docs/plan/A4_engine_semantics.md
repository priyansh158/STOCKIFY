# A4 — Engine semantics (the contract)

This is the single definition of how a simulation behaves. The Python oracle (M2) and the C++ engine (M3) both implement it; golden scenarios (A3) cite its section numbers. **If code and this file disagree, one of them is a bug.** Changing a rule here needs a decision-log entry (D-xx) and matching updates to both implementations and to the affected goldens, in the same change.

The goal is that two careful, independent implementations produce **identical** orders, fills, charges, positions and NAV, to the paisa. Every rule below is written so that there is only one way to implement it.

---

## §0 Numbers

**0.1 Integers for money and quantities.** Money is `int64` paise; quantities are `int64` units (I-3). Rates are integers too:

| Quantity | Unit | Example |
|---|---|---|
| Charge and tax rates | **ppb** (parts per 10⁹) | 0.15% → `1_500_000`; 0.00183% → `18_300`; ₹10/crore → `1_000`; GST 18% → `180_000_000` |
| Participation, depth-take fraction | **ppm** (parts per 10⁶) | 10% → `100_000` |
| Market protection | **bp** (basis points) | 2% → `200`; 0.5% → `50` |

Config files hold rates as decimal strings; loaders convert them **exactly** (Python `Decimal`, C++ a hand-written decimal parser) and fail if the result is not an integer in the target unit. Rates are never parsed into floating point.

**0.2 Integer helpers** (identical in both implementations; C++ uses a portable 128-bit helper, Python plain `int`):
- `round_half_up_div(a, d)` for `a ≥ 0, d > 0`: `(a + d/2) / d` with integer division, where `d` is even (all divisors used here are).
- `floor_div(a, d)` and `ceil_div(a, d)`: mathematical floor/ceiling for any sign (not C++ truncation).
- `mul_div_*(a, b, d)`: the product `a·b` is formed in 128 bits before dividing; overflow of the final result is an error, never wraparound.
- `floor_to(x, step) = floor_div(x, step)·step`, `ceil_to(x, step) = ceil_div(x, step)·step`.

**0.3 Floating point** is allowed only for analytics: indicators, Black-76/IV, SPAN-lite internals, metrics. Rules:
- Formulas are evaluated **in the exact expression order written** in this file and in `A2_interfaces.md` §4.3 (no algebraic rearrangement). C++ is compiled without FP contraction (C-9).
- Reductions are explicit left-to-right loops. The oracle must not use `sum()`, `math.fsum`, or numpy/pandas reductions in anything that feeds an artifact (Python's `sum()` of floats uses compensated summation since 3.12).
- A float that becomes money is converted by one function: margin requirements use `ceil_to(ceil(x), 100)` (round up to the next whole rupee).

## §1 Time and events

**1.1 Input events** (from event packs and live journals, format A1 §5) and their `t_event` (the moment the information became knowable, C-3):

| Kind | `t_event` |
|---|---|
| `TICK_FULL`, `TICK_QUOTE`, `INDEX` | receive time on our machine |
| `BAR` from Kite candles | bar start + interval |
| `BAR` from NSE end-of-day files (`bhav_eod` packs) | EOD publication time on the trade date (`pricing.json`) |
| `SETTLE` | publication time of that number (`pricing.json`) |
| `REFDATA`, `GAP`, `CLOCK` | when the daemon learned it / when the gap started or ended / wall time |

Records appended to a live journal get `t_event = max(intended time, last journal t_event)`, so a journal is always non-decreasing (late data such as evening settlement prices never goes back in time).

**1.2 Internal events** created by the engine: `PHASE` (session transitions from the bundle), `BAR_BUILT` (bars aggregated from snapshots, §2.2), `MIS_SQOFF` (the cut-off), `TIMER` (strategy timers), `SEG_CLOSE` (a session group's close), `DAY_END` (23:59:59.999999999 IST of each trading day, plus the final close-out of §1.10).

**1.3 One ordering for everything.** Every event, input or internal, has a key `(t, rank, src, seq)`:

<!-- <block name="event-ranks" same-as-pattern='^\| \d+ \| \x60(?P<value>[A-Z_]+)\x60'> -->

| rank | event | origin |
|---|---|---|
| 0 | `PHASE` | internal |
| 1 | `GAP` | input |
| 2 | `REFDATA` | input |
| 3 | `SETTLE` | input |
| 4 | `MARKET` (market data): `TICK_FULL`, `TICK_QUOTE`, `INDEX`, `BAR` | input |
| 5 | `CLOCK` | input |
| 6 | `BAR_BUILT` | internal |
| 7 | `MIS_SQOFF` | internal |
| 8 | `TIMER` | internal |
| 9 | `SEG_CLOSE` | internal |
| 10 | `DAY_END` | internal |

<!-- </block> -->

`src` = the pack's index in the bundle's pack list (0 for journals and for internal events); `seq` = the record's sequence number (input) or a creation counter (internal). The engine always processes the smallest key next. Internal events are only ever created with keys greater than the key being processed.

**1.4 Batch independence.** In `push()` mode the engine processes queued internal events only while their key is smaller than the key of the next input record; after the last record of a batch, internal events with larger keys wait for more input or for `finish()`. Consequently results never depend on how input was split into batches (A3 P-02). Live sessions run one engine per session-day and call `finish()` after the day's last records (including settlement prices) are appended (M5.3).

**1.5 Handling a market-data event E for instrument i**, in order:
1. Update i's market state (§2).
2. Match i's eligible orders against E (§4): stop triggers, then fills.
3. Apply fills to the ledger (§6).
4. Deliver queued callbacks (§3.9): `on_order_update` and `on_fill`, in the order they were generated.
5. Deliver `on_tick` (or `on_bar`) for E if the strategy subscribed to i, then any callbacks generated inside it.

Orders placed during steps 4–5 get `t_submit = t_event(E)`.

**1.6 `CLOCK` events only advance time.** Inserting or removing them never changes a deterministic artifact (A3 P-03).

**1.7 Latency.** `latency_ms` (run config, default 0). An order, modification or cancellation submitted at `t_submit` *activates* at `t_activate = t_submit + latency`. What it may match is defined in §4.0. A fill on an event before a cancellation activates stands (a realistic race).

**1.8 Timers.** `schedule_at(t)` requires `t > now()`; the timer fires at exactly `t` (rank 8), whether or not market data arrives.

**1.9 Sessions.** Phases come only from the bundle: `days[].groups[g]` for each session group `g ∈ {CM, CM_CAS, FO_FUT, FO_OPT}` (A1 §6.2). Any time not inside a listed phase is `CLOSED`. A group's **close** is the end of its last `CONTINUOUS` or `CAS` phase that day. `POSTCLOSE` is informational: for orders it behaves like `CLOSED`.

**1.10 Day processing.** `SEG_CLOSE` at each group's close cancels that group's open DAY orders and makes provisional marks (§6.4). `DAY_END` at 23:59:59.999999999 IST settles expiring contracts that have no settlement price yet (§7.2 fallback), writes `positions_eod` rows and the day's single NAV row, delivers `on_session_end`, and then (paper sessions) calls `save_state()`. Orders placed in `on_session_end` follow §3.10 (they queue for the next trading day). **Final close-out (D-52):** when a run's last input is knowable after its last trading day's `DAY_END` (that day's daily bar is knowable only at the next midnight IST, §1.1), the input is processed as usual and then one more `DAY_END` runs at that same moment, dated by its own IST date: it writes that date's `positions_eod` rows and NAV row and delivers `on_session_end`, like any other. Nothing after it is processed (orders it leaves pending stay pending; later timers never fire). Live (§1.4), this happens in `finish()`.

## §2 Market state

**2.1 Snapshots and evidence.** Per instrument the engine keeps the last two snapshots of the current day, `s0` and `s1`. When `s1` arrives:
- `ΔV = vol₁ − vol₀` (negative ⇒ data error, logged, treated as 0).
- `lo = min(ltp₀, ltp₁)`, `hi = max(ltp₀, ltp₁)`; if `low₁ < low₀` then `lo = min(lo, low₁)`; if `high₁ > high₀` then `hi = max(hi, high₁)` (new day extremes, C-1).
- **Interval VWAP**, valid only when `ΔV > 0`: `value = atp₁·vol₁ − atp₀·vol₀` (128-bit). It is *valid* only if `lo·ΔV ≤ value ≤ hi·ΔV`. Kite's ATP is rounded to the paisa, so for very active instruments the value can be badly off; invalid values are discarded, never clipped. Used as a price: buys `ceil_to(ceil_div(value, ΔV), tick)`, sells `floor_to(floor_div(value, ΔV), tick)`.
- The first snapshot of a day, or the first after a `GAP` affecting the instrument, has no `s0`: it provides prices and a book but no trade evidence.

**2.2 Bars built from snapshots** (`BAR_BUILT`, only for `subscribe_bars` on a tick dataset). Buckets `[start, start + interval)` on a grid anchored at the group's `CONTINUOUS` start that day. OHLC from the LTPs of snapshots whose `t_event` falls in the bucket; volume = Σ ΔV of those snapshots (the day's first snapshot contributes its whole day volume); OI = last OI seen. A bucket with no snapshots produces no bar (I-7). The bar is emitted at the bucket end. Built bars never produce fills.

**2.3 Staleness.** A snapshot is stale when `now − t_event > stale_after_ms` (`pricing.json`). Fill models never use a stale book; the strategy API exposes staleness.

## §3 Orders

**3.1 Fields.** `order_id`, `parent_id` (slices), instrument, side (`BUY`/`SELL`), quantity, product (`CNC`/`MIS`/`NRML`), type (`MARKET`/`LIMIT`/`SL`/`SL-M`), price, trigger, validity (`DAY`/`IOC`), market protection in bp (or `AUTO`), tag, `t_submit`, `t_activate`, status, filled quantity, average price, reason. Order ids are assigned 1, 2, 3, … in submission order across the whole run (§3.8 for slices).

**3.2 Validation at submit.** Checks run in this order; the first failure rejects with that code:

<!-- <block name="reject-codes" same-as-pattern='^\| \x60(?P<value>R_[A-Z_]+)\x60'> -->

| Code | Rule |
|---|---|
| `R_INSTRUMENT` | Unknown instrument, or past its last trading moment |
| `R_PRODUCT` | CNC on derivatives, NRML on equities, or a product the policy forbids |
| `R_SESSION` | MIS after the dated cut-off; `preopen_policy = reject` while the group is not `CONTINUOUS` (§3.10) |
| `R_CAS` | CAS-eligible stock during its `CAS` phase with `cas_policy = none` (§4.8) |
| `R_QTY_LOT` | Derivative quantity not a multiple of the lot size in force on the submit date. ⚠ Exception (verify against the NSE circular, X-3): an order that only reduces an existing position may be a multiple of any lot size in force while that position was opened |
| `R_TICK` | Price or trigger not a multiple of the tick size (rejected, never rounded; D-23) |
| `R_BAND` | Limit price outside the price band in force (`REFDATA`) |
| `R_FREEZE` | Quantity above the freeze limit with `freeze_policy = reject` (with `slice`, see §3.8) |
| `R_HOLDINGS` | CNC sell above holdings (no CNC short selling) |
| `R_NO_REFERENCE` | MARKET or SL-M with no LTP known for protection or for the funds check |
| `R_MARGIN` | Required funds (§6.5) exceed available funds |

<!-- </block> -->

System orders tagged `AUTO_SQOFF` or `RMS_SQOFF` skip `R_SESSION`, `R_CAS`, `R_FREEZE` (they are sliced) and `R_MARGIN`.

**3.3 Market protection** (C-8, K-12). At activation of a MARKET order (or the trigger of an SL-M): `ref` = the instrument's last LTP known at that moment, i.e. from events processed before it (bar packs: the close of the last bar of the fill interval known by then). No `ref` ⇒ rejected `R_NO_REFERENCE` at activation. `bp` = the order's protection or, for `AUTO`, the policy's `market_protection_auto_bp`. The order becomes a LIMIT with `from_market = true`:
- buy limit = `floor_div(ref·(10000 + bp), 10000·tick)·tick`;
- sell limit = `ceil_div(ref·(10000 − bp), 10000·tick)·tick`.

It keeps DAY validity, so an unfilled remainder rests as a limit. Every fill of every order is capped by its limit price.

**3.4 Stop triggers.** A buy stop triggers when evidence reaches `≥ trigger`; a sell stop when it reaches `≤ trigger`. Evidence: on bars, the path (§4.1); on snapshots, `hi`/`lo` when the order was active at `s0` (§4.0), otherwise `ltp₁` only. On trigger, SL becomes a LIMIT at its price; SL-M becomes a protected MARKET whose `ref` is the trigger price, or the first price beyond it when the market gapped through it (bars: the open; snapshots: `ltp₁`).

**3.5 States.** `PENDING` (submitted, not yet active) → `OPEN` or `TRIGGER_PENDING` → `COMPLETE` | `CANCELLED` | `REJECTED`; a sliced parent is `SLICED`. Partial fills keep an order `OPEN`. Cancellation reasons: `EOD`, `IOC`, `USER`, `R_CAS`, `RMS`.

**3.6 Modify and cancel** activate after latency (§1.7). Allowed type changes (Kite): LIMIT→MARKET; SL→LIMIT or MARKET; SL-M→LIMIT or MARKET; never LIMIT→SL/SL-M. Reducing quantity below the filled quantity is rejected.

**3.7 IOC.** Matched against the first eligible market-data event of its instrument; the remainder is cancelled (`IOC`) right after that event. An IOC that is not marketable on that event is cancelled unfilled.

**3.8 Slices** (`freeze_policy = slice`). The parent takes the next id and status `SLICED`; children take the following ids, each of `floor_to(freeze, lot)` units except the last, at most `max_slices` (otherwise the parent is rejected `R_FREEZE`). Each child re-runs §3.2 from `R_HOLDINGS` onward, and each pays its own per-order charges (as on Kite).

**3.9 Callback timing.** `place()`, `modify()` and `cancel()` validate synchronously and return immediately; `ctx.order(id)` shows the new status at once. The matching `on_order_update` callbacks are queued and delivered after the current callback returns, in the order generated.

**3.10 Orders while the group is not continuous.** An order submitted while its instrument's group is `CLOSED`, `PREOPEN` or `POSTCLOSE`:
- `preopen_policy = queue` (default): accepted as `PENDING`; activates at the next `CONTINUOUS` start of that group (plus latency). Its trade date is that day; DAY validity counts from then. The F&O pre-open call auction itself is not simulated.
- `preopen_policy = reject`: rejected `R_SESSION`.

## §4 Fill models

**4.0 Eligibility and common rules** (no look-ahead, C-3):
- **Bars**: an order may fill on a bar only if `t_activate ≤ effective_start`, where `effective_start = max(bar start, the group's CONTINUOUS start that day)`. (A daily bar's effective start is the open, so an order queued overnight fills on that day's bar.)
- **Snapshots**: fills that use trade evidence from `s0 → s1` (ΔV, interval VWAP, `lo`/`hi`) need `t_activate ≤ t(s0)`. Fills against the book shown in `s1` need `t_activate < t(s1)`.
- Fills use the pack's bars of the **smallest interval** available for the instrument; other intervals and built bars never fill.
- Fill quantities are multiples of the lot; fill prices are multiples of the tick and never worse than the order's limit.
- Participation caps: `cap = floor_to(floor_div(volume·ppm, 1_000_000), lot)`.

**4.1 `bar`.**
- The intrabar path is one of two: `P_HL = O→H→L→C` or `P_LH = O→L→H→C` (straight segments). Simulate the strategy's eligible orders on the instrument along each path, each simulation with a **copy** of the ledger and of the slippage RNG; compute the strategy NAV at the bar's close (instrument marked at `C`); **adopt the path with the lower NAV** (ties → `P_LH`), including that simulation's RNG state.
- MARKET: fills at `O` plus adverse slippage, capped by the protection limit. If `O` is already beyond the limit (a gap larger than the protection band), the order behaves as a resting limit at its protection price for the rest of the bar and later bars.
- Buy LIMIT `p`: if `O ≤ p` fills at `O`; else if the path reaches `p` it fills at `p` (`limit_touch_policy = through` requires `L < p`; `touch` requires `L ≤ p`). Sells mirror this.
- Stops: buy stop `s` triggers at the open if `O ≥ s`, else where the path first reaches `s`. SL-M fills at `max(s, O)` plus slippage (capped by protection); SL continues as a limit along the rest of the path.
- Volume cap per bar and instrument: the strategy's total fills ≤ participation cap of the bar volume. The remainder stays open (DAY) or is cancelled (IOC).

**4.2 `snapshot_ltp`** (label: approximate).
- Marketable orders (MARKET after protection, a buy LIMIT ≥ the evidence price, a triggered SL-M) fill only with trade evidence (`ΔV > 0`, order active at `s0`): price = the valid interval VWAP, otherwise `ltp₁`, plus slippage, capped by the limit; quantity ≤ participation cap of `ΔV`. Without evidence they wait for the next snapshot.
- Resting buy LIMIT `p` fills at `p` when `lo < p` (`through`) or `lo ≤ p` (`touch`), quantity ≤ participation cap of `ΔV`. Sells mirror this.

**4.3 `snapshot_quote`** (needs `TICK_FULL`; default for options, D-10).
- **Arriving orders** (on their first eligible snapshot): marketable buys walk the ask levels 1–5 of `s1` up to their limit, taking at each level `min(remaining, floor_to(floor_div(level_qty·depth_ppm, 1_000_000), lot))` at that level's price. Sells walk the bids. Within one snapshot, orders consume a working copy of the book in `(t_activate, order_id)` order; the book resets at the next snapshot.
- **Resting orders** (after their first eligible snapshot): a buy at `p` fills **at `p`** when (a) `s1`'s best ask ≤ `p` — quantity up to the depth-take share of the ask quantity at prices ≤ `p` — or (b) trade evidence shows `lo < p` — quantity up to the participation cap of `ΔV`; the fill is `min(remaining, max(qa, qb))`. Queue position is not modelled beyond requiring trades *through* the price. Sells mirror this.
- A stale book or an empty side means no fill (I-7).

**4.4 Slippage** (bar and LTP models only): `none`, `fixed_ticks(k)`, `random_ticks(k)` = `below(k+1)` ticks from stream 1, or `bps(x)` (ceil to tick). Always adverse; never beyond the protection limit.

**4.5 Randomness: PCG32**, exactly as the reference `pcg_basic.c`:
- Seeding `srandom(initstate, initseq)`: `state = 0; inc = (initseq << 1) | 1; next(); state += initstate; next()`.
- `next_u32()`: `old = state; state = old·6364136223846793005 + inc (mod 2⁶⁴); x = (((old >> 18) ^ old) >> 27) mod 2³²; r = old >> 59; return (x >> r) | (x << ((−r) & 31)) mod 2³²`.
- `below(n)` (n ≥ 1): `threshold = (2³² − n) mod n`; repeat `v = next_u32()` until `v ≥ threshold`; return `v mod n`.
- `next_u64() = (next_u32() << 32) | next_u32()` (high word drawn first); `random() = (next_u64() >> 11)·2⁻⁵³`.
- Streams: `initstate` = the run seed, `initseq` = 1 for slippage, 2 for `ctx.rng()`.
- **Per-day seeds** (`seed_mode`, VOL 5.1, D-66). With `seed_mode: run` (the default) both streams are seeded once from the run seed, as above. With `per_day`, each day's seed is the first 8 bytes (little-endian) of SHA-256 of the ASCII string `stockify-seed:<seed>:<YYYYMMDD>` (`<seed>` the run or session seed in decimal; `stockify.bundle.day_seed`), written into the bundle as `days[].seed`, the only source of a day's seed: neither engine derives it. Before the first event (or `on_start`) whose IST date is on or after a seeded day, both streams are re-created from that day's seed (`initseq` 1 and 2 as above); a day with no event of its own is passed over. So a run over a period draws, day by day, what one-day paper sessions draw. The top-level `seed` stays the run or session seed. Paper sessions (M5) run `per_day`.
- Both implementations must produce identical sequences (A3 P-01). No standard-library distributions (C-9).

**4.6 Price bands.** No fill outside the price band in force.

**4.7 Pre-open** behaviour is §3.10. Pre-open phases (F&O futures from 2025-12-08, X-6) are honoured only as "not continuous".

**4.8 Closing auction** (`cas_policy`; cash segment, CAS-eligible stocks, from 2026-08-03, X-7). At the `CAS` phase start, open DAY orders in those stocks are cancelled (`R_CAS`); new orders are rejected `R_CAS` (default `none`). `close_price` instead fills resting orders that would execute at the official close from end-of-day data, labelled approximate.

**4.9 `eod_close`** (for `bhav_eod` packs; label `eod_approx`). An order fills on the next eligible daily bar at that bar's **close** moved adversely by `half_spread_ticks`, capped by its limit and by the participation cap of the bar volume. (A decision made on day *d*'s published data therefore executes at day *d+1*'s close.)

## §5 Costs

**5.1** Charges are computed **per executed order per trade date** (the IST date of its fills): turnover = Σ fill price × quantity (premium turnover for options).

**5.2 Row matching.** For each component, a charge row matches when every key in its `applies` object matches the order (list values by membership) and its date range covers the trade date. Exactly one row per component may match; zero or several is a loader error.

**5.3 Components** (ppb rates, §0):
Amended by D-46 (Kite's contract note, captured 2026-11-06): components are **exact**, not rounded each. Work in units `E` = 10⁻¹⁸ paisa:
- Brokerage: `zero`; `flat` = `flat_paise·E`; `min_flat_pct` = `min(flat_paise·10⁹, turnover·pct_ppb)·10⁹`.
- STT (side-specific), exchange transaction (includes IPFT), SEBI fee: each `turnover·rate_ppb·10⁹` (exact).
- Stamp duty (buy side): rounded half-up to the whole rupee: `round_half_up_div(turnover·rate_ppb, 100·10⁹)·100·E`.
- GST: `(brokerage + exchange + sebi in paise·10⁹)·gst_ppb` (exact, on the unrounded parts).
- The order's total = `round_half_up_div(Σ components, E)` paise: the only rounding. `charges.csv` shows each component rounded half-up to the paisa for reading, so its columns can differ from `total` by a paisa or two; the ledger debits `total`.
- Non-order charges, each its own row in `charges.csv`: DP charge (per scrip per trade date with CNC sells; kind `dp`), the square-off fee plus GST on it (per `AUTO_SQOFF`/`RMS_SQOFF` order; kind `sqoff_fee`), exercise STT, exercise brokerage and physical-delivery charges (kind `settlement`, §7).

**5.4 Debit time.** When the order completes, or at `SEG_CLOSE` for orders cancelled with a partial fill; non-order charges when they arise.

## §6 Ledger

**6.1 Positions** are keyed by `(instrument_id, product)`. Average price = weighted average of the open quantity (`floor_div` of total cost by quantity, remainder tracked so realised P&L is exact). Reducing a position realises `(exit − entry)·qty` (sign-adjusted for shorts). Positions are in units; lot changes never alter them (C-7).

**6.2 CNC**: cash debited at the buy, credited at the sell (same-day availability per policy). Corporate actions are out of scope (I-5).

**6.3 MIS**: at the dated cut-off (`MIS_SQOFF`), the engine submits protected MARKET orders flattening every MIS position (tag `AUTO_SQOFF`), charging the fee when enabled. MIS orders after the cut-off are rejected `R_SESSION`.

**6.4 NRML derivatives.** Futures are marked at `SEG_CLOSE` to the last LTP (provisional; the cash difference moves to cash). When a `SETTLE` record with the contract's daily settlement price arrives, the engine re-marks to it as a separate ledger entry. Options: premium paid or received at the trade.

**6.5 Funds and margin.** `available = cash − blocked margin of positions − reserved amounts of open orders`. An order reserves at **submit** (so several orders placed in one callback cannot double-count funds) and releases on fill (replaced by the position's actual cash/margin effect), cancel or reject. Required amount, where `px` = the limit price, or the protection limit computed from the LTP known at submit for MARKET, or from the trigger for SL-M:

| Order | Required |
|---|---|
| Any order that only reduces an existing position | 0 |
| CNC buy | `qty·px` |
| CNC sell | 0 (holdings check `R_HOLDINGS`) |
| MIS (opening or increasing) | `round_half_up_div(qty·px·mis_margin_ppb, 10⁹)` |
| NRML futures (opening or increasing) | incremental margin-model requirement: `M(portfolio + order) − M(portfolio)`, floored at 0 |
| NRML option buy | `qty·px` (premium, X-10) |
| NRML option sell (opening or increasing) | incremental margin-model requirement |

Margin-model results are converted with `ceil_to(ceil(x), 100)` (§0.3). Charges are not reserved; if they push `available` below zero, risk-increasing orders are rejected `R_MARGIN` until it recovers. A margin model whose requirement grows over time (e.g. the expiry-week schedule, M4.7, D-60) can do the same. An order that only reduces an existing position is never rejected `R_MARGIN`, even in a shortfall, so a short account can always trade out of it. With `rms_square_off` enabled, positions are reduced at the policy time (`RMS_SQOFF`; not modelled while the shipped policy has no time).

**6.6 NAV** = cash + market value of CNC holdings + market value of option positions (long +, short −) + unrealised futures P&L since the last mark. Mark: `mark = ltp | mid` (run config); the last known value is used when no new data arrived.

**6.7 Day outputs** (at `DAY_END`): one `positions_eod` row per open position, one `equity` row per trading day.

## §7 Expiry and settlement

**7.1** Expiring contracts stop trading at their last trading moment (the group's close on the expiry date) and become "awaiting settlement".

**7.2 Index futures and options** settle in cash when the `SETTLE` record with the final settlement price (FSP) of the underlying for that expiry arrives (D-13): futures at `(FSP − last mark)·qty`; ITM options at intrinsic `|FSP − K|·qty`; OTM options at 0. Long ITM options pay exercise STT on intrinsic value; every option position open at expiry (exercised, assigned or expired) pays the expiry brokerage (`exercise_brokerage` in `charges.json`, D-59). If no FSP has arrived by `DAY_END` of the expiry date, settle at the last underlying price seen (index value, or the underlying's bar close) and label the run `settlement_fallback` (I-14); if none was ever seen, the run fails.

**7.3 Stock F&O** settle physically (M4.6, Z-4, D-58). When the stock's FSP arrives (the `SETTLE` record for the stock on the expiry date), or at the §7.2 fallback, every expiring position of that stock and expiry, taken in `(instrument_id, product)` order:
1. **closes**: a future is marked to the FSP (variation, §6.4); an option closes at price 0, so its premium is its whole P&L (an OTM option ends here);
2. **becomes a delivery leg** in the stock, product CNC: a long future, long ITM call or short ITM put *takes* (buys) its units; a short future, short ITM call or long ITM put *gives* (sells) them; futures at the FSP, options at the strike;
3. **nets**: with T units taken and G units given in that stock and expiry, N = min(T, G) units are netted. Take legs claim N in order, and so do give legs; a leg's claimed units are *netted*, the rest *delivered*;
4. **is booked** as a CNC trade of the stock at its price, through the ordinary CNC path: its cash follows the CNC credit rules of §6.2 and §6.5 (a give's sale proceeds are usable when any CNC sale's would be), not a separate delivery-date calendar. A give larger than the CNC holding leaves a short CNC holding and labels the run `short_delivery` (the NSE auction and close-out penalty are not modelled);
5. **is charged** in one `settlement` row (`physical_delivery` in `charges.json`): STT of 0.1% of the leg's value (units × price) for both sides, plus exercise STT (§7.2) on the intrinsic value of a long ITM option; brokerage of 0.25% of the delivered value plus 0.1% of the netted value; GST on that brokerage. No exercise brokerage, exchange, SEBI or stamp charges;
6. is logged `delivered <stock id> CNC <BUY|SELL> <units> at <price> (<source>)`, after the `settled …` lines.
Delivery margins before expiry come from the margin model (M4.7).

## §8 Metrics

Computed by both implementations with these formulas, in this expression order (§0.3). Let `NAV_0` = starting capital and `NAV_1 … NAV_T` the daily NAV rows (paise, converted to `double` once).
- Daily return `r_d = NAV_d / NAV_{d−1} − 1`; excess `x_d = r_d − rate_d / 252`, where `rate_d` is the risk-free rate row in force on day d.
- `mean = (Σ x_d) / T`; `sd = sqrt(Σ (x_d − mean)² / (T − 1))` (sample, ddof = 1); **Sharpe** `= mean / sd · sqrt(252)`; null when `T < 2` or `sd = 0`.
- **Sortino** `= mean / sqrt(Σ min(x_d, 0)² / T) · sqrt(252)`; null when the denominator is 0.
- **Volatility** = sample sd of `r_d` × sqrt(252). **CAGR** `= (NAV_T / NAV_0)^(252 / T) − 1`.
- **Max drawdown**: running peak over `NAV_0 … NAV_T`; drawdown `= 1 − NAV_d / peak`; max drawdown = its maximum; duration = the longest run of trading days from a peak until NAV first gets back to it (or to the end). **Calmar** `= CAGR / max drawdown` (null if 0).
- **Trades**: closing fills matched FIFO against opening fills per `(instrument, product)`; trade P&L is gross (charges excluded). Win rate = share of trades with P&L > 0; profit factor = gross wins / |gross losses| (null without losses); average trade.
- **Exposure** = continuous-phase seconds with any open position / total continuous-phase seconds. **Turnover** = (Σ traded value / mean NAV) × 252 / T. Costs by component and as a share of gross P&L; rejects by code; maximum participation used.

Undefined metrics are `null`, never NaN.

## §9 Outputs and determinism

**9.1 Deterministic artifacts** (A1 §6.3): `orders.csv`, `order_events.csv`, `fills.csv`, `charges.csv`, `positions_eod.csv`, `equity.csv`, `margin_daily.csv`, `metrics.json`, `events_log.txt`. `determinism.json` holds the SHA-256 of each. `manifest.json` (wall-clock duration, host) is outside the hashed set.

**9.2 Formatting.** CSV: fixed column order, `\n` line ends, UTF-8, enums spelled as in this file (`BUY`, `CNC`, `SL-M`, `COMPLETE`, `R_QTY_LOT`, `AUTO_SQOFF`, …). CSVs contain only integers, enums and plain strings, never floats. Floats appear only in `metrics.json`, written by our own JSON writer (sorted keys, no spaces) using one float format in both implementations: the shortest round-trip digits laid out like Python's `repr` (fixed notation for 1e-4 ≤ |x| < 1e16, otherwise `d.ddde±XX`; a trailing `.0` on integral values). The C++ formatter is tested against Python's `repr` on 10⁶ random doubles (A3 U-11).

**9.3 Comparisons.** Same implementation, same platform: every deterministic artifact byte-identical. Oracle vs engine: CSVs and `events_log.txt` byte-identical; `metrics.json` values equal within a relative 1e-12 (Python's and MSVC's math libraries may differ in the last bit of `pow`/`exp`); `margin_daily.csv` within 100 paise per row for SPAN-lite (M4.8).
