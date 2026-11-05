-- Early expiry (D-41): NSE expires a stock's whole F&O series before the
-- scheduled date on some corporate actions (mergers, demergers). `expiry`
-- stays the scheduled date (the contract's identity, as in Kite's symbol);
-- `actual_expiry` is the day it really expired. NULL = as scheduled.
ALTER TABLE instruments ADD COLUMN actual_expiry TEXT;
CREATE INDEX ix_instr_actual_expiry ON instruments(actual_expiry) WHERE actual_expiry IS NOT NULL;
