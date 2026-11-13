# Exact margins: the margin staircase (design)

Date: 2026-11-09. Status: draft for the owner's approval. Relates to: docs/plan/M4_fno.md (M4.8 SPAN-lite, M4.9
calibration, M4.10 exact SPAN, which this supersedes and widens), M5 (paper daemon), A4 §6.5 (funds and margin),
01_facts.md S-7 / X-12 / X-13 (verified 2026-11-09).

## 1. Intent

Margin decides whether an order is accepted and how much money a strategy has tied up. Today STOCKIFY estimates
it: the flat model, or SPAN-lite calibrated against Kite (NIFTY 2.1 % mean error, BANKNIFTY 8.4 %, hedged futures
not modelled). NSE publishes the exchange's own SPAN risk files daily, publicly, back to 2009. Kite's live margin is
better still, wherever it can be asked.

The owner wants **an objectively falling staircase**: for every margin check, use the most accurate source that can
give the right answer at that moment, and step down only when it can't. Everywhere margin matters: backtests on past
dates, the paper-trading daemon (M5), and pre-trade checks. "Objectively" means each step's accuracy is **measured**
against real Kite answers, and the order follows the evidence.

## 2. Goals

1. Archive NSE's margin files daily from now on. The exposure-margin (ELM) file is only kept by NSE for a rolling
   window of a few months.
2. Compute the **exact SPAN margin** from NSE's SPAN file (CME SPAN 4.00), in Python (oracle) then C++ (engine), with
   exact parity. Validate it against the 400 Kite basket-margin answers stored on 2026-11-09.
3. A **backtest staircase** that picks, per margin check, the best available step. Every run records which step it
   used on which days.
4. A **pre-trade margin check** command: the margin for a position, today or on any past date, in plain words.
5. Design, but build with M5, **the live staircase**: Kite first, every Kite answer journalled, and a database log
   of every gap between Kite and our best fallback.
6. A **staircase scorecard**: each step's measured error against Kite, by portfolio type, re-run whenever new Kite
   answers exist.

## 3. Non-goals

- Live paper trading itself (M5). This design only specifies what M5 must do for margin.
- Adopting calibrated SPAN-lite settings: still the owner's review (M4.9 🛑).
- Inter-exchange or cross-segment margin benefits, commodity and currency SPAN (the files cover them; out of scope).
- Replacing Kite: Kite stays the reference.

## 4. Anti-goals (a failure even if it "works")

- **Lookahead.** A backtest at time t must never use a file published after t. On day D, D's end-of-day file is
  published around 16:55 IST and must not price D's trades.
- Silent mixing: any run whose margin came from different steps without that being visible.
- Breaking determinism: the same bundle must give the same artifacts, re-runs included (I-8).
- Touching recorded tick data, or any Kite call that places, modifies or cancels orders.
- An "exact" figure that doesn't match Kite: "exact" must be demonstrated against Kite answers, not assumed.

## 5. The staircase

### 5.1 Backtests (deterministic: no Kite)

| Step | Source | Available when |
|---|---|---|
| B1 | Exact SPAN from the NSE SPAN file **in force at t**, plus ELM from NSE's contract-wise ELM file for that day | intraday/BOD files archived for D (from now on); ELM file archived for D |
| B2 | Exact SPAN as B1, plus ELM from STOCKIFY's dated ELM rules (margin_params / broker_policy) | SPAN file available, ELM file not (most history before ~Oct 2026) |
| B3 | SPAN-lite with the owner-adopted calibrated row in force | no usable SPAN file (before 2009, a missing day) and an adopted row exists |
| B4 | SPAN-lite with the placeholder rows | as B3, no adopted row |
| B5 | Flat model | nothing above applies, or the run asks for it explicitly |

**"In force at t"** on trading day D: the latest of D's SPAN files whose `<created>` time is ≤ t. Verified
2026-11-09: `nsccl.<D>.i1` is D's **start-of-day** file, created the evening before (9 Nov's at 20:26 IST on 8 Nov).
The later intraday files `i2`, `i3`, `i4` follow during D (≈11:00, 12:30, 14:00 IST), and D's end-of-day `.s` file
comes at ≈16:55. There is no separate `.b` file. Intraday files stay downloadable after the day (6 Nov's i2/i4 were
fetched on 9 Nov). If none of D's files is available, use the previous trading day's end-of-day file, which NSE
published before D's open. D's own `.s` file is never used for D's trades (§4).

How far back the intraday files go is not yet known (only EOD was probed back to 2009). The backfill fetches
whatever exists. The rule above then uses intraday precision wherever it exists, and the previous-day EOD file
elsewhere.

