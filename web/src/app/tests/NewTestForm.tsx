"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { ApiError } from "@/lib/api";
import { parseRupeesToPaise } from "@/lib/format";
import { coerceParam, validateParams } from "@/lib/params";
import { createTest } from "@/lib/tests";
import type { Strategy, StrategyParam } from "@/lib/types";
import { Field, Panel } from "@/components/ui";

type Source = "day" | "minute" | "bhav" | "ticks";

const SOURCES: { value: Source; label: string; fill: string }[] = [
  { value: "day", label: "Daily bars (Kite history)", fill: "bar" },
  { value: "minute", label: "Minute bars (Kite history)", fill: "bar" },
  { value: "bhav", label: "NSE end-of-day files", fill: "eod_close" },
  { value: "ticks", label: "Recorded ticks", fill: "snapshot_quote" },
];

const FILLS: { value: string; label: string }[] = [
  { value: "bar", label: "From bars" },
  { value: "eod_close", label: "Next day's close (end-of-day files)" },
  { value: "snapshot_quote", label: "From quotes (ticks)" },
  { value: "snapshot_ltp", label: "From last price (ticks, approximate)" },
];

const inputClass = "stk-field";

function defaultText(p: StrategyParam): string | boolean {
  if (p.type === "bool") return Boolean(p.default);
  return p.default === null || p.default === undefined ? "" : String(p.default);
}

