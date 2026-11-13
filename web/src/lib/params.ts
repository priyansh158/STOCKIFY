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
    const why = typeProblem(p.type, v);
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
