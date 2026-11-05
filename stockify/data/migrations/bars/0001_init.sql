-- bars.sqlite, version 1 (A1_schemas.md §2). Price basis: adjusted (K-6, I-5).

CREATE TABLE bars (
  instrument_id INTEGER, interval TEXT,          -- minute | 3minute | ... | day
  ts_ns INTEGER,                                 -- bar START, UTC
  open_paise INTEGER, high_paise INTEGER, low_paise INTEGER, close_paise INTEGER,
  volume INTEGER, oi INTEGER, fetched_on TEXT NOT NULL,
  PRIMARY KEY (instrument_id, interval, ts_ns)
);
CREATE TABLE bar_fetch_log (instrument_id INTEGER, interval TEXT, from_ns INTEGER, to_ns INTEGER,
  rows INTEGER, fetched_at_ns INTEGER, outcome TEXT);
CREATE INDEX ix_bar_fetch_log ON bar_fetch_log(instrument_id, interval, from_ns);
CREATE TABLE adjustments_detected (instrument_id INTEGER, detected_on TEXT, ratio REAL,
  sample_ts_ns INTEGER, action TEXT,             -- refetched | flagged_only (expired, not refetchable)
  PRIMARY KEY (instrument_id, detected_on));
