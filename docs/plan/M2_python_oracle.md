# M2 — Python oracle backtester

> **Goal**: a small, readable, single-threaded Python backtester that implements the engine semantics of `A4_engine_semantics.md` exactly. It is (a) the owner's research tool from now on for daily and minute bars and end-of-day option chains, and (b) the reference the C++ engine must match in M3 (C-10).
> **Depends on**: M1.1–M1.4 and M1.8. **Unblocks**: M3. **Size**: M.
> **Demo**: an SMA crossover on daily bars of RELIANCE and an end-of-day NIFTY short straddle on bhavcopy data, both producing a run folder and a one-page report.

Concepts: C-3, C-9, C-10, C-11, C-16. Semantics: `A4_engine_semantics.md` (the contract both implementations follow). Schemas: `A1_schemas.md` §6.

**Design rule**: clarity beats speed. No vectorisation inside the event loop, no clever caching. If the oracle and the engine disagree, the oracle must be the one a human can read and check by hand.

---

## M2.1 Run config and bundle resolver (M)

Files: `stockify/runconfig.py`, `stockify/bundle.py`. Both runners (oracle now, C++ in M3, paper sessions in M5) use these.

- [x] Parse and validate `run.json` (`A1_schemas.md` §6.1): strategy, data (pack specs or hashes), fill model, costs, margin model, capital, seed, price basis, latency, session policies, trial family.
- [x] `resolve_bundle(run_config) -> bundle.json` (`A1_schemas.md` §6.2, D-22): the trading days in range with their resolved phases in UTC ns; every instrument referenced by the packs with its dated contract specs and underlying link; the charge-schedule rows, broker-policy rows, margin and pricing parameters effective on any day in range; pack paths and hashes; strategy reference and source hash. The bundle is self-contained: the engine needs nothing else besides the packs.
- [x] Validation errors (fail before running): mixed price bases (I-5); a pack hash mismatch; charge rows with `verified: false` without `allow_unverified` (and when allowed, the run is labelled, I-14); an instrument without specs on a day it trades (equities: D-43); a strategy whose source cannot be hashed.
- [x] The bundle is written to the run folder and hashed; the hash goes into the manifest (I-8).

## M2.2 Oracle engine (M)

Package `stockify/oracle/`: `events.py` (iterates a pack through the numpy reader from M1.8), `clock.py`, `orders.py`, `matcher.py` (fill models), `costs.py`, `ledger.py`, `margin.py` (T0 flat only), `settlement.py`, `metrics.py`, `artifacts.py`, `runner.py`.

- [x] Event processing exactly per `A4` §1–§2: one `(t, rank, src, seq)` ordering for input and internal events, the batch-boundary rule, session phases from the bundle, timers, `SEG_CLOSE`/`DAY_END`, market-state evidence (ΔV, the validity-checked interval VWAP), and bars built from snapshots.
- [x] Orders and the state machine per `A4` §3 (MARKET with protection, LIMIT, SL, SL-M; DAY and IOC; CNC, MIS, NRML; validations; rejects with reason codes).
- [x] Fill models per `A4` §4: `bar`, `snapshot_ltp`, `snapshot_quote`, `eod_close`, with the §4.0 eligibility rules, participation caps, and adverse slippage from the seeded generator of `A4` §4.5 (the oracle implements the same PCG32 bit for bit, so both draw identical numbers).
- [x] Costs per `A4` §5, reading the bundle's charge rows: per executed order, per component, integer ppb arithmetic through `stockify.numerics`, rounded with the documented rule.
- [x] Numeric discipline (A4 §0.3): no `sum()`, `math.fsum`, numpy or pandas reductions in anything that produces an artifact; floats only where A4 allows them; formulas in the exact expression order given.
- [x] Ledger per `A4` §6: cash, positions (units), average price, realised and unrealised P&L, MIS square-off at the dated cut-off with the optional fee, CNC holdings, end-of-day marks.
- [x] **F&O-lite** (enough for end-of-day index option research before M4): index futures and options with premium accounting, T0 flat margin, cash settlement at the final settlement price from `SETTLE` events, exercise STT. Stock F&O (physical settlement) is out of scope for the oracle until M4.6.
- [x] Artifacts in exactly the layout of `A1_schemas.md` §6.3 (same file names, columns, number formatting) so `compare-artifacts` can diff oracle and engine runs.

## M2.3 Python strategy API (S)

File: `stockify/strategy.py`. Interface: `A2_interfaces.md` §4.

