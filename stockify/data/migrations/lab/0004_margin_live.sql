-- IWB 5.1: where Kite's answer and the model disagree. One row per request the margin poller made for a paper
-- session's book (step 'current'); an error row has `error` and no Kite columns. Columns are added to the table
-- 0001 created, so nothing is dropped. The whole file runs in one transaction (see 0002).
ALTER TABLE margin_live ADD COLUMN engine_ns INTEGER;         -- the stamp of the snapshot the request priced
ALTER TABLE margin_live ADD COLUMN trade_date TEXT;           -- IST, of engine_ns
ALTER TABLE margin_live ADD COLUMN legs_json TEXT;            -- [[instrument, signed units], ...] the hash covers
ALTER TABLE margin_live ADD COLUMN market_json TEXT;          -- underlying, portfolio type, expiry_day, legs, SPAN-file age
ALTER TABLE margin_live ADD COLUMN span_file TEXT;            -- the SPAN file in force at the request
ALTER TABLE margin_live ADD COLUMN step TEXT;                 -- 'current' (the booked portfolio)
ALTER TABLE margin_live ADD COLUMN kite_total_paise INTEGER;  -- span + exposure + additional (D-62: no premium)
ALTER TABLE margin_live ADD COLUMN ours_total_paise INTEGER;  -- the staircase's total for the same legs
ALTER TABLE margin_live ADD COLUMN gap_paise INTEGER;         -- ours - Kite's
ALTER TABLE margin_live ADD COLUMN gap_ppm INTEGER;           -- gap / Kite's, in parts per million (NULL when Kite's is 0)
ALTER TABLE margin_live ADD COLUMN request_ns INTEGER;
ALTER TABLE margin_live ADD COLUMN response_ns INTEGER;
ALTER TABLE margin_live ADD COLUMN error TEXT;                -- redacted
CREATE INDEX IF NOT EXISTS ix_margin_live_wall ON margin_live (wall_ns);
