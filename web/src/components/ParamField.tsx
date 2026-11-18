"use client";

import { wordsOf } from "@/lib/names";
import type { StrategyParam } from "@/lib/types";
import { Field } from "@/components/ui";

const inputClass = "stk-field";

/** One strategy setting in a form: a tick box, a list of plain choices, a typed value, or (a `ranking`) a read-only note, by what the schema says.
 *  `value` is the text shown (a percentage for a `percent` setting); the parent turns it into the stored value (`coerceParamText`). */
export default function ParamField({ param: p, value, invalid, onChange }: {
  param: StrategyParam;
  value: string | boolean;
  invalid: boolean;
  onChange: (next: string | boolean) => void;
}) {
  const label = `${p.label || wordsOf(p.name)}${p.required ? " *" : ""}`;
  if (p.type === "bool") {
    return (
      <label className="stk-checkrow">
        <input type="checkbox" className="stk-check" checked={Boolean(value)} aria-invalid={invalid}
          onChange={(e) => onChange(e.target.checked)} />
        <span>{label}<span className="stk-label stk-block">{p.help}</span></span>
      </label>
    );
  }
  if (p.type === "ranking") {
    // Set by the fundamentals screener (each month's list); shown, never edited.
    return (
      <Field label={p.label || wordsOf(p.name)} help={p.help}>
        <input className={inputClass} value="Set by the screener" readOnly aria-readonly="true" />
      </Field>
    );
  }
  if (p.choices?.length) {
    return (
      <Field label={label} help={p.help}>
        <select className={inputClass} value={String(value)} aria-invalid={invalid} onChange={(e) => onChange(e.target.value)}>
          {p.choices.map((c) => <option key={String(c.value)} value={String(c.value)}>{c.label}</option>)}
        </select>
      </Field>
    );
  }
  return (
    <Field label={label} help={p.help}>
      <input className={inputClass} value={String(value)} aria-invalid={invalid}
        inputMode={p.type === "int" || p.type === "decimal" ? "decimal" : "text"}
        placeholder={p.type === "hhmm" ? "15:10" : p.type === "symbol" ? "NSE:RELIANCE" : ""}
        onChange={(e) => onChange(e.target.value)} />
    </Field>
  );
}
