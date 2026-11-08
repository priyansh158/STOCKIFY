# A1 — Schemas: databases, files, configs, formats

Conventions for every schema here: time columns ending in `_ns` are UTC epoch nanoseconds (I-4); columns ending in `_paise` are integer paise (I-3); dates are ISO `YYYY-MM-DD` text in IST trading-day terms; rates in config files are **decimal strings holding fractions** (`"0.0005"` = 0.05%) that loaders convert exactly to integer ppb/ppm/bp (`A4_engine_semantics.md` §0). Every SQLite store has a `schema_version` table and numbered migrations (I-13).

---

## §1 `refdata.sqlite`

### 1.1 Instruments and identity (D-06)

```sql
CREATE TABLE instruments (
  instrument_id   INTEGER PRIMARY KEY,           -- permanent surrogate key
  exchange        TEXT NOT NULL,                 -- NSE | NFO | BSE ...
  kind            TEXT NOT NULL,                 -- EQ | INDEX | FUT | CE | PE | FUT_CONT
  tradingsymbol   TEXT,                          -- NULL for bhavcopy-only historical contracts
  underlying      TEXT,                          -- e.g. NIFTY, RELIANCE (derivatives)
  underlying_id   INTEGER REFERENCES instruments(instrument_id),
  expiry          TEXT,                          -- derivatives only
  strike_paise    INTEGER,                       -- options only
  first_seen      TEXT NOT NULL,
  last_seen       TEXT NOT NULL,
  source          TEXT NOT NULL                  -- kite_dump | bhavcopy | synthetic
);
CREATE UNIQUE INDEX ux_instr_symbol ON instruments(exchange, tradingsymbol) WHERE kind IN ('EQ','INDEX');
CREATE UNIQUE INDEX ux_instr_deriv  ON instruments(exchange, underlying, kind, expiry, IFNULL(strike_paise, -1))
                                     WHERE kind IN ('FUT','CE','PE');
CREATE UNIQUE INDEX ux_instr_cont   ON instruments(exchange, underlying, kind) WHERE kind = 'FUT_CONT';

CREATE TABLE instrument_tokens (               -- Kite tokens are only unique per day (K-9)
  instrument_id    INTEGER NOT NULL REFERENCES instruments(instrument_id),
  instrument_token INTEGER NOT NULL,
  exchange_token   INTEGER,
  valid_from       TEXT NOT NULL,
  valid_to         TEXT,                         -- NULL = still valid
  PRIMARY KEY (instrument_id, valid_from)
);
CREATE INDEX ix_tokens_lookup ON instrument_tokens(instrument_token, valid_from);

CREATE TABLE contract_specs (                  -- dated, append-only (C-7)
  instrument_id   INTEGER NOT NULL REFERENCES instruments(instrument_id),
  effective_from  TEXT NOT NULL,
  lot_size        INTEGER NOT NULL,
  tick_paise      INTEGER NOT NULL,
  source          TEXT NOT NULL,                 -- kite_dump | bhavcopy | circular | manual
  PRIMARY KEY (instrument_id, effective_from)
);

CREATE TABLE index_underlyings (               -- populated from contract_rules.json "underlyings"
  key            TEXT PRIMARY KEY,             -- NIFTY, BANKNIFTY, ...
  index_symbol   TEXT NOT NULL,                -- NSE:NIFTY 50, ...
  effective_from TEXT NOT NULL
);
```

### 1.2 NSE files and derived tables

```sql
CREATE TABLE nse_fetch_log (
  dataset TEXT, trade_date TEXT, url TEXT, http_status INTEGER, outcome TEXT,  -- ok | missing | error
  sha256 TEXT, bytes INTEGER, fetched_at_ns INTEGER, parser TEXT, parsed_ok INTEGER,
  PRIMARY KEY (dataset, trade_date)
);

CREATE TABLE fo_eod (
  trade_date TEXT, instrument_id INTEGER,
  open_paise INTEGER, high_paise INTEGER, low_paise INTEGER, close_paise INTEGER, last_paise INTEGER,
  prev_close_paise INTEGER, settle_paise INTEGER, underlying_paise INTEGER,
  oi INTEGER, oi_change INTEGER, volume INTEGER, turnover_paise INTEGER, lot_size INTEGER,
  PRIMARY KEY (trade_date, instrument_id)
);
CREATE TABLE cm_eod (    -- equities, series EQ (others optional)
  trade_date TEXT, instrument_id INTEGER, series TEXT,
  open_paise INTEGER, high_paise INTEGER, low_paise INTEGER, close_paise INTEGER, last_paise INTEGER,
  prev_close_paise INTEGER, volume INTEGER, turnover_paise INTEGER, trades INTEGER,
  PRIMARY KEY (trade_date, instrument_id, series)
);
CREATE TABLE index_eod (
  trade_date TEXT, instrument_id INTEGER,
  open_paise INTEGER, high_paise INTEGER, low_paise INTEGER, close_paise INTEGER,   -- index points × 100
  PRIMARY KEY (trade_date, instrument_id)
);
CREATE TABLE settlement_prices (               -- final settlement prices (D-13)
  underlying_id INTEGER, expiry TEXT, fsp_paise INTEGER, source TEXT, cross_check TEXT,  -- ok | mismatch | n/a
  PRIMARY KEY (underlying_id, expiry)
);
CREATE TABLE freeze_limits (                   -- dated per underlying (X-5)
  underlying TEXT, effective_from TEXT, freeze_qty INTEGER, source TEXT,
  PRIMARY KEY (underlying, effective_from)
);
CREATE TABLE daily_limits (                    -- price bands / circuits snapshot (M1.5)
  trade_date TEXT, instrument_id INTEGER, snapshot_ns INTEGER,
  upper_paise INTEGER, lower_paise INTEGER, source TEXT,
  PRIMARY KEY (trade_date, instrument_id, snapshot_ns)
);
CREATE VIEW fo_universe AS                     -- point-in-time F&O stock universe (C-4)
  SELECT DISTINCT f.trade_date, i.underlying
  FROM fo_eod f JOIN instruments i USING (instrument_id)
  WHERE i.kind = 'FUT' AND i.underlying NOT IN (SELECT key FROM index_underlyings);
```

