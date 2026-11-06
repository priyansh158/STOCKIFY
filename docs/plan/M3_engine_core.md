# M3 — C++ engine core (equity) and test runs

> **Goal**: the deterministic C++ engine (`stocksim_core`) implementing `A4_engine_semantics.md`, usable from Python (`stockify._engine`) with Python or C++ strategies, plus the `/tests` API and UI.
> **Depends on**: M2 (oracle, bundle resolver, goldens). **Unblocks**: M4, M5. **Size**: L.
> **Demo**: the same SMA run (a Python strategy) through the oracle and the engine gives byte-identical artifacts; a tick replay of a recorded day runs in seconds; a test started from `/tests` on a phone shows metrics, equity curve and fills.

Concepts: C-3, C-8, C-9, C-10, C-11, C-16. Contract: `A4_engine_semantics.md`. Interfaces: `A2_interfaces.md` §1–§5. Formats: `A1_schemas.md` §5–§6.

**Rule for this milestone**: implement A4 section by section, and after each section run the golden scenarios that cover it on both implementations. Never "fix" a disagreement by changing the oracle to match the engine without first deciding, from A4, which one is right.

---

## M3.0 Recon (S)

- [x] Confirm M2 is complete: goldens pass on the oracle, bundles resolve, packs build. Confirm the engine still builds (`run.ps1 -BuildEngine`; Windows only, D-26).
- [x] Read `A2_interfaces.md` §1–§5 and list any header that conflicts with the repo; record decisions in `RECON.md`.

## M3.1 Foundations (S)

Files: `engine/include/stocksim/{types,int128,decimal,pcg32,sha256,format,json_out,json_bundle}.hpp`, matching `src/*.cpp`.

- [x] `types.hpp`: `Paise`, `Qty`, `TimeNs`, `Ppb`, `Ppm`, `Bp` (all integers), `InstrumentId`, `OrderId`, enums for side, product, order type, validity, status (incl. `Sliced`), reasons (A4 §3).
- [x] `int128.hpp`: the integer helpers of A4 §0.2 with 128-bit intermediates, for MSVC (D-26); tested against Python on random inputs.
- [x] `decimal.hpp`: exact decimal-string → ppb/ppm/bp/paise conversion; rejects non-integral results; **never via `double`** (A4 §0.1).
- [x] `pcg32.hpp`: PCG32 exactly as A4 §4.5 (seeding, `next_u32`, `below`, `next_u64`, `random`); golden sequence test shared with the oracle (A3 P-01).
- [x] `sha256.hpp`: wrapper over vendored picosha2.
- [x] `format.hpp` / `json_out.hpp`: CSV writer (fixed columns, `\n`, integers and enums only) and the canonical float format + JSON writer of A4 §9.2, tested against Python's `repr` (A3 U-11).
- [x] `json_bundle.hpp`: load `bundle.json` (A1 §6.2) with nlohmann/json; money and rates in the bundle are already integers (paise, ppb, ppm, bp).

## M3.2 Event packs and sources (S)

Files: `event.hpp`, `pack_reader.hpp/.cpp`, `source.hpp`.

- [x] Memory-mapped reader for `.evt` (A1 §5) behind a tiny platform layer (`MapViewOfFile`; Windows only, D-26). Validate magic, version, record sizes and non-decreasing `(t_event, rank, seq)`; fail with the byte offset on any violation.
- [x] `EventSource` interface; `PackSource` (one or many packs merged by sort key), `VectorSource` (tests), `LiveBatchSource` (accepts encoded batches pushed from Python; used in M5).
- [x] Round-trip test: the Python builder (M1.8) writes a pack, the C++ reader decodes it, and a C++ re-encoder produces identical bytes.

## M3.3 Event loop, clock, phases, timers (M)

Files: `engine.hpp/.cpp`, `session.hpp/.cpp`, `timers.hpp`.

- [ ] One priority queue keyed `(t, rank, src, seq)` merging input records (several packs by pack index) with internal events: `PHASE`, `BAR_BUILT`, `MIS_SQOFF`, `TIMER`, `SEG_CLOSE`, `DAY_END` (A4 §1.2–§1.3, §1.8–§1.10).
- [ ] `Engine::run(EventSource&)`, `Engine::push(batch)`, `Engine::finish()` share one code path with the batch-boundary rule of A4 §1.4, so nothing depends on how input was chunked (property test A3 P-02, including boundaries at timer, phase and bar-end times).
- [ ] Bars built from snapshots (A4 §2.2) for strategies that subscribe to bars on tick data.
- [ ] CLOCK events only advance time (A4 §1.6; property test P-03).
- [ ] G14 (dated F&O close: 15:30 vs 15:40) passes on the engine.

