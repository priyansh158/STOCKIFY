/** Pure: when "Stop all" has something to stop. No imports, so Node can run it in the checks. */

/**
 * The statuses Stop all acts on: every session that is not STOPPED (the daemon pauses a running one and keeps a created or
 * armed one from starting; a STOPPED session refuses any command). With none of those the bar would be a button that does nothing.
 */
export function canStopAll(sessions: ReadonlyArray<{ status: string }> | null | undefined): boolean {
  return sessions?.some((s) => s.status !== "STOPPED") ?? false;
}
