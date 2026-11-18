-- fundamentals.sqlite version 1 (FS 1): the screener's universe and NSE's sector classification. Derived from NSE
-- files (kept raw under nse/raw/fundamentals), so rebuildable; rows are kept for every day they were read.

-- Today's Nifty 500 list as read on `as_of`, with each stock's median daily traded value over the last
-- `liquidity_days` trading days (refdata cm_eod, series EQ; a day without a row counts as 0).
CREATE TABLE universe (
  as_of TEXT NOT NULL,                     -- IST date the list was read
  symbol TEXT NOT NULL,                    -- NSE symbol as in the list
  instrument_id INTEGER,                   -- refdata instruments NSE:<symbol>; NULL when unknown there
  isin TEXT NOT NULL,
  company TEXT NOT NULL,
  median_value_paise INTEGER,              -- NULL when the stock has no instrument (no traded value known)
  liquid INTEGER NOT NULL,                 -- 1 when median_value_paise >= liquidity_floor_paise
  list_industry TEXT NOT NULL,             -- the list's own "Industry" column (NSE's sector name)
  PRIMARY KEY (as_of, symbol)
);

-- NSE's four-level classification of a stock, as read on `as_of` (today's version applies to the whole period).
CREATE TABLE sectors (
  symbol TEXT NOT NULL,
  as_of TEXT NOT NULL,
  macro TEXT NOT NULL,                     -- macro-economic sector
  sector TEXT NOT NULL,
  industry TEXT NOT NULL,
  basic_industry TEXT NOT NULL,
  PRIMARY KEY (symbol, as_of)
);
