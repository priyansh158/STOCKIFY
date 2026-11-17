-- desk.sqlite version 1 (HD 4.1): the holdings desk's derived data. Rows are kept for every day and never deleted;
-- a row written again for the same key (the nightly job re-run on one day) replaces that one row.

-- One row per voting strategy per holding per nightly run: how it did against buy-and-hold on that stock.
CREATE TABLE evidence (
  trade_date TEXT NOT NULL,                      -- IST date the nightly job ran for
  instrument_id INTEGER NOT NULL,
  ref TEXT NOT NULL,                             -- "package.module:Class"
  params_json TEXT NOT NULL,                     -- the strategy params of the backtest
  from_date TEXT NOT NULL, to_date TEXT NOT NULL,  -- first and last trading day backtested (IST)
  trades INTEGER NOT NULL,
  cagr REAL, buy_hold_cagr REAL,                 -- fractions; NULL when undefined
  sharpe REAL, buy_hold_sharpe REAL,             -- annualised, daily returns of nav_paise; NULL when undefined
  max_drawdown REAL, win_rate REAL,              -- fractions; NULL when undefined
  windows_json TEXT NOT NULL,                    -- [{year, sharpe, buy_hold_sharpe, won}]
  windows_won INTEGER NOT NULL, windows_total INTEGER NOT NULL,
  grade TEXT NOT NULL,                           -- strong | some | weak | none
  note TEXT NOT NULL,                            -- one plain sentence for the page
  PRIMARY KEY (trade_date, instrument_id, ref)
);
CREATE INDEX ix_evidence_instrument ON evidence(instrument_id, trade_date);

-- The desk's view of one holding on one day (verdict and everything the page shows), as JSON.
CREATE TABLE views (
  trade_date TEXT NOT NULL,
  instrument_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  verdict TEXT NOT NULL,
  json TEXT NOT NULL,
  made_ns INTEGER NOT NULL,                      -- when it was made, UTC epoch ns
  PRIMARY KEY (trade_date, instrument_id)
);
CREATE INDEX ix_views_instrument ON views(instrument_id, trade_date);

-- The option chain of a holding's underlying as polled, one row per poll.
CREATE TABLE chain_snapshots (
  instrument_id INTEGER NOT NULL,
  taken_ns INTEGER NOT NULL,                     -- UTC epoch ns
  json TEXT NOT NULL,
  PRIMARY KEY (instrument_id, taken_ns)
);
