-- fundamentals.sqlite version 5 (FS 5): the screen's ordered list as computed on a day, best first. Derived (rebuilt
-- by running the screen again); `json` is the public row (symbol, sector, peer_group, ratios, sector_medians,
-- reasons, short_reason): it carries no score.

CREATE TABLE screen_runs (
  day TEXT NOT NULL,                       -- IST date the screen was computed for, YYYY-MM-DD
  position INTEGER NOT NULL,               -- 1 = first in the list
  symbol TEXT NOT NULL,
  json TEXT NOT NULL,
  PRIMARY KEY (day, position)
);