Raw files: `$LAB/nse/raw/<dataset>/<YYYYMMDD>.<ext>` plus `<file>.sha256`; Kite dumps under `$LAB/nse/raw/kite_instruments/`. Immutable.

### 1.3 Tick days and data quality

```sql
CREATE TABLE tick_days (
  trade_date TEXT PRIMARY KEY, landing_path TEXT, archive_path TEXT,
  status TEXT,                                   -- recording | landing | archived | verified | landing_deleted
  packets INTEGER, landing_bytes INTEGER, archive_bytes INTEGER, digest TEXT, verified_at_ns INTEGER
);
CREATE TABLE data_quality (
  trade_date TEXT, instrument_id INTEGER, metric TEXT, value REAL, detail TEXT,
  PRIMARY KEY (trade_date, instrument_id, metric)
);
```

## §2 `bars.sqlite`

```sql
CREATE TABLE bars (
  instrument_id INTEGER, interval TEXT,          -- minute | 3minute | ... | day
  ts_ns INTEGER,                                 -- bar START, UTC
  open_paise INTEGER, high_paise INTEGER, low_paise INTEGER, close_paise INTEGER,
  volume INTEGER, oi INTEGER, fetched_on TEXT NOT NULL,
  PRIMARY KEY (instrument_id, interval, ts_ns)
);
CREATE TABLE bar_fetch_log (instrument_id INTEGER, interval TEXT, from_ns INTEGER, to_ns INTEGER,
  rows INTEGER, fetched_at_ns INTEGER, outcome TEXT);
CREATE TABLE adjustments_detected (instrument_id INTEGER, detected_on TEXT, ratio REAL,
  sample_ts_ns INTEGER, action TEXT,             -- refetched | flagged_only (expired, not refetchable)
  PRIMARY KEY (instrument_id, detected_on));
```

Price basis of everything in `bars.sqlite` = `adjusted` (K-6, I-5).

## §3 Ticks

### 3.1 Landing file `ticks/landing/ticks_YYYYMMDD.sqlite` (WAL)

```sql
CREATE TABLE packets (
  seq INTEGER PRIMARY KEY,                       -- strictly increasing within the day
  frame_no INTEGER NOT NULL,                     -- which WebSocket frame the packet came in (replay rebuilds frames exactly)
  recv_ns INTEGER NOT NULL,                      -- Clock.now_ns() at receipt (event time, C-1)
  mono_ns INTEGER NOT NULL,                      -- monotonic clock (latency diagnostics only)
  instrument_id INTEGER, instrument_token INTEGER NOT NULL,
  mode INTEGER NOT NULL,                         -- 1 ltp | 2 quote | 3 full
  pkt_len INTEGER NOT NULL,                      -- 8 | 28 | 32 | 44 | 184
  ltp INTEGER, last_qty INTEGER, atp INTEGER, volume INTEGER, buy_qty INTEGER, sell_qty INTEGER,
  open INTEGER, high INTEGER, low INTEGER, close INTEGER,
  last_trade_ts_s INTEGER, oi INTEGER, oi_high INTEGER, oi_low INTEGER, exch_ts_s INTEGER,
  raw BLOB NOT NULL                              -- exact packet bytes (D-08)
);
CREATE INDEX ix_packets_instr ON packets(instrument_id, seq);
CREATE TABLE gaps (gap_id INTEGER PRIMARY KEY, start_ns INTEGER, end_ns INTEGER, reason TEXT,
  scope TEXT);                                   -- all | tier:<name> | instrument:<id>
CREATE TABLE subscription_log (recv_ns INTEGER, action TEXT, instrument_id INTEGER,
  instrument_token INTEGER, mode TEXT, reason TEXT);
CREATE TABLE heartbeats (minute_ns INTEGER PRIMARY KEY, count INTEGER);   -- 1-byte heartbeats per minute (liveness)
CREATE TABLE recorder_meta (key TEXT PRIMARY KEY, value TEXT);
-- keys: trade_date, universe_sha256, kiteconnect_version, recorder_version, parser_version, host
```

Prices in `packets` are as sent by Kite (paise; index values × 100). Parsing rules: `01_facts.md` K-2/K-3.

### 3.2 Archive `ticks/archive/date=YYYY-MM-DD/`

