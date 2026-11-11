-- M5: session ids become journal folder names, and Windows folder names ignore case, so ids must be unique
-- ignoring case (S1 and s1 would share one journal folder). Lookups stay exact.
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_id_nocase ON sessions (session_id COLLATE NOCASE);
