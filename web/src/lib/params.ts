/**
 * Client-side twin of `stockify.strategies._catalogue.validate_params`. The server stays authoritative;
 * this only lets the form point at a problem before submitting. Messages match the server's.
 */
import type { StrategyParam } from "./types";

const HHMM = /^\s*\d{1,2}:\d{1,2}(:\d{1,2})?\s*$/;
const SYMBOL = /^([A-Z]+:)?\S.*$/;
const DIGITS = String.raw`\d+(?:_\d+)*`;
const DECIMAL = new RegExp(String.raw`^\s*[+-]?(?:${DIGITS}(?:\.(?:${DIGITS})?)?|\.${DIGITS})(?:[eE][+-]?${DIGITS})?\s*$`);

function typeProblem(type: StrategyParam["type"], v: unknown): string | null {
  switch (type) {
    case "int":
      if (typeof v === "boolean") return null;
      if (typeof v === "number") return Number.isInteger(v) ? null : "is not a whole number";
      if (typeof v === "string" && /^\s*[+-]?\d+(_\d+)*\s*$/.test(v)) return null;
      return "is not a whole number";
    case "bool":
      return typeof v === "boolean" ? null : "is not true or false";
    case "decimal":
      if (typeof v === "number") return Number.isFinite(v) ? null : "is not a decimal number";
      // Python's Decimal(): digits may be grouped with single underscores ("1_000.5")
      if (typeof v === "string" && DECIMAL.test(v)) return null;
      return "is not a decimal number";
    case "str":
    case "underlying":
      return typeof v === "string" && v.trim() ? null : "is not a non-empty text";
    case "symbol":
      return typeof v === "string" && SYMBOL.test(v.trim()) ? null : "is not a symbol like NSE:RELIANCE";
    case "hhmm": {
      if (typeof v !== "string" || !HHMM.test(v)) return "is not a time like 15:10";
      const parts = v.trim().split(":").map(Number);
      if (parts[0] > 23 || parts.slice(1).some((p) => p > 59)) return "is not a time like 15:10";
      return null;
    }
    default:
      return `has an unknown schema type '${String(type)}'`;
  }
}

function choiceProblem(p: StrategyParam, v: unknown): string | null {
  if (!p.choices?.length) return null;
  const text = String(v).trim();
  const ok = p.choices.some((c) => String(c.value) === text || (p.type !== "str" && p.type !== "symbol" && Number(c.value) === Number(text)));
  return ok ? null : `${JSON.stringify(v)} is not one of ${p.choices.map((c) => String(c.value)).join(", ")}`;
}

export function validateParams(schema: StrategyParam[], params: Record<string, unknown>): string[] {
  const problems: string[] = [];
  const names = new Set(schema.map((p) => p.name));
  for (const k of Object.keys(params).sort()) {
    if (!names.has(k)) problems.push(`${k}: unknown parameter`);
  }
  for (const p of schema) {
    const v = params[p.name];
    if (v === undefined || v === null) {
      if (p.required) problems.push(`${p.name}: required`);
      continue;
    }
    const why = typeProblem(p.type, v) ?? choiceProblem(p, v);
    if (why) problems.push(`${p.name}: ${why}`);
  }
  return problems;
}

/** Text typed into a form field → the JSON value sent for that param type ("" means not given). */
export function coerceParam(type: StrategyParam["type"], text: string | boolean): unknown {
  if (typeof text === "boolean") return text;
  const t = text.trim();
  if (t === "") return undefined;
  if (type === "int" && /^[+-]?\d+$/.test(t)) return Number(t);
  return t; // decimals stay strings: the engine converts them exactly
}

const PLAIN_DECIMAL = /^\d+(?:\.\d+)?$/;

/** A stored fraction as the percentage a person types: "0.95" -> "95", "0.075" -> "7.5". Exact (digits are moved, never multiplied);
 *  text that is not a plain decimal comes back unchanged. */
export function fractionToPercentText(fraction: string): string {
  const t = fraction.trim();
  if (!PLAIN_DECIMAL.test(t)) return fraction;
  return shiftPoint(t, 2);
}

/** What a person typed as a percentage ("95", "9.5", "95 %") as the stored fraction: "0.95", "0.095". Text that is not a plain
 *  number comes back unchanged, so the usual check names the problem. */
export function percentTextToFraction(text: string): string {
  const t = text.trim().replace(/\s*%$/, "");
  if (!PLAIN_DECIMAL.test(t)) return text;
  return shiftPoint(t, -2);
}

/** Moves the decimal point of a plain decimal string by `places` (positive: right) without any arithmetic. */
function shiftPoint(t: string, places: number): string {
  const [whole, frac = ""] = t.split(".");
  let digits = whole + frac;
  let point = whole.length + places;
  if (point < 0) { digits = "0".repeat(-point) + digits; point = 0; }
  if (point > digits.length) digits += "0".repeat(point - digits.length);
  let out = digits.slice(0, point).replace(/^0+(?=\d)/, "") || "0";
  const tail = digits.slice(point).replace(/0+$/, "");
  if (tail) out += "." + tail;
  return out;
}

/** The text a form field starts with for a parameter: its default as typed, a percentage for a `percent` entry. */
export function paramText(p: StrategyParam): string | boolean {
  if (p.type === "bool") return Boolean(p.default);
  if (p.default === null || p.default === undefined) return "";
  return p.percent ? fractionToPercentText(String(p.default)) : String(p.default);
}

/** Text typed into a parameter's field, as the JSON value stored (a percentage becomes its fraction). */
export function coerceParamText(p: StrategyParam, text: string | boolean): unknown {
  return coerceParam(p.type, typeof text === "string" && p.percent ? percentTextToFraction(text) : text);
}
