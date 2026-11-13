/**
 * Calls and plain-words helpers for the paper-trading pages (/api/trade/...). The API only reads the lab and queues
 * commands for the daemon: there is no call here, or anywhere in the API, that places an order.
 * With NEXT_PUBLIC_STOCKIFY_FIXTURES=1 the list page answers from small fixtures so it can be built without the API.
 */
import { api } from "./api";
import { pathSegment } from "./pathSeg";
import type {
  CommandStatusDTO,
  KiteStatus,
  SessionCommandDTO,
  SessionCreatedDTO,
  SessionDetailDTO,
  SessionDTO,
  SessionFillDTO,
  SessionMarginDTO,
  SessionOrderDTO,
  SessionPositionDTO,
  SessionTimelineDTO,
  TradeLoginDTO,
} from "./types";

const FIXTURES = process.env.NEXT_PUBLIC_STOCKIFY_FIXTURES === "1";
const sid = pathSegment;

const FIXTURE_SESSIONS: SessionDTO[] = [
  {
    session_id: "paper-20261111-a1b2c3", name: "orb_nifty", status: "RUNNING", pause_reasons: [],
    today_pnl_paise: 125_050, open_positions: 2, coverage_today: 0.998, parity_last: "pass", labels: [],
    created_ist: "2026-11-09T09:00:00+05:30",
  },
  {
    session_id: "paper-20261110-d4e5f6", name: "sma_reliance", status: "PAUSED", pause_reasons: ["owner", "token_expired"],
    today_pnl_paise: null, open_positions: 0, coverage_today: null, parity_last: null, labels: [],
    created_ist: "2026-11-08T09:00:00+05:30",
  },
];

export function listSessions(): Promise<SessionDTO[]> {
  return FIXTURES ? Promise.resolve(FIXTURE_SESSIONS) : api<SessionDTO[]>("/api/trade/sessions");
}

export function getSession(id: string): Promise<SessionDetailDTO> {
  return api<SessionDetailDTO>(`/api/trade/sessions/${sid(id)}`);
}

export function getPositions(id: string): Promise<SessionPositionDTO[]> {
  return api<SessionPositionDTO[]>(`/api/trade/sessions/${sid(id)}/positions`);
}

export function getOrders(id: string, limit = 200): Promise<SessionOrderDTO[]> {
  return api<SessionOrderDTO[]>(`/api/trade/sessions/${sid(id)}/orders?limit=${limit}`);
}

export function getFills(id: string, limit = 200): Promise<SessionFillDTO[]> {
  return api<SessionFillDTO[]>(`/api/trade/sessions/${sid(id)}/fills?limit=${limit}`);
}

export function getTimeline(id: string, limit = 200): Promise<SessionTimelineDTO[]> {
  return api<SessionTimelineDTO[]>(`/api/trade/sessions/${sid(id)}/timeline?limit=${limit}`);
}

export function getMargin(id: string, limit = 200): Promise<SessionMarginDTO[]> {
  return api<SessionMarginDTO[]>(`/api/trade/sessions/${sid(id)}/margin?limit=${limit}`);
}

export function getLogin(): Promise<TradeLoginDTO> {
  return api<TradeLoginDTO>("/api/trade/login");
}

export function getKiteStatus(): Promise<KiteStatus> {
  return api<KiteStatus>("/api/kite/status");
}

export function createSession(config: Record<string, unknown>): Promise<SessionCreatedDTO> {
  return api<SessionCreatedDTO>("/api/trade/sessions", { method: "POST", body: JSON.stringify({ config }) });
}

export type Control = "pause" | "resume" | "stop";

export function sendControl(id: string, verb: Control): Promise<SessionCommandDTO> {
  return api<SessionCommandDTO>(`/api/trade/sessions/${sid(id)}/${verb}`, { method: "POST" });
}

export function stopAll(): Promise<SessionCommandDTO> {
  return api<SessionCommandDTO>("/api/trade/stop-all", { method: "POST" });
}

export function getCommand(cmdId: number): Promise<CommandStatusDTO> {
  return api<CommandStatusDTO>(`/api/trade/commands/${cmdId}`);
}