| File | Rows | Columns |
|---|---|---|
| `full.parquet` | tradable packets recorded in FULL mode | all `packets` columns + `bid_px_1..5, bid_qty_1..5, bid_orders_1..5, ask_px_1..5, ask_qty_1..5, ask_orders_1..5` |
| `quote.parquet` | tradable packets in QUOTE mode | `packets` columns |
| `index.parquet` | index packets | `seq, frame_no, recv_ns, instrument_id, ltp, high, low, open, close, change, exch_ts_s, raw` |
| `gaps.parquet`, `subscriptions.parquet`, `heartbeats.parquet` | copies of the landing tables | |
| `meta.json` | | row counts, per-file SHA-256, landing digest, parser version, created time |

zstd compression, row groups ≈ 1M rows. Each row group covers a contiguous `seq` range; inside it rows are ordered by `(instrument_id, seq)` with delta-encoded integers (D-37), so readers sort a row group by `seq`.

## §4 `lab.sqlite`

Accessed only through `stockify/store.py` (same pattern as `geo_alerts.py`: singleton connection, WAL, `check_same_thread=False`, Row factory; tests use `SandboxedDBTest` with `db_modules = (stockify.store, …)`).

```sql
-- runs (M3)
CREATE TABLE runs (run_id TEXT PRIMARY KEY, name TEXT, family_id TEXT, status TEXT,   -- queued|running|done|failed|cancelled
  created_ns INTEGER, started_ns INTEGER, finished_ns INTEGER, folder TEXT,
  bundle_sha256 TEXT, engine_build_id TEXT, impl TEXT,                                  -- engine | oracle
  config_json TEXT, metrics_json TEXT, labels_json TEXT, error TEXT);
CREATE TABLE run_orders (run_id TEXT, order_id INTEGER, row_json TEXT, PRIMARY KEY (run_id, order_id));
CREATE TABLE run_fills (run_id TEXT, fill_id INTEGER, order_id INTEGER, t_ns INTEGER, instrument_id INTEGER,
  side TEXT, qty INTEGER, price_paise INTEGER, PRIMARY KEY (run_id, fill_id));
CREATE TABLE run_equity (run_id TEXT, trade_date TEXT, nav_paise INTEGER, cash_paise INTEGER,
  margin_paise INTEGER, PRIMARY KEY (run_id, trade_date));
CREATE TABLE run_charges (run_id TEXT, row_no INTEGER, kind TEXT, order_id INTEGER, trade_date TEXT,
  components_json TEXT, total_paise INTEGER, PRIMARY KEY (run_id, row_no));

-- research (M6)
CREATE TABLE trial_families (family_id TEXT PRIMARY KEY, description TEXT, objective TEXT, created_ns INTEGER);
CREATE TABLE holdout_unlocks (unlock_ns INTEGER, run_id TEXT, family_id TEXT, reason TEXT);

-- sessions (M5)
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, name TEXT, family_id TEXT, status TEXT, pause_reason TEXT,
  config_json TEXT, created_ns INTEGER, stopped_ns INTEGER);
CREATE TABLE session_days (session_id TEXT, trade_date TEXT, bundle_sha256 TEXT, journal_path TEXT,
  sod_state_json TEXT, eod_state_json TEXT, coverage REAL, parity TEXT,   -- pass | fail | pending
  PRIMARY KEY (session_id, trade_date));
CREATE TABLE session_timeline (session_id TEXT, engine_ns INTEGER, wall_ns INTEGER, kind TEXT, detail_json TEXT);
CREATE TABLE session_orders (session_id TEXT, trade_date TEXT, order_id INTEGER, row_json TEXT,
  PRIMARY KEY (session_id, trade_date, order_id));
CREATE TABLE session_order_events (session_id TEXT, trade_date TEXT, event_no INTEGER, row_json TEXT,
  PRIMARY KEY (session_id, trade_date, event_no));
CREATE TABLE session_fills (session_id TEXT, trade_date TEXT, fill_id INTEGER, row_json TEXT,
  PRIMARY KEY (session_id, trade_date, fill_id));
CREATE TABLE session_charges (session_id TEXT, trade_date TEXT, row_no INTEGER, row_json TEXT,
  PRIMARY KEY (session_id, trade_date, row_no));
CREATE TABLE session_positions_eod (session_id TEXT, trade_date TEXT, row_no INTEGER, row_json TEXT,
  PRIMARY KEY (session_id, trade_date, row_no));
CREATE TABLE session_equity (session_id TEXT, trade_date TEXT, nav_paise INTEGER, cash_paise INTEGER,
  margin_paise INTEGER, PRIMARY KEY (session_id, trade_date));
CREATE TABLE margin_live (session_id TEXT, wall_ns INTEGER, portfolio_sha256 TEXT, kite_json TEXT,
  model_json TEXT);
CREATE TABLE parity_results (session_id TEXT, trade_date TEXT, kind TEXT,     -- journal | recorder
  outcome TEXT, summary_json TEXT, created_ns INTEGER, PRIMARY KEY (session_id, trade_date, kind));

-- margin calibration (M4.9)
CREATE TABLE margin_calibration (sample_id INTEGER PRIMARY KEY, wall_ns INTEGER, underlying TEXT,
  portfolio_json TEXT, market_json TEXT, kite_json TEXT, t0_paise INTEGER, t1_paise INTEGER, t1_breakdown_json TEXT);

-- recorder health (M1.5); rows older than 7 days are pruned daily
CREATE TABLE recorder_status (wall_ns INTEGER PRIMARY KEY, connected INTEGER, subs_full INTEGER, subs_quote INTEGER,
  packets_per_s REAL, queue_depth INTEGER, writer_lag_ms REAL, last_packet_age_ms_json TEXT, bytes_today INTEGER);
```

