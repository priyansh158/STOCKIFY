-- refdata.sqlite, version 1 (A1_schemas.md §1).

-- §1.1 Instruments and identity (D-06)
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
CREATE INDEX ix_instr_chain ON instruments(underlying, expiry, kind);

CREATE TABLE instrument_tokens (               -- Kite tokens are only unique per day (K-9)
  instrument_id    INTEGER NOT NULL REFERENCES instruments(instrument_id),
  instrument_token INTEGER NOT NULL,
  exchange_token   INTEGER,
  valid_from       TEXT NOT NULL,              -- first trading day the token maps here
  valid_to         TEXT,                       -- first day it no longer does (exclusive); NULL = still valid
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

-- §1.2 NSE files and derived tables
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

-- §1.3 Tick days and data quality
CREATE TABLE tick_days (
  trade_date TEXT PRIMARY KEY, landing_path TEXT, archive_path TEXT,
  status TEXT,                                   -- recording | landing | archived | verified | landing_deleted
  packets INTEGER, landing_bytes INTEGER, archive_bytes INTEGER, digest TEXT, verified_at_ns INTEGER
);
CREATE TABLE data_quality (
  trade_date TEXT, instrument_id INTEGER, metric TEXT, value REAL, detail TEXT,
  PRIMARY KEY (trade_date, instrument_id, metric)
);
