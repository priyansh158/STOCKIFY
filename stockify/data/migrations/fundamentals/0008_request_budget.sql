-- fundamentals.sqlite version 8 (FS 8.3): the NSE requests the fundamentals job has sent for each trading day, so
-- the day's request budget (fundamentals.json daily_request_budget, ruling R12) holds across the daemon's retries.

CREATE TABLE request_budget (
  day TEXT PRIMARY KEY,                    -- IST trade date the job ran for, YYYY-MM-DD
  spent INTEGER NOT NULL                   -- NSE requests sent by the budgeted runs for that day (warm-ups, retries)
);