## §5 Event pack / journal format (`.evt`, version 1)

Little-endian, no padding between records, readers use `memcpy` (no alignment assumptions).

**Header (64 bytes)**

| Offset | Type | Field |
|---|---|---|
| 0 | char[8] | magic `"STKEVT01"` |
| 8 | u16 | version = 1 |
| 10 | u16 | flags: bit 0 = journal, bit 1 = contains CLOCK |
| 12 | u32 | header size = 64 |
| 16 | u64 | record count (journals: 0 while open; set on finalise) |
| 24 | i64 | min `t_event` |
| 32 | i64 | max `t_event` |
| 40 | u8 | price basis: 0 raw, 1 adjusted |
| 41 | u8[23] | zero |

**Record prefix (24 bytes)**: `u16 kind`, `u16 size` (whole record incl. prefix), `u32 instrument_id` (0 if none), `i64 t_event`, `u64 seq`.

<!-- <block name="event-kinds" same-as-pattern='^\| (?P<key>\d+) \| \x60(?P<value>[A-Z_]+)\x60'> -->

| kind | name | rank (A4 §1.3) | payload after prefix | size |
|---|---|---|---|---|
| 1 | `TICK_QUOTE` | 4 | i64 × 10: ltp, last_qty, atp, volume, buy_qty, sell_qty, open, high, low, close | 104 |
| 2 | `TICK_FULL` | 4 | the 10 quote fields; i64 × 5: last_trade_ts_s, oi, oi_high, oi_low, exch_ts_s; depth 10 × {i64 price, i64 qty, i32 orders, i32 pad} (bids 1–5 then asks 1–5) | 384 |
| 3 | `INDEX` | 4 | i64 × 7: value, high, low, open, close, change, exch_ts_s (0 if absent) | 80 |
| 4 | `BAR` | 4 | i32 interval_s, i32 pad, i64 start_ns, i64 × 4 OHLC, i64 volume, i64 oi | 88 |
| 5 | `CLOCK` | 5 | none | 24 |
| 6 | `GAP` | 1 | u8 phase (0 start, 1 end), u8 pad, u16 reason, u32 scope (0 all; 1 the instrument in the prefix) | 32 |
| 7 | `REFDATA` | 2 | u16 subtype, u16 pad, u32 len, then `len` bytes of canonical JSON (sorted keys, no whitespace) | 32 + len |
| 8 | `SETTLE` | 3 | u8 subtype (0 final settlement price of the underlying in the prefix for an expiry, 1 daily settlement price of the contract in the prefix), u8[3] pad, i32 date (YYYYMMDD: the expiry or the trade date), i64 price_paise | 40 |

<!-- </block> -->

<!-- <block name="refdata-subtypes" same-as-pattern='(?P<key>\d) \x60(?P<value>[a-z_]+)\x60'> -->
- REFDATA subtypes: 1 `instrument_add` (static fields + spec), 2 `price_band`, 3 `margin_requirement` (portfolio hash, amount, components, source), 4 `spec_change`. Because `size` is a u16, a REFDATA JSON payload is at most 65,503 bytes; larger content is split into several records (one instrument per `instrument_add`).
<!-- </block> -->
<!-- <block name="gap-reasons" same-as-pattern='(?P<key>\d) (?P<value>[a-z_]+)'> -->
- GAP reasons: 1 disconnect, 2 process_stop, 3 token_expired, 4 stall, 5 not_subscribed. Landing gaps scoped to a tier are expanded into one GAP record per affected instrument.
<!-- </block> -->
- Sorting inside a file: `(t_event, rank, seq)`. In packs, `seq` is reassigned 1…N after sorting by `(t_event, rank, source seq, instrument_id)`, where bars' source seq is their row order by `(instrument_id, start_ns)`. Several packs in one run are merged by `(t_event, rank, pack index, seq)` (A4 §1.3). In journals, `seq` is the dispatcher's per-day counter.
- Prices are paise; index values are points × 100. Unknown kinds are an error, never skipped.

## §6 Runs

### 6.1 `run.json`

```json
{
  "schema": "stockify.run/1",
  "name": "sma_reliance_daily",
  "family_id": "sma_cross/RELIANCE/daily",
  "strategy": { "kind": "python", "ref": "stockify.strategies.sma_cross:SmaCross",
                "params": { "symbol": "NSE:RELIANCE", "fast": 20, "slow": 50 } },
  "data": { "packs": [ { "spec": { "dataset": "bars", "interval": "day",
            "instruments": ["NSE:RELIANCE"], "from": "2022-01-03", "to": "2026-09-30" } } ] },
  "price_basis": "adjusted",
  "capital_paise": 100000000,
  "fill_model": { "kind": "bar", "limit_touch_policy": "through", "participation": "0.10",
                  "slippage": { "kind": "fixed_ticks", "k": 1 } },
  "latency_ms": 0,
  "costs": { "broker": "zerodha", "allow_unverified": true, "auto_square_off_fee": true, "dp_charges": true },
  "margin": { "model": "flat" },
  "policies": { "preopen": "queue", "cas": "none", "freeze": "slice", "max_slices": 10, "rms_square_off": false },
  "mark": "ltp",
  "seed": 42,
  "unlock_holdout": null
}
```

