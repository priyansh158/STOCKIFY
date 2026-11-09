-- M5: session bookkeeping. Plain ADD COLUMN is not idempotent, but migrate.apply runs the whole file in
-- one transaction (SQLite DDL is transactional), so a failure leaves none of it applied.
ALTER TABLE sessions ADD COLUMN strategy_label TEXT;
ALTER TABLE sessions ADD COLUMN pause_reasons_json TEXT NOT NULL DEFAULT '[]';   -- sorted set; pause_reason keeps the first, for display
ALTER TABLE session_days ADD COLUMN status TEXT;          -- open | finalised | failed
ALTER TABLE session_days ADD COLUMN finalised_ns INTEGER;
ALTER TABLE session_days ADD COLUMN coverage_json TEXT;
ALTER TABLE session_days ADD COLUMN labels_json TEXT;
ALTER TABLE session_days ADD COLUMN last_frame_no INTEGER;
ALTER TABLE session_days ADD COLUMN last_frame_recv_ns INTEGER;
CREATE INDEX IF NOT EXISTS ix_timeline_session ON session_timeline (session_id, wall_ns);
CREATE TABLE IF NOT EXISTS session_commands (cmd_id INTEGER PRIMARY KEY, session_id TEXT, -- NULL = every session
  command TEXT NOT NULL, args_json TEXT, created_ns INTEGER NOT NULL, applied_ns INTEGER, result TEXT);
