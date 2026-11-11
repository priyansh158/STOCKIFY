-- version 2: a gap can name the frames the recording lacks (RM 5.3). Set only for reason `queue_full`
-- (the writer queue overflowed and the frame was never landed); NULL on every other gap.
ALTER TABLE gaps ADD COLUMN first_frame_no INTEGER;     -- first dropped frame_no of the run
ALTER TABLE gaps ADD COLUMN last_frame_no INTEGER;      -- last dropped frame_no (consecutive run, inclusive)
ALTER TABLE gaps ADD COLUMN frames INTEGER;             -- last_frame_no - first_frame_no + 1
