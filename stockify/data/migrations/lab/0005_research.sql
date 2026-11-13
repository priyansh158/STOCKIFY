-- AM 6.2 (M6): run roles, research jobs, an append-only holdout-unlock log, and runs that are never deleted.
-- Additive only (I-13): columns are added to tables 0001 created, nothing is dropped. The whole file runs in one
-- transaction (see 0002).
ALTER TABLE runs ADD COLUMN role TEXT NOT NULL DEFAULT 'trial';      -- trial | sensitivity | oos | rerun
ALTER TABLE runs ADD COLUMN job_id TEXT;                              -- the sweep / walk-forward / sensitivity job
ALTER TABLE trial_families ADD COLUMN named INTEGER NOT NULL DEFAULT 0;
ALTER TABLE holdout_unlocks ADD COLUMN job_id TEXT;
CREATE INDEX IF NOT EXISTS ix_runs_family ON runs (family_id, role, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_unlock_job ON holdout_unlocks (job_id, reason) WHERE job_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS research_jobs (job_id TEXT PRIMARY KEY, kind TEXT NOT NULL,   -- sweep | walkforward | sensitivity
  name TEXT, family_id TEXT, status TEXT NOT NULL, created_ns INTEGER NOT NULL, finished_ns INTEGER,
  folder TEXT, spec_json TEXT,     -- the job's input (sweep spec, windows parameters)
  state_json TEXT,                 -- what the job decided: windows, chosen params, train and test run ids
  error TEXT);
CREATE TRIGGER IF NOT EXISTS runs_are_never_deleted BEFORE DELETE ON runs
  BEGIN SELECT RAISE(ABORT, 'runs are never deleted from the registry (M6.4)'); END;
CREATE TRIGGER IF NOT EXISTS holdout_unlocks_no_update BEFORE UPDATE ON holdout_unlocks
  BEGIN SELECT RAISE(ABORT, 'holdout unlocks are a log (M6.6)'); END;
CREATE TRIGGER IF NOT EXISTS holdout_unlocks_no_delete BEFORE DELETE ON holdout_unlocks
  BEGIN SELECT RAISE(ABORT, 'holdout unlocks are a log (M6.6)'); END;
