-- Symbol lookups for every kind (instruments.find): the partial unique index
-- ux_instr_symbol only serves queries that also say kind IN ('EQ','INDEX').
CREATE INDEX ix_instr_symbol ON instruments(exchange, tradingsymbol);
