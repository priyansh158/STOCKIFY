-- ticks/landing/ticks_YYYYMMDD.sqlite, version 1 (A1_schemas.md §3.1).
CREATE TABLE packets (
  seq INTEGER PRIMARY KEY,                       -- strictly increasing within the day
  frame_no INTEGER NOT NULL,                     -- which WebSocket frame the packet came in
  recv_ns INTEGER NOT NULL,                      -- Clock.now_ns() at receipt (event time, C-1)
  mono_ns INTEGER NOT NULL,                      -- monotonic clock (latency diagnostics only)
  instrument_id INTEGER, instrument_token INTEGER NOT NULL,
  mode INTEGER NOT NULL,                         -- 1 ltp | 2 quote | 3 full
  pkt_len INTEGER NOT NULL,                      -- 8 | 28 | 32 | 44 | 184
  ltp INTEGER, last_qty INTEGER, atp INTEGER, volume INTEGER, buy_qty INTEGER, sell_qty INTEGER,
  open INTEGER, high INTEGER, low INTEGER, close INTEGER,
  last_trade_ts_s INTEGER, oi INTEGER, oi_high INTEGER, oi_low INTEGER, exch_ts_s INTEGER,
  raw BLOB NOT NULL                              -- exact packet bytes (D-08)
);
CREATE INDEX ix_packets_instr ON packets(instrument_id, seq);
CREATE TABLE gaps (gap_id INTEGER PRIMARY KEY, start_ns INTEGER, end_ns INTEGER, reason TEXT,
  scope TEXT);                                   -- all | tier:<name> | instrument:<id>
CREATE TABLE subscription_log (recv_ns INTEGER, action TEXT, instrument_id INTEGER,
  instrument_token INTEGER, mode TEXT, reason TEXT);
CREATE TABLE heartbeats (minute_ns INTEGER PRIMARY KEY, count INTEGER);
CREATE TABLE recorder_meta (key TEXT PRIMARY KEY, value TEXT);
-- keys: trade_date, universe_sha256, kiteconnect_version, recorder_version, parser_version, host, last_alive_ns