`strategy.kind`: `python` (`module:Class`) or `cpp` (registered name). `data.packs[]` items are either `{"spec": …}` (built on demand) or `{"sha256": …}`. `allow_unverified: true` lets the run use charge rows not yet verified (Z-3) and labels it (I-14).

### 6.2 `bundle.json` (D-22)

```json
{
  "schema": "stockify.bundle/1",
  "run": { "...": "the validated run.json" },
  "seed": 42,
  "days": [ { "date": "2026-10-28",
              "groups": { "CM":     [ { "phase": "PREOPEN", "start_ns": 0, "end_ns": 0 }, { "phase": "CONTINUOUS", "start_ns": 0, "end_ns": 0 } ],
                          "CM_CAS": [ "..." ], "FO_FUT": [ "..." ], "FO_OPT": [ "..." ] },
              "mis_cutoff_ns": { "CM": 0, "CM_CAS": 0, "FO": 0 },
              "day_end_ns": 0 } ],
  "instruments": [ { "id": 1234, "exchange": "NFO", "kind": "CE", "tradingsymbol": "…",
                     "underlying_id": 1, "underlying_class": "INDEX", "session_group": "FO_OPT",
                     "expiry": "2026-11-03", "strike_paise": 2500000, "settlement": "cash",
                     "last_trading_ns": 0,
                     "specs": [ { "from": "2026-10-05", "lot": 65, "tick_paise": 5, "freeze_qty": 3510 } ] } ],
  "charges": { "...": "component rows effective on any day in range (§7.2), rates already converted to ppb" },
  "broker_policy": { "...": "rows effective in range (§7.5)" },
  "margin_params": { "...": "§7.6" },
  "pricing": { "...": "§7.7" },
  "packs": [ { "sha256": "…", "path": "packs/….evt", "price_basis": "adjusted" } ],
  "strategy": { "kind": "python", "ref": "…", "source_sha256": "…", "params": {} },
  "labels": [ "fill:bar", "margin:flat(approx)", "charges:unverified_rows" ],
  "sod_state": null
}
```

(Zeros are placeholders.) An instrument's `session_group` is `CM` or `CM_CAS` for equities (CAS-eligible from 2026-08-03 when the stock is in that day's `fo_universe`), `FO_FUT` or `FO_OPT` for derivatives; indices follow `CM`. Paper sessions get one bundle per day, with `seed` derived as in A4 §4.5 and `sod_state` filled.

### 6.3 Run folder `runs/<run_id>/`

<!-- <block name="deterministic-files" same-as-pattern='^\| \x60(?P<value>[a-z_]+\.[a-z]+)\x60 \| ✔'> -->