**The ELM file for D applies to D** (verified 2026-11-09). D's `F_AEL_OPT_CONTRACTS_<D>` carries D's rates,
including the expiry-day uplift for contracts expiring on D: NIFTY 9 Nov options show 4–5 % in 9 Nov's file vs 2–3 %
in 6 Nov's. ELM rates follow published rules, not market prices, so using D's file for D's trades leaks no future
market information. *My call:* the archive records the fetch time as the ELM file's time (the file has no
`<created>`).

**Expiry-day add-on: no double count.** Because NSE's ELM % already includes the expiry-day uplift, B1 applies the
ELM file's % only. STOCKIFY's own expiry-day add-on rule (X-9, VOL 4.3) applies only where ELM comes from the dated
rules (B2 and below).

**A contract missing from the in-force file** (e.g. a strike or expiry listed during D, absent from the
previous-day file). *My call:* the staircase works **per underlying per check**. If any leg of an underlying is
missing from the in-force file, that whole underlying steps down to the next step that can price all its legs
(B3/B4 SPAN-lite, else B5). It never mixes sources within one underlying, because spread and option-value credits
only make sense together. The step is recorded per underlying (§8).

**Validation gate.** *My call:* "matches Kite" means SPAN within ₹50 or 0.5 % per portfolio, whichever is larger,
on at least 95 % of the 400 samples, and exposure matching Kite's `exposure` within ₹1. Validation runs on the oracle
before the C++ engine is built. If it fails, the builder investigates and reports to the supervisor (🛑) before going
on. B1 is never labelled "exact" unless it has passed.

**Default model.** *My call, following the owner's "the most accurate option always gets used":* the staircase
becomes the default `margin.model` (`staircase`) for new run configs. Existing configs, goldens and saved bundles
that name `flat` or `spanlite` keep that model and their outputs (no golden regenerated for this). A run can still
pin any single step (`flat`, `spanlite`, `span_exact`) for comparisons. The new `margin_daily.csv` step column
changes every golden's artifacts and hashes: a one-time, listed change, both engines together.

**Stocks.** *My call:* stock derivatives are in scope for B1/B2 (same SPAN file, same ELM file). The D-60
delivery-margin schedule's "% of VaR + ELM" rows stay on their labelled approximation until NSE's cash-market VaR
files are verified (S-7 lists them unchecked). The archive adds those files once a source is confirmed. Validation
starts with the index samples; a stock sample set comes from the next live calibration.