- [x] Base class `Strategy` with hooks `on_start`, `on_bar`, `on_tick`, `on_timer`, `on_fill`, `on_order_update`, `on_session_phase`, `on_session_end`, `save_state`, `load_state`.
- [x] `Context` protocol: time, subscriptions, last snapshot / depth / bars, indicators (SMA, EMA, RSI and ATR with Wilder smoothing, session VWAP), orders, positions, funds, timers, parameters, logging, `rng()`.
- [x] The oracle provides one `Context` implementation; the C++ engine provides another in M3.8. **Strategies written now must run unchanged on the engine later**; checked in M3 by running every reference strategy on both (done, M3.10: `tests/stockify/test_engine_strategies.py`).

## M2.4 Reference strategies (S)

Files: `stockify/strategies/`.

- [x] `buy_and_hold` (benchmark for every equity test).
- [x] `sma_cross` (fast/slow on daily or minute bars; long-only and long-short variants).
- [x] `opening_range_breakout` (minute bars; MIS; exit by time or stop).
- [x] `eod_short_straddle` (`bhav_eod` dataset, `eod_close` fill model, A4 §4.9): after each day's file is published, decide whether to sell the ATM call and put of the nearest NIFTY weekly; the orders fill at the next day's close moved adversely by `half_spread_ticks`; hold to expiry (cash settlement at the FSP) or exit when a stop measured on daily closes is hit. Labelled `eod_approx` (I-14). This gives option research a first answer from free data while the recorder accumulates intraday history.

## M2.5 Golden scenarios (M)

Format and list: `A3_testing.md` §2–§3.

- [x] Golden runner `python -m stockify golden run --impl oracle` loads every `tests/golden/*.yaml`, builds a tiny pack and bundle from the YAML, runs, and compares artifacts to the expected values in the YAML.
- [x] Write G01–G10, G12, G14, G16, G19 and G20 *(2026-11-06: all written and passing, G08 included)* with hand-computed expectations (G11 waits for M4.6, G17–G18 for M4.3; G13 and G15 are data-layer tests written in M1). Each YAML explains its expected numbers in a `notes` block, step by step, so a human can audit them.
- [x] G08 (costs) uses ground truth *(2026-11-06: captured by the owner and passing: 11 cases, every component within ₹0.000001 and every total to the paisa, after D-46)* from Kite's virtual contract note (K-10): `python -m stockify tools capture-charges --cases tests/golden/charges_cases.json` calls `POST /charges/orders` for a list of imaginary orders and saves the responses (no secrets) to `tests/fixtures/kite_charges/<date>.json`. 🛑 The owner runs the capture, since it needs a live access token. Re-capture whenever charges change, and keep old captures with their dates.

## M2.6 Metrics and run report (S)

- [x] Metrics per `A4` §8 (C-16): NAV series, daily returns, CAGR, volatility, Sharpe (√252), Sortino, max drawdown and duration, Calmar, trades, win rate, profit factor, average trade, exposure, turnover, total costs by component, cost share of gross P&L, rejects by reason, maximum participation used.
- [x] `report.md` per run: headline metrics, labels (approximations, unverified charges), equity-curve PNG (matplotlib), cost breakdown, and the manifest summary.

## M2.7 CLI and notebook use (S)

- [x] `python -m stockify oracle run --config run.json` → run folder path.
- [x] `from stockify.oracle import run; res = run(config_dict)` returns a `RunResult` with pandas DataFrames (orders, fills, equity, metrics) for notebooks.
- [x] `python -m stockify compare-artifacts <runA> <runB> --tol-paise 0` diffs two run folders (used heavily in M3).

---

## Verification

```
.\.venv\Scripts\python.exe tests\run_tests.py
python -m stockify golden run --impl oracle            # all golden scenarios pass
python -m stockify oracle run --config examples/runs/sma_reliance_daily.json
python -m stockify oracle run --config examples/runs/eod_straddle_nifty.json
python -m stockify oracle run --config examples/runs/sma_reliance_daily.json   # second run: identical artifact hashes
```

## Definition of done

Golden scenarios pass; two identical runs give identical artifact hashes; the two demo runs produce reports the owner has read; every boxed task ticked.

**Status 2026-11-06: done.** All 15 goldens pass (G08 against the owner's Kite capture); repeated runs are byte-identical; the owner read both demo reports. The one open box (the same strategies on the C++ engine) is M3's check.