export default function NewTestForm({ strategies, startOpen = false }: { strategies: Strategy[]; startOpen?: boolean }) {
  const router = useRouter();
  const [open, setOpen] = useState(startOpen);
  const [ref, setRef] = useState(strategies[0] ? `${strategies[0].kind}|${strategies[0].ref}` : "");
  const strategy = useMemo(() => strategies.find((s) => `${s.kind}|${s.ref}` === ref), [strategies, ref]);
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [source, setSource] = useState<Source>("day");
  const [symbols, setSymbols] = useState("NSE:RELIANCE");
  const [chainUnderlying, setChainUnderlying] = useState("");
  const [chainStrikes, setChainStrikes] = useState("10");
  const [from, setFrom] = useState("2024-01-01");
  const [to, setTo] = useState("2026-09-30");
  const [fill, setFill] = useState("bar");
  const [basis, setBasis] = useState<"raw" | "adjusted">("adjusted");
  const [capital, setCapital] = useState("10,00,000");
  const [seed, setSeed] = useState("42");
  const [allowUnverified, setAllowUnverified] = useState(true);
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const bad = useMemo(() => new Set(problems.map((p) => p.split(": ")[0].replace(/^strategy\.params\./, ""))), [problems]);

  const params = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const p of strategy?.params ?? []) {
      const v = coerceParam(p.type, values[p.name] ?? defaultText(p));
      if (v !== undefined && !(p.type === "bool" && v === p.default && !p.required)) out[p.name] = v;
    }
    return out;
  }, [strategy, values]);

  function pickSource(s: Source) {
    setSource(s);
    setFill(SOURCES.find((x) => x.value === s)?.fill ?? "bar");
    if (s === "bhav" || s === "ticks") setBasis("raw");
  }

  function buildConfig(): { config: Record<string, unknown> | null; problems: string[] } {
    const found: string[] = [];
    if (!strategy) return { config: null, problems: ["choose a strategy"] };
    found.push(...validateParams(strategy.params, params));
    const capitalPaise = parseRupeesToPaise(capital);
    if (capitalPaise === null || capitalPaise <= 0) found.push("capital: enter an amount in rupees, e.g. 10,00,000");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(from) || !/^\d{4}-\d{2}-\d{2}$/.test(to) || from > to) {
      found.push("dates: the first day must be on or before the last");
    }
    if (!/^\d+$/.test(seed)) found.push("seed: a whole number, 0 or more");
    if (chainUnderlying.trim() && !/^\s*[1-9]\d{0,2}\s*$/.test(chainStrikes)) {
      found.push("strikes: a whole number of strikes each side, 1 to 999");
    }
    if ((fill === "eod_close") !== (source === "bhav")) found.push("fills: next-day-close fills go with NSE end-of-day files only");
    if (fill === "eod_close" && basis !== "raw") found.push("prices: end-of-day files need actual (raw) prices");
    const instruments: unknown[] = symbols.split(",").map((s) => s.trim()).filter(Boolean);
    if (chainUnderlying.trim()) {
      for (const expiry of ["nearest", "next"]) {
        instruments.push({ chain: { underlying: chainUnderlying.trim().toUpperCase(), expiry, strikes: Number(chainStrikes.trim()) } });
      }
    }
    if (instruments.length === 0 && source !== "ticks") found.push("instruments: name at least one symbol or option chain");
    if (found.length) return { config: null, problems: found };
    const spec: Record<string, unknown> = {
      dataset: source === "day" || source === "minute" ? "bars" : source === "bhav" ? "bhav_eod" : "ticks",
      from,
      to,
    };
    if (source === "day") spec.interval = "day";
    if (source === "minute") spec.interval = "minute";
    if (instruments.length) spec.instruments = instruments;
    const first = symbols.split(",")[0]?.trim().split(":").pop()?.toLowerCase() || chainUnderlying.toLowerCase() || "test";
    return {
      config: {
        schema: "stockify.run/1",
        name: `${strategy.name}_${first}`.replace(/[^a-z0-9_]+/gi, "_"),
        strategy: { kind: strategy.kind, ref: strategy.ref, params },
        data: { packs: [{ spec }] },
        price_basis: basis,
        capital_paise: capitalPaise,
        fill_model: { kind: fill },
        costs: { allow_unverified: allowUnverified },
        seed: Number(seed),
      },
      problems: [],
    };
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const { config, problems: found } = buildConfig();
    setProblems(found);
    if (!config) return;
    setBusy(true);
    try {
      const { run_id } = await createTest(config);
      router.push(`/tests/${encodeURIComponent(run_id)}`);
    } catch (err) {
      const body = err instanceof ApiError ? err.body : null;
      const detail = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : null;
      setProblems(Array.isArray(detail) ? detail.map((d) => (typeof d === "string" ? d : JSON.stringify(d))) : [String(err instanceof Error ? err.message : err)]);
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <div>
        <button type="button" onClick={() => setOpen(true)} className="stk-btn" data-tone="primary">
          New test
        </button>
      </div>
    );
  }

  return (
    <Panel>
      <form onSubmit={submit} className="stk-stack-s" noValidate aria-label="New test">
        <div className="stk-row-between">
          <h2>New test</h2>
          <button type="button" onClick={() => setOpen(false)} className="stk-btn" data-tone="quiet">
            Close
          </button>
        </div>

        <div className="stk-form">
          <Field label="Strategy" help={strategy?.doc}>
            <select className={inputClass} value={ref} onChange={(e) => { setRef(e.target.value); setValues({}); }}>
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
        </div>

        <div className="stk-form">
          <Field label="Market data">
            <select className={inputClass} value={source} onChange={(e) => pickSource(e.target.value as Source)}>
              {SOURCES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
            </select>
          </Field>
          <Field label="Symbols" help="comma-separated, e.g. NSE:RELIANCE, NSE:NIFTY 50">
            <input className={inputClass} value={symbols} aria-invalid={bad.has("instruments")} onChange={(e) => setSymbols(e.target.value)} />
          </Field>
          <Field label="How orders fill">
            <select className={inputClass} value={fill} aria-invalid={bad.has("fills")} onChange={(e) => setFill(e.target.value)}>
              {FILLS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
            </select>
          </Field>
          <Field label="Option chain of" help="e.g. NIFTY (optional)">
            <input className={inputClass} value={chainUnderlying} onChange={(e) => setChainUnderlying(e.target.value)} />
          </Field>
          <Field label="Strikes each side">
            <input className={inputClass} inputMode="numeric" value={chainStrikes} aria-invalid={bad.has("strikes")} onChange={(e) => setChainStrikes(e.target.value)} />
          </Field>
          <Field label="Prices" help="adjusted: corrected for splits and bonuses; actual: as traded">
            <select className={inputClass} value={basis} aria-invalid={bad.has("prices")} onChange={(e) => setBasis(e.target.value as "raw" | "adjusted")}>
              <option value="adjusted">Adjusted</option>
              <option value="raw">Actual (raw)</option>
            </select>
          </Field>
          <Field label="First day">
            <input type="date" className={inputClass} value={from} aria-invalid={bad.has("dates")} onChange={(e) => setFrom(e.target.value)} />
          </Field>
          <Field label="Last day">
            <input type="date" className={inputClass} value={to} aria-invalid={bad.has("dates")} onChange={(e) => setTo(e.target.value)} />
          </Field>
          <Field label="Starting money (₹)">
            <input className={inputClass} data-fig inputMode="decimal" value={capital} aria-invalid={bad.has("capital")} onChange={(e) => setCapital(e.target.value)} />
          </Field>
          <Field label="Seed" help="same seed, same result">
            <input className={inputClass} data-fig inputMode="numeric" value={seed} aria-invalid={bad.has("seed")} onChange={(e) => setSeed(e.target.value)} />
          </Field>
          <label className="stk-checkrow stk-form-wide">
            <input type="checkbox" className="stk-check" checked={allowUnverified} onChange={(e) => setAllowUnverified(e.target.checked)} />
            <span>Allow charge rates not yet verified (the result is labelled)</span>
          </label>
        </div>

        {problems.length > 0 && (
          <ul role="alert" className="stk-problems">
            {problems.map((p) => <li key={p}>{p}</li>)}
          </ul>
        )}

        <div>
          <button type="submit" disabled={busy} className="stk-btn" data-tone="primary">
            {busy ? "Starting…" : "Start test"}
          </button>
        </div>
      </form>
    </Panel>
  );
}
