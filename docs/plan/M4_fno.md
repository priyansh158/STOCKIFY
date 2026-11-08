# M4 — Futures and options

> **Goal**: futures and options behave in the engine (and the oracle) the way they do at NSE and Zerodha: dated contract specs, Black-76 pricing and implied volatility, option-chain views, cash and physical settlement with the right charges, and margin models from flat to SPAN-lite, calibrated against Kite.
> **Depends on**: M3 (and recorded chains from M1.5). **Unblocks**: M5 option sessions, M6. **Size**: L.
> **Demo**: an intraday short straddle and an iron fly replayed on recorded NIFTY chains with quote-based fills and SPAN-lite margin; the report shows the iron fly's much smaller margin and the calibration error against Kite.

Concepts: C-2, C-7, C-12, C-13, C-15. Contract: `A4_engine_semantics.md` §6–§7. Facts: `01_facts.md` §2–§3.

**Order of work for every feature below**: write the readable Python version in the oracle first, then the C++ version, then make both pass the same goldens (C-10).

---

## M4.0 Recon and fact check (S)

- [x] 🛑 Verify every ⚠ row this milestone depends on and record evidence in `01_facts.md`: X-4 (2024 lot values), X-5 (other indices on 2026-10-05), X-9 (stock calendar-spread benefit), X-10 (upfront premium), X-12 and X-13 (ELM and SPAN files), Z-3 (transaction-charge history), Z-5 (CAS-era MIS cut-off), S-7 (public URLs). Stop if any changes a rule below.

## M4.1 Derivative contract model (S)

- [x] Bundle and engine carry, per derivative: kind (`FUT`, `CE`, `PE`), underlying and underlying class (`INDEX` / `STOCK`), expiry date, strike, settlement type (cash for index, physical for stock, dated in `contract_rules.json`), last trading moment (the FO close on the expiry date from `sessions.json`: 15:30 IST before 2026-08-03, 15:40 IST after, X-7).
- [x] Freeze limits per underlying, dated: from NSE circulars (X-5) entered into `freeze_limits` (A1 §1.2), with `qtyfreeze.csv` (S-6) only as a cross-check because it lags. Slices use `floor(freeze / lot) × lot` units, since an order above the freeze quantity is rejected and every order must be a whole number of lots (A3 G16).

## M4.2 Expiries and rollover helpers (S)

- [x] `ctx.expiries(underlying, kind)` and `ctx.futures_chain(underlying)` (sorted by expiry) from the bundle.
- [x] Python helpers (not engine rules) in `stockify/fno/roll.py`: `days_to_expiry`, `roll_due(contract, trading_days_before)`, `next_contract(contract)`.

## M4.3 Black-76 and implied volatility (M)

Files: `engine/include/stocksim/pricing/black76.hpp`, `stockify/fno/black76.py` (independent implementation, C-10).

