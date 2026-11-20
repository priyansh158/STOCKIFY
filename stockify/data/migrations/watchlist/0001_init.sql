-- watchlist.sqlite version 1 (CW 2): the stocks the owner watches, the alerts raised on them, and a change counter.
-- Not derived data (the list is the owner's choice): backed up with the other stores, never dropped.

-- The watched stocks, in the order they were added.
CREATE TABLE watchlist (
  instrument_id INTEGER PRIMARY KEY,             -- refdata instruments NSE equity
  symbol TEXT NOT NULL,                          -- NSE symbol, upper case
  added_ns INTEGER NOT NULL,                     -- UTC epoch ns
  position INTEGER NOT NULL                      -- 1, 2, 3 ... as added; a gap after a removal is fine
);
CREATE UNIQUE INDEX ux_watchlist_symbol ON watchlist(symbol);

-- Every alert raised, kept after a stock leaves the list. The unique key is the once-only record: it survives a restart.
CREATE TABLE watch_alerts (
  instrument_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('high52', 'low52', 'results')),
  ref TEXT NOT NULL,                             -- what makes it the same alert: the IST day for a 52-week cross, the meeting date for results
  level_paise INTEGER,                           -- the day's high or low that crossed (52-week alerts)
  prior_paise INTEGER,                           -- the prior 52-week high or low it crossed
  prior_date TEXT,                               -- IST date of that prior high or low
  at_ns INTEGER NOT NULL,                        -- UTC epoch ns the alert was found
  delivery TEXT NOT NULL DEFAULT 'pending',      -- pending | sent | capped | failed
  UNIQUE (instrument_id, kind, ref)
);
CREATE INDEX ix_watch_alerts_at ON watch_alerts(at_ns);

-- One row: bumped on every change to the list, so a running evaluator notices an add or a removal.
CREATE TABLE meta (
  version INTEGER NOT NULL
);
INSERT INTO meta (version) VALUES (0);
