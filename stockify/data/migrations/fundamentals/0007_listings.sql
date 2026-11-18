-- fundamentals.sqlite version 7 (FS 8): when each company's NSE results listings were last read in full, so the daily
-- job lists every company once a week and the backfill resumes after the companies already listed.

CREATE TABLE filings_listed (
  symbol TEXT PRIMARY KEY,
  listed_on TEXT NOT NULL,                 -- IST date both listings were last read and recorded (filings.list_for)
  listed_ns INTEGER NOT NULL               -- UTC epoch ns of that read, on the Http's clock
);