/** Starts the Kite login (same flow as the portfolio page): asks the API for the Zerodha URL and goes there. */
export async function startKiteLogin(): Promise<void> {
  const { url } = await api<{ url: string }>("/api/kite/login_url");
  window.location.assign(url);
}

// ── plain words ──────────────────────────────────────────────────────────────

const STATUS_TEXT: Record<string, string> = {
  CREATED: "Created, not started",
  ARMED: "Ready for the next open",
  RUNNING: "Running",
  PAUSED: "Paused",
  STOPPED: "Stopped",
};

export function statusText(status: string): string {
  return STATUS_TEXT[status] ?? status;
}

/** Why a session is paused, as a sentence an owner can act on. An unknown code is shown unchanged. */
const PAUSE_TEXT: Record<string, string> = {
  owner: "You paused it.",
  stop_all: "Stop all was pressed.",
  token_expired: "The Kite login ran out. Log in to Kite, then resume.",
  parity_failure: "The check that replays the day did not match what the live run did.",
  determinism: "Running the same day twice gave different results.",
  error: "The session ran into an error.",
  stopped: "It was stopped.",
};

export function pauseReasonText(reason: string): string {
  return PAUSE_TEXT[reason] ?? reason;
}

/** The result of the replay check, in two or three words. */
export function parityText(parity: string | null | undefined): string {
  if (parity === "pass") return "Matched";
  if (parity === "fail") return "Did not match";
  if (!parity) return "Not run yet";
  return parity;
}

/** What the replay check is, once per page. A mismatch pauses the session (parity.py `_pause`). */
export const PARITY_HELP =
  "After a day ends, STOCKIFY plays that day again from the recorded prices and compares it with what the session did. " +
  "If they do not match, the session is paused.";

/** A command's outcome for the owner. */
export function commandText(c: CommandStatusDTO): string {
  const what = c.command === "stop_all" ? "Stop all" : c.command.charAt(0).toUpperCase() + c.command.slice(1);
  if (c.status === "applied") return `${what} took effect.`;
  if (c.status === "failed") return `${what} did not work${c.error ? `: ${c.error}` : "."}`;
  return `${what} is queued. It takes effect when the background service picks it up.`;
}

/** An order's status in plain words; an unknown one is shown unchanged. */
const ORDER_STATUS_TEXT: Record<string, string> = {
  PENDING: "Waiting",
  OPEN: "Open",
  TRIGGER_PENDING: "Waiting for its trigger price",
  COMPLETE: "Filled",
  CANCELLED: "Cancelled",
  REJECTED: "Rejected",
  SLICED: "Split into smaller orders",
};

export function orderStatusText(status: string | null | undefined): string {
  if (!status) return "Unknown";
  return ORDER_STATUS_TEXT[status] ?? status;
}

/** What a timeline entry says, in plain words; an unknown kind is shown with underscores as spaces. */
const TIMELINE_TEXT: Record<string, string> = {
  login: "Kite login state changed",
  pause: "Paused",
  resume: "Resumed",
  start: "Started",
  end: "Day ended",
  new_day: "New trading day began",
  parity: "Replay check finished",
  late_login: "Kite login came late",
  recovered: "Recovered after a restart",
  stop_requested: "Stop requested",
  session_failed: "Session failed",
  gap: "Gap in market data",
};

export function timelineText(kind: string): string {
  const t = TIMELINE_TEXT[kind];
  if (t) return t;
  const s = kind.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Signed rupees for a gain or loss: "+₹1,250.50", "-₹300.00", or "—" when unknown. */
export function signedRupees(paise: number | null | undefined, rupees: (p: number | null | undefined) => string): string {
  if (paise === null || paise === undefined) return "—";
  return `${paise > 0 ? "+" : ""}${rupees(paise)}`;
}

export function tone(paise: number | null | undefined): "up" | "down" | undefined {
  if (paise === null || paise === undefined || paise === 0) return undefined;
  return paise > 0 ? "up" : "down";
}
