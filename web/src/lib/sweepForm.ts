/**
 * The pure parts of the sweep form: turn what the owner typed for each setting into the values to try, count the
 * tries live, and say in words why Run is disabled. The server stays authoritative (it checks every value again with
 * the catalogue's own rules); this only lets the form point at a problem before sending. Checked by
 * tests/stockify/test_jobs_checks.py. No imports of the fetch wrapper, so a plain script can load it.
 */
import { validateParams } from "./params";
import type { AxisValue, ResearchLimitsDTO, StrategyParam } from "./types";

/** At most this many values for one setting; the API's `SweepRequestDTO.axes` allows the same. */
export const MAX_VALUES = 100;
export const PARAM_PREFIX = "strategy.params.";

const ZERO = BigInt(0);
const ONE = BigInt(1);
const TEN = BigInt(10);

type Decimal = { n: bigint; scale: number };

function parseDecimal(text: string): Decimal | null {
  const m = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(text.trim());
  if (!m) return null;
  const frac = m[3] ?? "";
  const n = BigInt(`${m[2]}${frac}`) * (m[1] === "-" ? -ONE : ONE);
  return { n, scale: frac.length };
}

function toScale(d: Decimal, scale: number): bigint {
  return d.n * TEN ** BigInt(scale - d.scale);
}

function format(n: bigint, scale: number): string {
  const neg = n < ZERO;
  let s = (neg ? -n : n).toString().padStart(scale + 1, "0");
  if (scale > 0) {
    s = `${s.slice(0, s.length - scale)}.${s.slice(s.length - scale)}`.replace(/\.?0+$/, "");
  }
  return `${neg ? "-" : ""}${s}`;
}

const RANGE = /^\s*(\S+)\s+to\s+(\S+)\s+by\s+(\S+)\s*$/i;

function quoted(s: string): string {
  return `“${s}”`;
}

/** What the owner typed for one setting → the values to try. Empty text keeps the test's own value (no values).
 * "5, 10, 15" is a list; "5 to 30 by 5" is a range (whole or decimal numbers, exact: no float arithmetic). */
export function parseValues(param: StrategyParam, text: string): { values: AxisValue[]; problem: string | null } {
  const t = text.trim();
  if (t === "") return { values: [], problem: null };
  let raw: AxisValue[];
  const range = RANGE.exec(t);
  if (range) {
    if (param.type !== "int" && param.type !== "decimal") {
      return { values: [], problem: "A range like 5 to 30 by 5 works for numbers only. List the values with commas instead." };
    }
    const [from, to, by] = [parseDecimal(range[1]), parseDecimal(range[2]), parseDecimal(range[3])];
    if (!from || !to || !by) return { values: [], problem: "Write a range with plain numbers, like 5 to 30 by 5." };
    const scale = Math.max(from.scale, to.scale, by.scale);
    const [a, b, s] = [toScale(from, scale), toScale(to, scale), toScale(by, scale)];
    if (s <= ZERO) return { values: [], problem: "The step of a range must be above zero." };
    if (a > b) return { values: [], problem: "A range goes up: the first number must not be above the last." };
    const count = (b - a) / s + ONE;
    if (count > BigInt(MAX_VALUES)) return { values: [], problem: `That range has ${count} values; the most is ${MAX_VALUES}.` };
    raw = [];
    for (let v = a; v <= b; v += s) {
      const shown = format(v, scale);
      raw.push(param.type === "int" ? (/\./.test(shown) ? shown : Number(shown)) : shown);
    }
  } else {
    raw = t
      .split(",")
      .map((x) => x.trim())
      .filter((x) => x !== "")
      .map((x) => (param.type === "int" && /^[+-]?\d+$/.test(x) ? Number(x) : x));
  }
  if (raw.length > MAX_VALUES) return { values: [], problem: `That is ${raw.length} values; the most is ${MAX_VALUES}.` };
  for (const v of raw) {
    const why = validateParams([{ ...param, required: false }], { [param.name]: v })[0];
    if (why) return { values: [], problem: `${quoted(String(v))} ${why.slice(why.indexOf(": ") + 2)}.` };
  }
  const seen = new Set<string>();
  for (const v of raw) {
    const k = JSON.stringify(v);
    if (seen.has(k)) return { values: [], problem: `${quoted(String(v))} is listed twice.` };
    seen.add(k);
  }
  return { values: raw, problem: null };
}