Archiving starts now, so most history uses the previous-day EOD rule (B1/B2 with yesterday's file) unless intraday
files turn out to exist that far back.

### 5.2 Live (M5)

| Step | Source | Fails when |
|---|---|---|
| L1 | Kite basket margin for the exact position, now | not logged in (token dies 06:00 IST daily); rate limit (≈10/s) reached; a decision needs an answer faster than a request takes; Kite/network down; position Kite won't price (expired / unknown contract) |
| L2 | **NSE parameters at live prices**: the in-force SPAN file's per-contract scan ranges and volatility settings, revalued with the recorder's current option and futures prices | no SPAN file archived for today or yesterday; prices stale beyond a threshold |
| L3 | The in-force NSE SPAN file as published (risk arrays as-is, prices as of publication) | as L2 |
| L4 | SPAN-lite (adopted row, else placeholder) | — |
| L5 | Flat model | — |

**Kite wins whenever it answers.** In parallel, the session computes its best fallback (the highest of L2–L5 that
works) for the same position and moment.

**Replay determinism (M5.4).** Every Kite answer used is journalled as an input event (position, request time,
Kite's span / exposure / option premium / total). A replay uses the journalled answer; it never asks Kite again.

### 5.3 The disagreement log (live)

Every time a Kite answer and our best fallback differ, one row: wall time, engine time, session, the exact legs,
the market snapshot used (prices, the SPAN file in force), Kite's breakdown (span, exposure, option premium, total),
our answer, which step produced it, the gap in ₹ and %. Stored in lab.sqlite: extend the planned `margin_live` table
(migration only, never drop). Not used automatically. A report command summarises where the gaps cluster: by
portfolio type, underlying, time of day, age of the in-force SPAN file. It feeds later model improvement, like the
2026-11-09 calibration rows did.

## 6. Data and archive

**Verified 2026-11-09** (01_facts S-7):

- SPAN: `https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.<YYYYMMDD>.s.zip` (end of day), intraday
  `.i1.zip` …, a zip of `nsccl.<YYYYMMDD>.s.spn`. CME SPAN 4.00 XML, about 10 MB zipped / 54 MB raw. Per contract: price, delta,
  volatility and a risk array (16 scenario values plus a composite delta). Per combined commodity: price/volatility
  scan ranges, short-option-minimum tiers, intra- and inter-commodity spread tiers. EOD present back to 2009-01-01 (RECON is the source for the start date).
- ELM: NSE reports API, "F&O-Contract wise ELM file", returns gzipped `F_AEL_OPT_CONTRACTS_<DDMMYYYY>.csv`
  (instrument, symbol, expiry, strike, CE/PE, 0, ELM %; 147,472 rows on 6 Nov 2026). Rolling window only (1 Oct
  2026 ✔, 1 Jul 2026 ✗). Needs a cookie warm-up on all-reports-derivatives.
- Fetch with the existing `stockify.data.nse_files.Http` (browser headers, warm-up, throttle). No login.

**Archive** (new, under `$LAB`, through `stockify.paths`): raw files kept byte for byte, plus a manifest (URL, fetch
time, SHA-256, publication time from the file's `<created>`), like the NSE EOD archive.

- From now on: every trading day, all of the day's SPAN files (i1 start-of-day, i2–i4 intraday, .s end-of-day), plus that day's ELM file. A daemon
  schedule job after the EOD file is out (≈17:00 IST), plus intraday fetches during the session.
- Right away: the ELM files still inside NSE's window (back to ~Oct 2026).
- History: EOD SPAN files back to 2009, fetched by a resumable, throttled backfill (about 4,300 days, ~20 GB). Pacing
  is the builder's choice; politeness to NSE comes first.
- Storage estimate: ~60 MB per trading day with intraday files, ~15 GB a year.

## 7. The exact SPAN calculation

Python first (`stockify/fno/span.py`), then C++ (`engine/src/margin/span.cpp`), exact parity, behind the existing
`MarginModel` interface as a new model. Per combined commodity:

- scanning risk = the worst of the 16 scenario values of Σ (position × risk array), with the file's rules for
  extreme scenarios;
- intra-commodity (calendar) spread charge from the intra tiers;
- inter-commodity credits as the file specifies (NSE files may show none);
- short-option minimum from the SOM tiers;
- net option value (long options credit their value, short options charge theirs);

then exposure margin per §5.1 (the ELM file % or the dated rules), and the expiry-day add-on rules already in place
(X-9). Rounded and converted to paise exactly as A4 §0.3 requires.

**C++ never reads files or the network.** Python resolves, for a run, the SPAN parameters each day needs (only the
run's underlyings, only the files in force) into the bundle, so the bundle hash captures them. The engine reads them
from the bundle. Large-run bundle size is a builder concern: per-day subsets of a few underlyings stay small.

**Validation.** Recompute each of the 400 stored 2026-11-09 Kite answers with the SPAN file in force at that
sample's wall time (archive 9 Nov's files first). Target: span within a few rupees of Kite's `span`, exposure matching
Kite's `exposure`. Every miss is investigated, not tuned away.

## 8. Recording which step was used

- Each margin check notes its step. A run's manifest and labels show the counts per step and the list of days per
  step (e.g. `margin:span_exact` 240 days, `margin:spanlite_placeholder` 3 days).
- `margin_daily.csv` gains the step per row: a new column in a deterministic artifact, so both engines,
  A1 §6.3 and blockwatch change together.
- The `/tests` detail page explains the steps in plain words (the M4.14 label card).

## 9. Pre-trade check

`python -m stockify margin check --leg "BUY 65 NIFTY26NOVFUT" --leg … [--date YYYY-MM-DD] [--at HH:MM]`. It prints the
margin in ₹ with its breakdown, which staircase step produced it, and (with `--date`) the figure as it stood then.
It runs offline from the archive. A live variant asking Kite comes with M5. The `/tests` page can call the same
code later.

## 10. Scorecard

`python -m stockify margin scorecard`: for every stored Kite answer (calibration rows today; the live disagreement
log later), each step's error vs Kite by portfolio type, in plain words. If a lower step measures better than a
higher one for some case, the scorecard says so and the order is revisited (a recorded decision, not an automatic
reorder).

## 11. Constraints (unchanged project rules)

Windows only. Deterministic engine, money in int paise, IST only at the edges. Dated config with fact IDs. Paths via
`stockify.paths`. lab.sqlite migrations only. Tests offline and credential-free: a trimmed real SPAN XML fixture (a
few contracts, real values) checked in, and the full file never needed by tests. Oracle first, then engine, with
parity and fuzz coverage (P-04). Kite: read-only endpoints only; tokens never logged.

## 12. Order of work

1. Archive: daily SPAN and ELM fetch job, the immediate ELM grab, 9 Nov's files (validation needs them), then the
   history backfill in the background.
2. SPAN parser and exact calculation (oracle), validated against the 400 Kite answers; then the C++ engine with parity.
3. The backtest staircase B1–B5, the step recording, the artifact column, the UI explanation.
4. The pre-trade check and the scorecard.
5. Live L1–L5, Kite journalling, the disagreement log: specified here, built with M5.

## 13. Left to the builder

Archive folder layout and manifest format; the backfill pacing; the bundle shape for per-day SPAN parameters; the
parser approach (streaming vs whole-file); internal names; the scorecard's output layout; how `--leg` is spelled.