| File | Deterministic (A4 §9) | Content |
|---|---|---|
| `run.json`, `bundle.json` | input | as above |
| `orders.csv` | ✔ | `order_id,parent_id,instrument_id,side,product,type,validity,qty,price_paise,trigger_paise,protection_bp,tag,t_submit_ns,t_activate_ns,status,filled_qty,avg_price_paise,reason` |
| `order_events.csv` | ✔ | `t_ns,order_id,event,detail` |
| `fills.csv` | ✔ | `fill_id,order_id,t_ns,instrument_id,side,qty,price_paise,evidence` (evidence: `bar_open`, `bar_path`, `vwap_int`, `ltp`, `book_lvl_<k>`, `book_cross`, `trade_through`, `eod_close`) |
| `charges.csv` | ✔ | `row_no,kind,order_id,instrument_id,trade_date,turnover_paise,brokerage,stt,exchange,sebi,stamp,gst,dp,other,total` (paise; `kind` ∈ `order, dp, sqoff_fee, settlement`; `order_id` 0 when not an order) |
| `positions_eod.csv` | ✔ | `trade_date,instrument_id,product,qty,avg_price_paise,mark_paise,mark_source,unrealised_paise,realised_paise,underlying_mark_paise` (the last: a derivative's underlying mark at DAY_END, empty otherwise; D-55) |
| `equity.csv` | ✔ | `trade_date,nav_paise,cash_paise,margin_blocked_paise,gross_pnl_paise,charges_paise` |
| `margin_daily.csv` | ✔ (±100 paise across platforms) | `trade_date,underlying_id,model,scan,somc,elm,addons,total` (M4) |
| `metrics.json` | ✔ (floats; A4 §9.3) | A4 §8 |
| `events_log.txt` | ✔ | strategy and engine log lines stamped with engine time |
| `determinism.json` | — | SHA-256 of each deterministic file |
| `manifest.json` | ✗ | engine build info, Python version, OS, wall-clock start/duration, input hashes, labels |
| `report.md` (+ `equity.png`) | ✗ | human summary (M2.6) |

<!-- </block> -->

## §7 Config files (`stockify/config/`)

Every dated row carries `effective_from`, `source` (usually a `01_facts.md` ID) and `verified`; `verified_on` is required when `verified` is true (I-1, I-14). `effective_from: "2015-01-01"` is a floor meaning "in force before any data this lab uses", not a claimed start date.

### 7.1 `universe.json`

```json
{
  "schema": "stockify.universe/1",
  "indices_full": ["NSE:NIFTY 50", "NSE:NIFTY BANK", "NSE:INDIA VIX"],
  "futures_full": [ { "underlying": "NIFTY", "months": ["near", "next"] },
                    { "underlying": "BANKNIFTY", "months": ["near", "next"] } ],
  "chains": [
    { "underlying": "NIFTY", "expiry": "weekly:0", "n_full": 10, "n_quote": 20, "recenter_threshold": 2, "hysteresis": 2 },
    { "underlying": "NIFTY", "expiry": "weekly:1", "n_full": 6, "n_quote": 6 },
    { "underlying": "NIFTY", "expiry": "monthly:0", "n_full": 6, "n_quote": 6 },
    { "underlying": "BANKNIFTY", "expiry": "monthly:0", "n_full": 6, "n_quote": 6 },
    { "underlying": "BANKNIFTY", "expiry": "monthly:1", "n_full": 0, "n_quote": 6 } ],
  "equities_full": ["NSE:RELIANCE", "NSE:HDFCBANK", "NSE:INFY"],
  "stock_futures_full": [ { "underlying": "RELIANCE", "months": ["near"] } ],
  "equities_quote": ["NSE:TCS", "NSE:ICICIBANK", "…"],
  "limits": { "max_per_connection": 3000, "warn_fraction": "0.8" }
}
```

### 7.2 `charges.json` (per-component dated history)

```json
{
  "schema": "stockify.charges/1",
  "broker": "zerodha",
  "components": {
    "brokerage": [
      { "applies": { "segment": "CM", "product": "CNC" }, "effective_from": "2015-01-01", "kind": "zero",
        "source": "Z-1", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "CM", "product": "MIS" }, "effective_from": "2015-01-01", "kind": "min_flat_pct",
        "flat_paise": 2000, "pct": "0.0003", "source": "Z-1", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "FO", "kind": ["FUT"] }, "effective_from": "2015-01-01", "kind": "min_flat_pct",
        "flat_paise": 2000, "pct": "0.0003", "source": "Z-1", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "FO", "kind": ["CE", "PE"] }, "effective_from": "2015-01-01", "kind": "flat",
        "flat_paise": 2000, "source": "Z-1", "verified": true, "verified_on": "2026-10-29" } ],
    "stt": [
      { "applies": { "segment": "FO", "kind": ["FUT"] }, "effective_from": "2015-01-01", "sell": "0.0001", "source": "Z-2", "verified": false },
      { "applies": { "segment": "FO", "kind": ["FUT"] }, "effective_from": "2023-04-01", "sell": "0.000125", "source": "Z-2", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "FO", "kind": ["FUT"] }, "effective_from": "2024-10-01", "sell": "0.0002", "source": "Z-2", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "FO", "kind": ["FUT"] }, "effective_from": "2026-04-01", "sell": "0.0005", "source": "Z-2", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "FO", "kind": ["CE", "PE"] }, "effective_from": "2015-01-01", "sell": "0.0005", "source": "Z-2", "verified": false },
      { "applies": { "segment": "FO", "kind": ["CE", "PE"] }, "effective_from": "2023-04-01", "sell": "0.000625", "source": "Z-2", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "FO", "kind": ["CE", "PE"] }, "effective_from": "2024-10-01", "sell": "0.001", "source": "Z-2", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "FO", "kind": ["CE", "PE"] }, "effective_from": "2026-04-01", "sell": "0.0015", "source": "Z-2", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "CM", "product": "CNC" }, "effective_from": "2015-01-01", "buy": "0.001", "sell": "0.001", "source": "Z-1", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "CM", "product": "MIS" }, "effective_from": "2015-01-01", "sell": "0.00025", "source": "Z-1", "verified": true, "verified_on": "2026-10-29" } ],
    "exercise_stt": [
      { "applies": { "segment": "FO", "kind": ["CE", "PE"] }, "effective_from": "2016-06-01", "pct_of_intrinsic": "0.00125", "source": "Z-2", "verified": false },
      { "applies": { "segment": "FO", "kind": ["CE", "PE"] }, "effective_from": "2026-04-01", "pct_of_intrinsic": "0.0015", "source": "Z-2", "verified": true, "verified_on": "2026-10-29" } ],
    "exercise_brokerage": [
      { "applies": { "segment": "FO", "kind": ["CE", "PE"] }, "effective_from": "2015-01-01", "kind": "flat", "flat_paise": 2000,
        "source": "Z-1 (charged on exercised/assigned contracts; basis unconfirmed)", "verified": false } ],
    "exchange_txn": [
      { "applies": { "segment": "FO", "kind": ["FUT"] }, "effective_from": "2024-10-01", "pct": "0.0000173", "source": "Z-3", "verified": false },
      { "applies": { "segment": "FO", "kind": ["FUT"] }, "effective_from": "VERIFY", "observed_on": "2026-10-29", "pct": "0.0000183", "source": "Z-1", "verified": false },
      { "applies": { "segment": "FO", "kind": ["CE", "PE"] }, "effective_from": "2024-10-01", "pct": "0.0003503", "source": "Z-3", "verified": false },
      { "applies": { "segment": "FO", "kind": ["CE", "PE"] }, "effective_from": "VERIFY", "observed_on": "2026-10-29", "pct": "0.0003553", "source": "Z-1", "verified": false },
      { "applies": { "segment": "CM" }, "effective_from": "VERIFY", "observed_on": "2026-10-29", "pct": "0.0000307", "source": "Z-1", "verified": false } ],
    "sebi": [ { "applies": {}, "effective_from": "2015-01-01", "pct": "0.000001", "source": "Z-1 (₹10/crore)", "verified": true, "verified_on": "2026-10-29" } ],
    "stamp": [
      { "applies": { "segment": "CM", "product": "CNC" }, "effective_from": "2020-07-01", "buy": "0.00015", "source": "Z-1", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "CM", "product": "MIS" }, "effective_from": "2020-07-01", "buy": "0.00003", "source": "Z-1", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "FO", "kind": ["FUT"] }, "effective_from": "2020-07-01", "buy": "0.00002", "source": "Z-1", "verified": true, "verified_on": "2026-10-29" },
      { "applies": { "segment": "FO", "kind": ["CE", "PE"] }, "effective_from": "2020-07-01", "buy": "0.00003", "source": "Z-1", "verified": true, "verified_on": "2026-10-29" } ],
    "gst": [ { "applies": {}, "effective_from": "2017-07-01", "rate": "0.18", "on": ["brokerage", "exchange_txn", "sebi"], "source": "Z-1", "verified": true, "verified_on": "2026-10-29" } ],
    "dp": [ { "applies": { "segment": "CM", "product": "CNC", "side": "SELL" }, "effective_from": "VERIFY", "observed_on": "2026-10-29",
              "per_scrip_per_day_paise": 1534, "includes_gst": true, "source": "Z-1", "verified": false } ],
    "auto_square_off_fee": [ { "applies": {}, "effective_from": "VERIFY", "observed_on": "2026-10-29",
              "per_order_paise": 5000, "plus_gst": true, "source": "Z-1", "verified": false } ],
    "physical_delivery": [ { "applies": { "segment": "FO" }, "effective_from": "VERIFY", "observed_on": "2026-10-29",
              "brokerage_pct": "0.0025", "netted_brokerage_pct": "0.001", "stt_each_side_pct_of_value": "0.001", "source": "Z-4", "verified": false } ]
  }
}
```

Rules:
- **`"effective_from": "VERIFY"`** marks a value observed on `observed_on` whose start date is unknown. The loader makes it effective from `observed_on` if an earlier row exists for the same `applies` key, otherwise from `2015-01-01`, and keeps `verified: false`. Older dates therefore keep using the older row, and every run touching an unverified row is labelled until the date is confirmed (Z-3).
- Rows marked `verified: false` above have values or start dates from secondary sources or memory (the 2016-06-01 exercise-STT basis change, the pre-2023 STT rates, the 2024-10 exchange rates). Confirm them before trusting backtests that rely on them; never invent history without a source.
- Row matching is A4 §5.2: exactly one row per component per order and date.

### 7.3 `sessions.json`

```json
{
  "schema": "stockify.sessions/1",
  "versions": [
    { "group": "FO_OPT", "effective_from": "2015-01-01",
      "phases": [ { "phase": "CONTINUOUS", "start": "09:15", "end": "15:30" } ],
      "close_vwap_window": ["15:00", "15:30"], "source": "X-7", "verified": true, "verified_on": "2026-10-29" },
    { "group": "FO_OPT", "effective_from": "2026-08-03",
      "phases": [ { "phase": "CONTINUOUS", "start": "09:15", "end": "15:40" } ],
      "close_vwap_window": ["15:10", "15:40"], "source": "X-7", "verified": true, "verified_on": "2026-10-29" },
    { "group": "FO_FUT", "effective_from": "2015-01-01",
      "phases": [ { "phase": "CONTINUOUS", "start": "09:15", "end": "15:30" } ], "source": "X-7", "verified": true, "verified_on": "2026-10-29" },
    { "group": "FO_FUT", "effective_from": "2025-12-08",
      "phases": [ { "phase": "PREOPEN", "start": "09:00", "end": "09:15" },
                  { "phase": "CONTINUOUS", "start": "09:15", "end": "15:30" } ], "source": "X-6", "verified": true, "verified_on": "2026-10-29" },
    { "group": "FO_FUT", "effective_from": "2026-08-03",
      "phases": [ { "phase": "PREOPEN", "start": "09:00", "end": "09:15" },
                  { "phase": "CONTINUOUS", "start": "09:15", "end": "15:40" } ], "source": "X-6, X-7", "verified": true, "verified_on": "2026-10-29" },
    { "group": "CM", "effective_from": "2015-01-01",
      "phases": [ { "phase": "PREOPEN", "start": "09:00", "end": "09:15" },
                  { "phase": "CONTINUOUS", "start": "09:15", "end": "15:30" },
                  { "phase": "POSTCLOSE", "start": "15:40", "end": "16:00" } ], "source": "⚠ post-close times", "verified": false },
    { "group": "CM", "effective_from": "2026-08-03",
      "phases": [ { "phase": "PREOPEN", "start": "09:00", "end": "09:15" },
                  { "phase": "CONTINUOUS", "start": "09:15", "end": "15:30" },
                  { "phase": "POSTCLOSE", "start": "15:50", "end": "16:00" } ], "source": "X-7", "verified": false },
    { "group": "CM_CAS", "effective_from": "2026-08-03",
      "phases": [ { "phase": "PREOPEN", "start": "09:00", "end": "09:15" },
                  { "phase": "CONTINUOUS", "start": "09:15", "end": "15:15" },
                  { "phase": "CAS", "start": "15:15", "end": "15:35" },
                  { "phase": "POSTCLOSE", "start": "15:50", "end": "16:00" } ], "source": "X-7 (⚠ CAS sub-phases)", "verified": false }
  ]
}
```

Times are IST; the resolver converts them to UTC ns per trading day and applies `special_sessions.csv`. Before 2026-08-03 there is no `CM_CAS` group: every equity is `CM`.

### 7.4 `contract_rules.json`

```json
{
  "schema": "stockify.contract_rules/1",
  "expiry_rules": [
    { "effective_from": "2024-11-20", "weekly": { "underlyings": ["NIFTY"], "weekday": "THU" },
      "monthly": { "weekday": "THU", "which": "last" }, "source": "X-1", "verified": true, "verified_on": "2026-10-29" },
    { "effective_from": "2025-09-01", "weekly": { "underlyings": ["NIFTY"], "weekday": "TUE" },
      "monthly": { "weekday": "TUE", "which": "last" }, "source": "X-2", "verified": true, "verified_on": "2026-10-29" } ],
  "holiday_shift": "previous_trading_day",
  "settlement": [ { "effective_from": "2019-10-01", "INDEX": "cash", "STOCK": "physical", "source": "X-8", "verified": true, "verified_on": "2026-10-29" } ],
  "underlyings": { "NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE",
                   "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "NIFTYNXT50": "NSE:NIFTY NEXT 50" }
}
```

(⚠ verify the index tradingsymbols against the Kite dump; add pre-2024-11-20 weekly rules — several indices on different weekdays — only from a sourced list.)

### 7.5 `broker_policy.json`

Keys, each a dated list of rows: `mis_cutoff` (`{group, time}`, Z-5; ⚠ CAS-era equity time), `mis_margin_pct` (decimal string, e.g. `"0.20"`), `market_protection_auto_bp` (integer bp, e.g. `200` = 2%; **an assumption**, labelled; D-34), `freeze_policy_default`, `max_slices` (10, R-1), `cnc_sell_credit_same_day` (fraction), `delivery_margin_schedule` (Z-4 rows: `day_offset`, `applies_to`, `basis`, `pct`), `block_new_long_otm_stock_options_expiry_week`, `rms_square_off_time`.

### 7.6 `margin_params.json`

`flat` (dated: `index_pct`, `stock_pct`; assumption defaults `"0.12"` / `"0.20"`), `spanlite` (per underlying, dated: `psr_pct`, `vsr_pct`, `extreme_multiple` 3, `extreme_weight` `"0.35"`, `somc_per_lot_paise`, `elm_pct`, `default_iv`, `source`, `calibration_id`), `expiry_day_short_index_option_addon` (dated: `pct` `"0.02"`, `basis` — ⚠ X-9).

### 7.7 `pricing.json`

`risk_free` (dated rate; placeholder `"0.065"` until the owner sets it), `day_count` `"ACT/365"`, `stale_after_ms` (default 5000), `eod_publication_ist` `"19:00"` (used as `t_event` of `bhav_eod` bars and daily settlement prices) and `fsp_publication_ist` `"16:00"` (final settlement prices) — ⚠ record the observed publication times.

### 7.8 `schedule.json` — the only place job times live (IST)

```json
{ "schema": "stockify.schedule/1",
  "jobs": { "refdata": "08:30", "subscribe": "08:58", "unsubscribe": "15:45", "compaction": "16:30",
            "nse_fetch": { "start": "19:30", "retry_every_min": 15, "until": "23:00" },
            "token_dependent_retry": { "every_min": 5, "until": "09:10" },
            "day_end": "23:59:59" } }
```

### 7.9 `storage.json`

`retention_days` (**null = never delete landing files**; 🛑 the owner sets a number only after M5.13 backups exist), `disk_warn_free_gb` (20), `disk_warn_free_fraction` (`"0.10"`), `parquet_zstd_level` (9).

### 7.10 Other files

- `sources.json`: per dataset `{url_template, valid_from, valid_to, parser, verified, evidence}` (S-1…S-7).
- `kite_limits.json`: `historical_rps` 3, `quote_rps` 1, `quote_max_instruments` 500, `ws_max_instruments` 3000, `ws_max_connections` 3, `historical_max_days` per interval (⚠ defaults), `learned` (written by M1.4).
- `calendar/nse_holidays.csv`: `date,segments,description,source`. `calendar/special_sessions.csv`: `date,group,phase,start,end,name,source`.
- `freeze_limits.json`: index quantity-freeze limits from NSE circulars, dated per `underlying` (`{underlying, effective_from, freeze_qty, source, verified}`, X-5). They decide for the underlyings they list; the refdata `freeze_limits` table (`qtyfreeze.csv` snapshots, S-6) covers the rest and cross-checks them (D-54).
- `notify.json`: channel type and per-event switches (secrets elsewhere). `research.json`: `holdout_from`, default walk-forward settings.
