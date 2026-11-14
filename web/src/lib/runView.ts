/**
 * Pure helpers for how a run's strategy and fills read on the run pages (checked from Python by
 * tests/stockify/run_view_check.ts). No React, no fetching.
 */
import type { TestFill } from "./types";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-10-05" as "5 Oct 2026". */
function dayWords(iso: string): string {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return `${d} ${MONTHS[m - 1]} ${y}`;
}

/**
 * What the Time column of the Fills table shows for a fill, and the explanation for a tooltip (null when none is needed).
 * A fill the engine priced at the day's close (`at_close`, sent by the server from the fill's evidence) has no moment of
 * its own: its stamp is when the exchange publishes the end-of-day file, so it reads as the day, "at the close", with the
 * stamp explained. Every other fill reads as its IST date and minute.
 */
export function fillWhen(f: Pick<TestFill, "t_ist" | "at_close">): { text: string; hint: string | null } {
  if (f.at_close) {
    return {
      text: `${dayWords(f.t_ist)}, at the close`,
      hint:
        `This fill is the day's closing price. The close is only known once the exchange publishes its end-of-day file, ` +
        `so the simulator stamps it ${f.t_ist.slice(11, 16)} IST, the publication time. Nothing traded at that minute.`,
    };
  }
  return { text: `${dayWords(f.t_ist)}, ${f.t_ist.slice(11, 16)}`, hint: null };
}

/**
 * The note beside a fill's "Show on chart" control when the fill has no marker on the chart now showing; null when the
 * chart is not drawn at all (no price data: the chart's own notice says so) or has not answered yet.
 */
export function chartHint(chartDrawn: boolean, markerId: string | null): string | null {
  if (!chartDrawn || markerId !== null) return null;
  return "Not on the chart now showing (another instrument or time range)";
}