- [x] Price, delta, gamma, vega, theta, rho per C-13; normal CDF via `erfc` (`std::erfc` / `math.erfc`).
- [x] IV: no-arbitrage bound check first (return NaN + reason: `BELOW_INTRINSIC`; `ABOVE_FORWARD` for calls above `D·F`; `ABOVE_STRIKE` for puts above `D·K`; `BELOW_TICK` when the price is under one tick), then Brent's method on σ ∈ [0.001, 5.0] to 1e-10 absolute in price, falling back to bisection; flag `LOW_VEGA` when vega is too small for a stable answer.
- [x] Time to expiry: ACT/365 from `now` to the contract's last trading moment (M4.1), floor at one second.
- [x] Rate from `pricing.json` (dated; a placeholder until the owner sets it from current T-bill yields).
- [x] Tests: A3 G17 (reference vectors, including Hull's textbook Black-76 example) and G18 (round trip over a grid, with sub-tick points flagged), using the single tolerance table in `A3_testing.md` §3.

## M4.4 Option-chain view (M)

- [x] `ctx.chain(underlying, expiry)` returns, per strike and side: bid, ask, mid, LTP, staleness, OI, IV at bid/ask/mid, delta, gamma, vega, theta; plus the forward `F`, its source, and the ATM strike.
- [x] Forward: the same-expiry future's mid when it exists; otherwise the synthetic forward (C-13) — the median of `K + (C_mid − P_mid)/D` over the 3–5 strikes nearest the money with both sides quoted and not stale; with fewer than two usable strikes, the index value, labelled `forward_from_index`.
- [x] Computed lazily and cached per engine time step; deterministic (no unordered iteration).

## M4.5 Index derivatives at expiry (S)

- [x] Extend M3.8's cash settlement: exercise STT on long ITM options (0.15% of intrinsic from 2026-04-01, 0.125% before; dated in `charges.json`), brokerage on exercised/expired contracts per policy, `settlement_fallback` label (A4 §7.2).
- [x] A3 G10 passes on both implementations.

## M4.6 Stock F&O physical settlement (M)

- [x] A4 §7.3 with Z-4: at expiry, futures and ITM options become CNC holdings (futures at the settlement price, options at the strike, option P&L exit price 0); STT 0.1% of contract value for both sides plus exercise STT; brokerage 0.25% of delivery value, 0.1% for positions netted in the same stock; cash moves on the delivery date.
- [x] Netting: equal take and give quantities in the same stock and expiry produce no delivery.
- [x] A3 G11 passes on both implementations.

## M4.7 Margin T0 completed (S)

- [x] Flat model additions: long options = premium only (X-10); +2% expiry-day ELM on short index options expiring that day (X-9: notional = underlying price × quantity, Zerodha's formula, decided 2026-11-07; confirm once with Kite basket margins on an expiry day); the stock F&O delivery-margin schedule of Z-4 from `broker_policy.json` (holiday shift included).
- [x] RMS behaviour on shortfall (A4 §6.5): reject risk-increasing orders; optional `RMS_SQOFF` at a policy time.

## M4.8 Margin T1: SPAN-lite (L)

Files: `engine/src/margin/spanlite.cpp`, `stockify/fno/spanlite.py`. Parameters: `margin_params.json` (A1 §7).

- [x] Each leg's IV: from its current mid quote; if it has no usable quote, its last valid IV today; otherwise the ATM IV of the same expiry; otherwise `default_iv` from `margin_params.json` (the run is labelled when either fallback is used).
- [x] Per underlying, revalue the whole position (futures linearly, options with Black-76 at each leg's IV) under 16 scenarios (C-12): price moves {0, ±⅓, ±⅔, ±1} × PSR, each with volatility up/down by VSR; plus two extreme moves ±`extreme_multiple` × PSR counted at `extreme_weight` (defaults 3 and 0.35 from the classic SPAN definition, ⚠ verify against NSE's risk-parameter file when available).
- [x] Scanning risk = worst scenario loss (≥ 0). Short-option minimum = `somc_per_lot × short lots`. SPAN-lite = max(scanning risk, short-option minimum).
- [x] Exposure/ELM: `elm_pct × notional` for futures and short options (index and stock defaults, dated; the per-contract ELM file replaces them when S-7 is verified); +2% expiry-day add-on (X-9).
- [x] Total = Σ underlyings (no inter-commodity credit in v1), converted to paise by rounding **up** to the next whole rupee (A4 §0.3). The breakdown (scan, SOMC, ELM, add-ons) is recorded per day in `margin_daily.csv`. Within one platform the oracle and the engine call the same C math library and must agree exactly; across Windows and Linux the totals may differ by up to 100 paise (A4 §9.3), so parity scenarios avoid margin totals sitting exactly at a funds threshold.
- [x] Label: `margin_spanlite` (approximate, I-14), with the calibration id used.

## M4.9 Calibration against Kite (M)

Tool: `python -m stockify margin calibrate --underlying NIFTY --samples 200`. Table: `margin_calibration` (A1 §4).

- [x] Sample portfolios from the current chain: single legs across moneyness and expiries, straddles, strangles, verticals, iron flies and condors, futures, futures hedged with options.
- [x] For each, call `POST /margins/basket?consider_positions=false` (K-10; ≤ 10 requests/s, K-8) and store Kite's `initial`, `final` and per-order components with the market snapshot used; compute T0 and T1 for the same snapshot.
- [x] Fit PSR and VSR (and `somc_per_lot`) per underlying by minimising relative error on `final`. Report median and 90th-percentile absolute relative error by portfolio type.
- [x] Write the fitted values as a **new dated row** in `margin_params.json` with `source: "calibration <date>"`. 🛑 The owner reviews the error report before the row is adopted.
- [ ] Re-run monthly and after any NSE margin circular (e.g. X-11), keeping history.

*(The tool is built and tested against a fake Kite client (IWB 4.1); its first real run is the owner's, during market hours. The fit goes to `margin_params.json` → `spanlite_pending` and is adopted only by moving it into `spanlite` after review.)*

## M4.10 Margin T2: exact SPAN (stretch, L)

- [ ] 🛑 Only after S-7 is verified (public URL and format of the SPAN risk-parameter files) and only if T1's calibrated error is unacceptable for the owner's strategies.
- [ ] Parse the SPAN 4.00 risk-parameter file; use its per-contract risk arrays, spread charges, short-option minimums and net option value rules; validate against Kite basket margins.

## M4.11 Futures pre-open and freeze slicing (S)

- [x] The not-continuous policy (A4 §3.10) covers futures in the FO pre-open phase from 2025-12-08 (X-6) and options before their open; G20 already tests the rule with its own bundle, so here re-run it against the real `sessions.json`.
- [x] Freeze slicing (A4 §3.8) with the real dated limits from M4.1; re-run G16 against them (the golden itself carries its own limits and passes from M2/M3).

## M4.12 Replaying recorded chains (S)

- [x] Chain selectors in pack specs (M1.8) and `REFDATA` events for strikes subscribed mid-day; strategies see staleness and missing strikes and never get fabricated quotes (I-7).

## M4.13 Reference option strategies (M)

- [x] `short_straddle_stop`: at a configured time, sell the ATM call and put (ATM from the chain view's forward); per-leg stop at `x%` above entry premium (SL orders); time exit; quote-based fills; SPAN-lite margin. (Margin is the run's `margin.model`; flat T0 until M4.8 lands.)
- [x] `iron_fly`: the same plus long wings `w` strikes away, to show the margin and risk difference.
- [x] A comparison report: the same days run as `eod_short_straddle` (M2.4, bhavcopy) and as the intraday version on recorded chains, side by side, with their labels. The gap between them is a direct measure of how much end-of-day approximations distort option results. *(Tool: `python -m stockify tools compare-strategies`; tested on chain worlds. The real-data run, `examples/runs/straddle_vs_ironfly_recorded.json` on ≥ 5 recorded days, is pending going live.)*

## M4.14 UI additions (S)

- [x] `/tests/[id]`: margin breakdown per day, Greeks of open positions at end of day, settlement events, labels.

---

## Verification

```
python -m stockify golden run --impl oracle && python -m stockify golden run --impl engine   # incl. G10–G12, G16–G18
ctest --test-dir engine/build/win-msvc-release -C Release
python -m stockify run --config examples/runs/straddle_vs_ironfly_recorded.json
python -m stockify margin calibrate --underlying NIFTY --samples 50      # owner, market hours
```

## Definition of done

Goldens G10–G12 and G16–G18 pass on both implementations; SPAN-lite calibrated for NIFTY and BANKNIFTY with an error report the owner accepted; the straddle vs iron-fly demo runs on at least five recorded days.
