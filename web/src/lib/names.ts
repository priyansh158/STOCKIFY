/**
 * Pure helpers for how names read on the pages (checked from Python by tests/stockify/names_check.ts). No React, no fetching.
 * Strategy names, order tags and setting labels come from the server in plain words (stockify.display); these are for the
 * texts the person chose themselves (a run's or session's name), for the few code words the server does not send, and for
 * the friendly default name the New test and New session forms suggest.
 */

/** "Cmp eod straddle spanlite" for "cmp_eod_straddle_spanlite": a name the person chose, shown as words. The stored name (and every id made from it) is unchanged. */
export function nameWords(name?: string | null): string {
  const t = (name ?? "").replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  return t ? t[0].toUpperCase() + t.slice(1) : "";
}

/** Any code-like word as plain words in sentence case: "refdata_stale" gives "Refdata stale", "R_MARGIN" gives "R margin". */
export function wordsOf(code: string): string {
  const t = code.replace(/[_.:\-]+/g, " ").replace(/\s+/g, " ").trim().toLowerCase();
  return t ? t[0].toUpperCase() + t.slice(1) : code;
}

export function upperFirst(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

/** The run's status as the lists say it. */
export const STATUS_WORDS: Record<string, string> = {
  queued: "Waiting", running: "Running", done: "Finished", failed: "Failed", cancelled: "Cancelled",
};

export function statusWords(status: string): string {
  return STATUS_WORDS[status] ?? wordsOf(status);
}

/** Which engine ran the test, in words. */
export function implWords(impl?: string | null): string | null {
  if (!impl) return null;
  if (impl === "engine") return "C++ engine";
  if (impl === "oracle") return "Python reference engine";
  return wordsOf(impl);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-06-01" as "Jun 2026"; "" when it is not a date. */
export function monthYear(iso: string): string {
  const m = /^(\d{4})-(\d{2})-\d{2}/.exec(iso.trim());
  return m && Number(m[2]) >= 1 && Number(m[2]) <= 12 ? `${MONTHS[Number(m[2]) - 1]} ${m[1]}` : "";
}

/** The name the forms suggest: "Opening Range Breakout · RELIANCE · Jun 2026" (whatever parts are known). */
export function suggestedName(strategyName: string, symbolsText: string, from?: string): string {
  const first = symbolsText.split(",")[0]?.trim().split(":").pop()?.trim() ?? "";
  return [strategyName, first, from ? monthYear(from) : ""].filter(Boolean).join(" · ");
}

/** The longest run name the lab accepts (stockify.runconfig). */
export const RUN_NAME_MAX = 64;

/**
 * A run's name as a safe folder-name segment, which is what the lab requires of a run name (letters, digits, "." "_" "-",
 * starting with a letter or digit, 64 at most): spaces and other marks become "_". "Opening Range Breakout · RELIANCE · Jun 2026"
 * gives "Opening_Range_Breakout_RELIANCE_Jun_2026". Never empty ("test" when nothing is left).
 */
export function runNameSlug(text: string): string {
  const s = text.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[^A-Za-z0-9]+/, "").replace(/[_.-]+$/, "");
  const cut = s.slice(0, RUN_NAME_MAX).replace(/[_.-]+$/, "");
  const name = cut || "test";
  return /^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$/i.test(name) ? `run_${name}` : name;   // a Windows device name is refused
}
