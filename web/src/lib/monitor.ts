/**
 * Words for the news monitor on the Geo alerts page (D-150): when it last checked, and why the "Check the news now"
 * button is off. Pure, so tests/stockify/test_geo_check_checks.py can check them on fixed inputs.
 */
import type { GeoMonitorStatus } from "./types";

/** One line about the last finished check. `when` is the time already written in IST. Nothing is claimed about
 * whether the scheduled monitor is installed: the app reads only what the monitor leaves behind. */
export function lastCheckWords(status: GeoMonitorStatus | null, when: string): string {
  if (!status) return "When the news was last checked could not be read.";
  if (!status.last_check_at) return "No check has been recorded yet.";
  const by = status.last_by === "background" ? " by the background monitor" : status.last_by === "page" ? " from this page" : "";
  return `Last check: ${when}${by}${status.last_plain ? `. ${status.last_plain}` : "."}`;
}

/** Why the button cannot be pressed now, or null when it can. A running check of this page shows its own progress. */
export function checkBlockedReason(running: boolean, checkingElsewhere: boolean): string | null {
  if (running) return null;
  if (checkingElsewhere) return "The background monitor is checking right now. Reload this page in a minute.";
  return null;
}
