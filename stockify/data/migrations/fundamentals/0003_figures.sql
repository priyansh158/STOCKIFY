-- fundamentals.sqlite version 3 (FS 3): the figures parsed from each filing's XBRL (stockify/fundamentals/xbrl.py).
-- Rebuildable from the raw XBRL files; a filing that is held has no rows here.

-- One row per field filed in the filing's own period: amounts in paise, EPS and face value in paise per share.
CREATE TABLE figures (
  filing_id TEXT NOT NULL,                 -- filings.filing_id
  field TEXT NOT NULL,                     -- one of xbrl.FIELDS
  value_paise INTEGER NOT NULL,
  value_raw TEXT NOT NULL,                 -- the element(s) and text as filed, e.g. "RevenueFromOperations 2438650000000.00"
  PRIMARY KEY (filing_id, field)
);