## M3.4 Instruments and specs (S)

- [x] Build an `InstrumentTable` from the bundle: static fields, dated specs (lot, tick, freeze) per trading day, underlying links, price bands from `REFDATA` events.
- [ ] `spec_on(instrument, date)` is O(1) per day (pre-resolved arrays). Test against G12 (mid-life lot change).

## M3.5 Orders (M)

Files: `orders.hpp/.cpp`.

- [ ] Validation in A4 §3.2 order with reject codes; market protection conversion (§3.3); stop triggers (§3.4); state machine (§3.5); modify/cancel with latency (§3.6); IOC (§3.7); freeze slicing (§3.8).
- [ ] Every transition is logged into the order-events stream that becomes `orders.csv` (one row per order with final state) and `order_events.csv` (every transition with engine time).
- [ ] Unit tests for each reject code, each type conversion, and latency races (fill before cancel takes effect).

## M3.6 Fill models (L)

Files: `fills/bar.cpp`, `fills/snapshot_ltp.cpp`, `fills/snapshot_quote.cpp`, `fills/common.hpp`.

- [ ] The eligibility rules of A4 §4.0 in one place, used by every model (property test P-07).
- [ ] `bar` per A4 §4.1, including the two-path pessimistic choice by end-of-bar NAV (with RNG and ledger copies) and the participation cap; `eod_close` per A4 §4.9.
- [ ] `snapshot_ltp` per A4 §4.2 using the §2 evidence (`ΔV`, interval VWAP, new day extremes).
- [ ] `snapshot_quote` per A4 §4.3 walking recorded depth; stale-book rule; no depth ⇒ no fill.
- [ ] Slippage per A4 §4.4 from PCG32 stream 1.
- [ ] Not-continuous and closing-auction policies (A4 §3.10, §4.7–§4.8).
- [ ] Goldens G01–G07, G16, G19 and G20 pass on the engine; unit tests for each model's edge cases (gap through stop, limit exactly at the low with `touch` vs `through`, `ΔV = 0`, empty book side, stale book).

## M3.7 Costs (M)

Files: `costs.hpp/.cpp`.

- [ ] Charge rows from the bundle, selected by (segment, product, trade date). Per-order aggregation and component rounding per A4 §5. DP charge for CNC sell days; auto-square-off fee.
- [ ] G08 (Kite virtual contract note fixtures) and G09 (STT switch on 2026-04-01) pass on both implementations.
- [ ] `charges.csv`: one row per charged order with every component, so a contract note can be compared line by line.

## M3.8 Ledger, T0 margin, MIS, end of day (M)

Files: `ledger.hpp/.cpp`, `margin_flat.cpp`.

- [ ] Positions, average price, realised P&L, cash, NAV (A4 §6); funds reservation at submit with the required-amount table of A4 §6.5. MIS square-off at the dated cut-off with `AUTO_SQOFF` orders that bypass the session/margin checks, plus the optional fee (G04). `SEG_CLOSE` (DAY-order expiry, provisional futures marks) and `DAY_END` (settlement fallback, `positions_eod`, the single daily NAV row, `on_session_end`) per A4 §1.10 and §6.7; re-marks when daily settlement prices arrive (A4 §6.4).
- [ ] `MarginModel` interface (A2 §2) with `FlatMargin` (T0, D-14): MIS `mis_margin_pct`; NRML futures and short options `pct × notional` by underlying class (index / stock) from `margin_params.json`; labelled approximate (I-14).
- [ ] Index options/futures cash settlement per A4 §7.2 (needed for G10; physical settlement is M4.6).

## M3.9 Metrics and artifacts (S)

- [ ] Metrics per A4 §8; artifact files and `determinism.json` per A4 §9 and A1 §6.3.
- [ ] Determinism tests (A3 DT-01, DT-03): run twice and re-run from the folder, compare hashes; DT-02 compares oracle and engine with the tolerances of A4 §9.3.

## M3.10 Strategy API, registry and Python trampoline (L)

Files: `strategy.hpp`, `context.hpp/.cpp`, `indicators.hpp`, `engine/strategies/*.cpp`, `engine/bindings/module.cpp`. Interface: `A2_interfaces.md` §3–§4.

