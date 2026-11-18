-- fundamentals.sqlite version 2 (FS 2): NSE's results filings per company and where each XBRL file was kept.
-- One row per filing as NSE listed it. A later filing for the same period (a new broadcast) is a new row: a
-- restatement is a new version, never an overwrite. Rows are never deleted.

CREATE TABLE filings (
  filing_id TEXT PRIMARY KEY,              -- sha256 of symbol, period_end, period_kind, basis, broadcast_ns, xbrl_url
  symbol TEXT NOT NULL,
  period_start TEXT,                       -- ISO date the figures start (fromDate); NULL when NSE gives none
  period_end TEXT NOT NULL,                -- ISO date the period ends (toDate / qe_Date)
  period_kind TEXT NOT NULL,               -- Q | H | A
  basis TEXT NOT NULL,                     -- consolidated | standalone
  broadcast_ns INTEGER,                    -- UTC epoch ns of NSE's broadcast (listed in IST); NULL when NSE gives none
  broadcast_source TEXT,                   -- the listing field the time came from: broadCastDate | exchdisstime |
                                           -- filingDate | broadcast_Date; NULL when none had one
  source TEXT NOT NULL,                    -- results (corporates-financial-results) | integrated (integrated-filing-results)
  seq_number TEXT,                         -- NSE's own sequence number for the filing
  xbrl_url TEXT,                           -- NULL when NSE lists no XBRL file
  xbrl_sha256 TEXT,                        -- of the kept file, once downloaded
  xbrl_file TEXT,                          -- path under nse/raw/fundamentals/xbrl ("2024/RELIANCE_..."), once downloaded
  status TEXT NOT NULL,                    -- listed | downloaded | parsed | held
  reason TEXT                              -- plain words: why held, or the last download failure
);

CREATE INDEX filings_symbol ON filings (symbol, period_end);
CREATE INDEX filings_status ON filings (status);
