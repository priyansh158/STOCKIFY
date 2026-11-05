/**
 * TypeScript mirror of `server/schemas.py`. Keep these in sync — when a Pydantic
 * model on the backend changes shape, update the matching interface here.
 *
 * Single source of truth: the FastAPI OpenAPI doc at /api/docs and
 * /openapi.json. Future: codegen these from openapi.json instead of hand-typing.
 */

// ── Search ───────────────────────────────────────────────────────────────────

export interface SearchHit {
  ticker: string;
  name: string;
  exchange: "NSE" | "BSE";
  sector?: string | null;
  instrument_type: string;
  source: "kite" | "yfinance";
  instrument_token?: number | null;
}

// ── Portfolio ────────────────────────────────────────────────────────────────

export interface Holding {
  symbol: string;
  exchange: string;
  quantity: number;
  average_price: number;
  last_price: number;
  pnl: number;
  pnl_pct: number;
  instrument_token?: number;
}

export interface PortfolioSnapshot {
  holdings: Holding[];
  total_value: number;
  total_pnl: number;
  fetched_at: number;
  stale?: boolean;
  error?: string | null;
}

// ── Geo alerts ───────────────────────────────────────────────────────────────

export interface GeoAlert {
  id: number;
  created_at: number;
  severity: string;
  disruption_score: number;
  scope?: string | null;
  title: string;
  source?: string | null;
  url?: string | null;
  published_at?: string | null;
  guid?: string | null;
  why_disruptive?: string | null;
  investor_action?: string | null;
  confidence?: number | null;
  affected_tickers: string[];
  emailed_at?: number | null;
  acknowledged_at?: number | null;
}

// ── Kite ─────────────────────────────────────────────────────────────────────

export interface KiteStatus {
  configured: boolean;
  authenticated: boolean;
  error?: string | null;
}

// ── Data layer (M1) ──────────────────────────────────────────────────────────

export interface RecorderStatus {
  wall_ns: number;
  connected: boolean;
  subs_full: number;
  subs_quote: number;
  packets_per_s: number;
  queue_depth: number;
  writer_lag_ms?: number | null;
  last_packet_age_ms: Record<string, number | null>;
  bytes_today: number;
  age_s: number;
}

export interface RecorderStatusResponse {
  status: RecorderStatus | null;
}

// ── Live ticks (WS) ──────────────────────────────────────────────────────────

export interface TickEvent {
  type: "tick";
  token: number;
  ltp: number;
  change_pct?: number;
  ts: number;
}