- [ ] C++ `Strategy` base class and `Context`; indicators (SMA, EMA, RSI and ATR with Wilder smoothing, session VWAP) identical in definition to the oracle's (golden indicator vectors, A3 U-05).
- [ ] Registry: `STOCKSIM_STRATEGY(ClassName, "name")`; C++ reference strategies `sma_cross_cpp` and `orb_cpp` (port the Python ones to prove the native path).
- [ ] pybind11 module `stockify._engine` exactly as `A2_interfaces.md` §3: `Engine(bundle_path)`, `add_pack(path)`, `set_strategy_cpp(name)`, `set_strategy_py(obj)`, `load_sod_state(json)`, `run()`, `push(bytes)`, `finish()`, `drain_outputs()` (new orders, order events, fills, charges and logs since the last call, for M5), `eod_state()`, `write_artifacts(dir)`, `labels()`, plus module-level `build_info()`.
- [ ] Python strategies: a trampoline subclass forwards hooks to the Python object; `Context` methods are exposed with the same names and semantics as the oracle's `Context` (M2.3). Python exceptions inside a hook stop the run with the traceback in `events_log.txt` and status `failed` (never swallowed).
- [ ] Every M2 reference strategy (Python) runs unchanged on the engine and matches the oracle.

## M3.11 Runners and import (M)

Files: `stockify/runs.py`, `stockify/__main__.py` (`run`), `engine/tools/stocksim_run.cpp`.

- [ ] `python -m stockify run --config run.json`: resolve the bundle (M2.1), build or locate packs (M1.8), run through `stockify._engine`, write the run folder (A1 §6.3), import summary rows into `lab.sqlite` (A1 §4). This is what the API launches as a subprocess (I-9).
- [ ] `stocksim_run --bundle <run>/bundle.json --out <dir>`: C++-registered strategies only; for benchmarks and CI.
- [ ] Re-run from a folder: `python -m stockify run --rerun <run_id>` uses the archived bundle and packs and must reproduce `determinism.json` (I-8).

## M3.12 Parity with the oracle (M)

- [ ] `python -m stockify golden run --impl engine` passes all goldens written so far.
- [ ] Differential fuzzing (A3 P-04): generate random small event streams (seeded) plus a random-order strategy (seeded) and compare oracle vs engine artifacts with `compare-artifacts --tol-paise 0`. 200 seeds in the regular test run; 5,000 in a weekly job. Every failure is minimised (shrink the stream) and saved as a new golden.

## M3.13 Performance sanity (S)

- [ ] Benchmark: replay one recorded day (~300 instruments) with (a) a no-op C++ strategy, (b) a no-op Python strategy subscribed to everything, (c) a Python strategy on 10 instruments. Record events/second in `RECON.md`. Soft targets: (a) well over 1M events/s; (c) a full day in under a minute. If (b) is slow, that is expected (one Python call per event); document it rather than optimise early.

## M3.14 Test-run API and UI (M)

Server: `server/routers/tests.py`; DTOs in `server/schemas.py`, mirrored in `web/src/lib/types.ts`. Endpoints: `A2_interfaces.md` §6.

- [ ] `GET /api/strategies` (registered C++ strategies and discovered Python strategies with their parameter schemas, for the form). `POST /api/tests` validates and launches `python -m stockify run` as a subprocess (queue; at most `cpu_count − 1` concurrent runs); `GET /api/tests`, `GET /api/tests/{id}` (+ `/orders`, `/fills`, `/equity`, `/charges`, `/logs`), `POST /api/tests/{id}/cancel`. Declare fixed routes before `/{id}` routes (A2 §6).
- [ ] `/tests`: run cards (status, strategy, range, headline metrics, **labels**: approximate fills, flat margin, unverified charges, adjusted basis) and a "New test" form (strategy, parameters, symbols or chain selector, range, fill model, price basis, capital, seed).
- [ ] `/tests/[id]`: metrics, responsive equity curve, drawdown, fills list, cost breakdown, manifest (build id, hashes). Mobile-first: single column, tap targets ≥ 44 px, readable at 390 px width; ₹ via `Intl.NumberFormat("en-IN")`.

## M3.15 Strategy how-to (S)

- [ ] `docs/plan/HOWTO_strategy.md`: a complete Python strategy example (an opening-range breakout with a stop and a time exit), how to run it from the CLI and the UI, how to read the labels, and how to port it to C++ when it is settled.

---

## Verification

```
cmake --build --preset win-msvc-release && ctest --test-dir engine/build/win-msvc-release -C Release
.\.venv\Scripts\python.exe tests\run_tests.py
python -m stockify golden run --impl oracle && python -m stockify golden run --impl engine
python -m stockify run --config examples/runs/sma_reliance_daily.json        # twice: identical determinism.json
python -m stockify compare-artifacts <oracle_run> <engine_run> --tol-paise 0
cd web; npm run lint; npm run build
```

## Definition of done

All goldens and the differential fuzz pass on both implementations; determinism holds; a Python strategy runs unchanged on both; `/tests` works end to end on a phone-sized viewport; benchmark numbers recorded.
