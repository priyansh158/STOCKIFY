-- fundamentals.sqlite version 6 (FS 9): what the details page reads for each listed stock, computed once when the
-- day's list is saved (series.precompute) instead of on every request. Derived: rebuilt by saving the list again.

-- Each listed stock's figures quarter by quarter with its peer group's median at each quarter end: the json is
-- series.peer_series' list [{key, label, unit, points: [{period_end, value, sector_median}]}]. It carries no score.
CREATE TABLE screen_series (
  day TEXT NOT NULL,                       -- IST date of the list (screen_runs.day)
  symbol TEXT NOT NULL,
  json TEXT NOT NULL,
  PRIMARY KEY (day, symbol)
);

-- The universe a day's list was computed from (universe.as_of), so the page names and counts against that universe
-- even after a newer one is read.
CREATE TABLE screen_days (
  day TEXT PRIMARY KEY,                    -- IST date of the list
  universe_as_of TEXT                      -- universe.as_of in force when the list was saved; NULL when none
);
