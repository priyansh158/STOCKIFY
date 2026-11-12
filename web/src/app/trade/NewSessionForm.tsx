"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { ApiError } from "@/lib/api";
import { parseRupeesToPaise } from "@/lib/format";
import { coerceParam, validateParams } from "@/lib/params";
import { createSession } from "@/lib/trade";
import type { Strategy, StrategyParam } from "@/lib/types";
import { Field, Panel } from "@/components/ui";

const inputClass = "stk-field";

function defaultText(p: StrategyParam): string | boolean {
  if (p.type === "bool") return Boolean(p.default);
  return p.default === null || p.default === undefined ? "" : String(p.default);
}

export default function NewSessionForm({ strategies }: { strategies: Strategy[] }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [ref, setRef] = useState("");
  const chosen = ref || (strategies[0] ? `${strategies[0].kind}|${strategies[0].ref}` : "");
  const strategy = useMemo(() => strategies.find((s) => `${s.kind}|${s.ref}` === chosen), [strategies, chosen]);
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [name, setName] = useState("");
  const [symbols, setSymbols] = useState("NSE:RELIANCE");
  const [capital, setCapital] = useState("10,00,000");
  const [allowUnverified, setAllowUnverified] = useState(true);
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const params = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const p of strategy?.params ?? []) {
      const v = coerceParam(p.type, values[p.name] ?? defaultText(p));
      if (v !== undefined && !(p.type === "bool" && v === p.default && !p.required)) out[p.name] = v;
    }
    return out;
  }, [strategy, values]);

  const bad = useMemo(() => new Set(problems.map((p) => p.split(": ")[0].replace(/^strategy\.params\./, ""))), [problems]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!strategy) return setProblems(["strategy: choose a strategy"]);
    const found = validateParams(strategy.params, params);
    const capitalPaise = parseRupeesToPaise(capital);
    if (capitalPaise === null || capitalPaise <= 0) found.push("capital: enter an amount in rupees, e.g. 10,00,000");
    const universe = symbols.split(",").map((s) => s.trim()).filter(Boolean);
    if (universe.length === 0) found.push("symbols: name at least one symbol, e.g. NSE:RELIANCE");
    if (found.length) return setProblems(found);
    const first = universe[0].split(":").pop()?.toLowerCase() ?? "session";
    const label = (name.trim() || `${strategy.name}_${first}`).replace(/[^a-z0-9_ -]+/gi, "_");
    setProblems([]);
    setBusy(true);
    try {
      const { session_id } = await createSession({
        schema: "stockify.session/1",
        name: label,
        strategy: { kind: strategy.kind, ref: strategy.ref, params },
        universe,
        capital_paise: capitalPaise,
        costs: { allow_unverified: allowUnverified },
      });
      router.push(`/trade/${encodeURIComponent(session_id)}`);
    } catch (err) {
      const body = err instanceof ApiError ? err.body : null;
      const detail = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : null;
      setProblems(
        Array.isArray(detail)
          ? detail.map((d) => (typeof d === "string" ? d : JSON.stringify(d)))
          : [String(err instanceof Error ? err.message : err)],
      );
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <div>
        <button type="button" onClick={() => setOpen(true)} className="stk-btn" data-tone="primary">
          New session
        </button>
      </div>
    );
  }

  return (
    <Panel>
      <form onSubmit={submit} className="stk-stack-s" noValidate aria-label="New paper session" aria-describedby={problems.length ? "new-session-problems" : undefined}>
        <div className="stk-row-between">
          <h2>New session</h2>
          <button type="button" onClick={() => setOpen(false)} className="stk-btn" data-tone="quiet">
            Close
          </button>
        </div>

        <div className="stk-form">
          <Field label="Strategy" help={strategy?.doc}>
            <select className={inputClass} value={chosen} onChange={(e) => { setRef(e.target.value); setValues({}); }}>
              {strategies.map((s) => (
                <option key={`${s.kind}|${s.ref}`} value={`${s.kind}|${s.ref}`}>
                  {s.name}{s.kind === "cpp" ? " (C++)" : ""}
                </option>
              ))}
            </select>
          </Field>

          {(strategy?.params ?? []).map((p) => {
            const v = values[p.name] ?? defaultText(p);
            const label = `${p.label || p.name.replace(/_/g, " ")}${p.required ? " *" : ""}`;
            if (p.type === "bool") {
              return (
                <label key={p.name} className="stk-checkrow">
                  <input type="checkbox" className="stk-check" checked={Boolean(v)} aria-invalid={bad.has(p.name)}
                    onChange={(e) => setValues({ ...values, [p.name]: e.target.checked })} />
                  <span>{label}<span className="stk-label stk-block">{p.help}</span></span>
                </label>
              );
            }
            return (
              <Field key={p.name} label={label} help={p.help}>
                <input className={inputClass} value={String(v)} aria-invalid={bad.has(p.name)}
                  inputMode={p.type === "int" || p.type === "decimal" ? "decimal" : "text"}
                  placeholder={p.type === "hhmm" ? "15:10" : p.type === "symbol" ? "NSE:RELIANCE" : ""}
                  onChange={(e) => setValues({ ...values, [p.name]: e.target.value })} />
              </Field>
            );
          })}

          <Field label="Name (optional)" help="A short name to tell sessions apart.">
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Symbols to follow" help="Comma-separated, e.g. NSE:RELIANCE, NSE:NIFTY 50">
            <input className={inputClass} value={symbols} aria-invalid={bad.has("symbols")} onChange={(e) => setSymbols(e.target.value)} />
          </Field>
          <Field label="Starting money (₹)" help="Pretend money. Nothing real is used.">
            <input className={inputClass} data-fig inputMode="decimal" value={capital} aria-invalid={bad.has("capital")} onChange={(e) => setCapital(e.target.value)} />
          </Field>
          <label className="stk-checkrow stk-form-wide">
            <input type="checkbox" className="stk-check" checked={allowUnverified} onChange={(e) => setAllowUnverified(e.target.checked)} />
            <span>Allow charge rates not yet checked against the broker (the result is labelled)</span>
          </label>
        </div>

        {problems.length > 0 && (
          <ul id="new-session-problems" role="alert" className="stk-problems">
            {problems.map((p) => <li key={p}>{p}</li>)}
          </ul>
        )}

        <div>
          <button type="submit" disabled={busy} className="stk-btn" data-tone="primary">
            {busy ? "Creating…" : "Create session"}
          </button>
        </div>
      </form>
    </Panel>
  );
}
