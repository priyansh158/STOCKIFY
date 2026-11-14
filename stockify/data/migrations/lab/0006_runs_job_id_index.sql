-- DM 7.4 (M11): the web's progress poll counts a research job's runs by job_id every 1.5 s; an index keeps it cheap.
-- Additive (I-13): nothing is dropped or rewritten.
CREATE INDEX IF NOT EXISTS runs_job_id ON runs (job_id);