/** A yes/no setting: "keep", "try both" (or force one value). */
export function boolChoice(choice: "keep" | "both" | "true" | "false"): AxisValue[] {
  return choice === "both" ? [true, false] : choice === "true" ? [true] : choice === "false" ? [false] : [];
}

/** How many tries a set of value lists makes: the product of their lengths (an unchanged setting adds nothing). */
export function triesCount(lists: AxisValue[][]): number {
  return lists.filter((l) => l.length > 0).reduce((n, l) => n * l.length, 1);
}

export interface SweepPlan {
  axes: Record<string, AxisValue[]>;
  tries: number;
  problems: string[];
  /** Why Run is disabled, in words; null when it can run. */
  reason: string | null;
  needsConfirm: boolean;
}

/** `entries`: each setting's name, label and what was chosen for it. `confirmed`: the "run this many" tick. */
export function planSweep(
  entries: { param: StrategyParam; values: AxisValue[]; problem: string | null }[],
  limits: Pick<ResearchLimitsDTO, "max_trials" | "confirm_above">,
  hasRun: boolean,
  confirmed: boolean,
): SweepPlan {
  const axes: Record<string, AxisValue[]> = {};
  const problems: string[] = [];
  for (const e of entries) {
    if (e.problem) problems.push(`${e.param.label || e.param.name}: ${e.problem}`);
    if (e.values.length > 0) axes[`${PARAM_PREFIX}${e.param.name}`] = e.values;
  }
  const tries = triesCount(Object.values(axes));
  const needsConfirm = tries > limits.confirm_above;
  let reason: string | null = null;
  if (!hasRun) reason = "Choose a finished test to start from.";
  else if (problems.length > 0) reason = "Fix the settings marked above first.";
  else if (Object.keys(axes).length === 0) reason = "Give at least one setting more than one value to try.";
  else if (tries < 2) reason = "That is a single try. List two or more values for a setting.";
  else if (tries > limits.max_trials) reason = `That is ${tries.toLocaleString("en-IN")} tries and the most is ${limits.max_trials.toLocaleString("en-IN")}. Offer fewer values.`;
  else if (needsConfirm && !confirmed) reason = `Tick the box to confirm that you want to run ${tries.toLocaleString("en-IN")} tries.`;
  return { axes, tries, problems, reason, needsConfirm };
}

/** "15 tries", "1 try". */
export function triesWords(n: number): string {
  return `${n.toLocaleString("en-IN")} ${n === 1 ? "try" : "tries"}`;
}

export const OBJECTIVE_CHOICES: { value: string; label: string }[] = [
  { value: "sharpe", label: "Steadiest returns (Sharpe)" },
  { value: "sortino", label: "Steadiest, counting only losing days (Sortino)" },
  { value: "calmar", label: "Yearly return per worst fall (Calmar)" },
  { value: "cagr", label: "Yearly growth rate" },
  { value: "net_pnl", label: "Profit after costs" },
];

/** The walk-forward form's inputs → a request body or the first problem in words. Empty fields use research.json's defaults on the server. */
export function walkforwardRequest(
  sweepJobId: string,
  f: { train: string; test: string; step: string; objective: string; first: string; last: string },
): { body: Record<string, unknown> | null; problem: string | null } {
  const body: Record<string, unknown> = { sweep_job_id: sweepJobId };
  for (const [key, text, label] of [["train_days", f.train, "Training days"], ["test_days", f.test, "Test days"], ["step_days", f.step, "Step days"]] as const) {
    const t = text.trim();
    if (!/^\d{1,5}$/.test(t) || Number(t) < 1 || Number(t) > 10000) return { body: null, problem: `${label}: a whole number from 1 to 10,000.` };
    body[key] = Number(t);
  }
  if ((body.step_days as number) < (body.test_days as number)) {
    return { body: null, problem: "The step must be at least as long as the test period, or the test periods would overlap." };
  }
  body.objective = f.objective;
  for (const [key, text, label] of [["first_day", f.first, "First day"], ["last_day", f.last, "Last day"]] as const) {
    const t = text.trim();
    if (t === "") continue;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(t)) return { body: null, problem: `${label}: a date like 2026-03-31.` };
    body[key] = t;
  }
  if (body.first_day && body.last_day && (body.first_day as string) > (body.last_day as string)) {
    return { body: null, problem: "The first day is after the last day." };
  }
  return { body, problem: null };
}
