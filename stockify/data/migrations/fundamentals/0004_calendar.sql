-- fundamentals.sqlite version 4 (FS 7): the upcoming results calendar, as NSE's event calendar lists board meetings
-- to consider financial results. Derived from NSE's answer (kept raw under nse/raw/fundamentals/event-calendar).

CREATE TABLE results_calendar (
  symbol TEXT NOT NULL,
  meeting_date TEXT NOT NULL,              -- IST date of the board meeting, YYYY-MM-DD
  purpose TEXT NOT NULL,                   -- NSE's purpose text, e.g. "Financial Results"
  period_hint TEXT NOT NULL,               -- "Jul–Sep quarter": the period the results most likely cover
  read_on TEXT NOT NULL,                   -- IST date NSE's calendar was read
  PRIMARY KEY (symbol, meeting_date)
);
