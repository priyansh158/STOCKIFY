/**
 * Typed fetch wrapper for the STOCKIFY FastAPI backend.
 *
 * All paths are relative — Next.js rewrites `/api/*` to the FastAPI origin
 * (see `next.config.ts`). Server Components can call these too via the
 * absolute URL passed through `STOCKIFY_API_ORIGIN`.
 */

const ABSOLUTE_BASE =
  typeof window === "undefined"
    ? process.env.STOCKIFY_API_ORIGIN ?? "http://127.0.0.1:8001"
    : "";

export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) {
    super(message);
    this.name = "ApiError";
  }
}

export async function api<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const url = ABSOLUTE_BASE + path;
  const res = await fetch(url, {
    cache: "no-store",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    let body: unknown;
    try { body = await res.json(); } catch { body = await res.text().catch(() => null); }
    const detail =
      body && typeof body === "object" && body !== null && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, detail, body);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** SSE open helper — used by the analyze page in P2. */
export function openSSE(path: string): EventSource {
  return new EventSource(ABSOLUTE_BASE + path);
}
