"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { ApiError } from "@/lib/api";
import { parseRupeesToPaise } from "@/lib/format";
import { coerceParam, validateParams } from "@/lib/params";
import { createTest } from "@/lib/tests";
import type { Strategy, StrategyParam } from "@/lib/types";
import { buttonStyle, Card } from "./ui";

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

const inputClass = "min-h-11 w-full rounded-md border bg-transparent px-3 py-2 text-sm";
const inputStyle = { borderColor: "rgba(0,255,65,0.25)", color: "var(--color-fg)", background: "var(--color-bg-soft)" };

function Field({ label, help, children }: { label: string; help?: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-sm" style={{ color: "var(--color-fg)" }}>{label}</span>
      {children}
      {help && <span className="text-xs" style={{ color: "var(--color-fg-muted)" }}>{help}</span>}
    </label>
  );
}

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
    if ((fill === "eod_close") !== (source === "bhav")) found.push("fills: next-day-close fills go with NSE end-of-day files only");
    if (fill === "eod_close" && basis !== "raw") found.push("prices: end-of-day files need actual (raw) prices");
    const instruments: unknown[] = symbols.split(",").map((s) => s.trim()).filter(Boolean);
    if (chainUnderlying.trim()) {
      for (const expiry of ["nearest", "next"]) {
        instruments.push({ chain: { underlying: chainUnderlying.trim().toUpperCase(), expiry, strikes: Number(chainStrikes) || 10 } });
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
      <button type="button" onClick={() => setOpen(true)} className="stk-mono-tight min-h-11 w-full rounded-md border px-4 py-2" style={buttonStyle}>
        + New test
      </button>
    );
  }

  return (
    <Card>
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <div className="flex items-center justify-between">
          <h2 className="stk-mono-tight" style={{ color: "var(--color-green-soft)" }}>New test</h2>
          <button type="button" onClick={() => setOpen(false)} className="min-h-11 px-3 text-sm" style={{ color: "var(--color-fg-muted)" }}>
            Close
          </button>
        </div>

        <Field label="Strategy" help={strategy?.doc}>
          <select className={inputClass} style={inputStyle} value={ref} onChange={(e) => { setRef(e.target.value); setValues({}); }}>
            {strategies.map((s) => (
              <option key={`${s.kind}|${s.ref}`} value={`${s.kind}|${s.ref}`}>
                {s.name}{s.kind === "cpp" ? " (C++)" : ""}
              </option>
            ))}
          </select>
        </Field>

        {(strategy?.params ?? []).map((p) => {
          const v = values[p.name] ?? defaultText(p);
          const label = `${p.name.replace(/_/g, " ")}${p.required ? " *" : ""}`;
          if (p.type === "bool") {
            return (
              <label key={p.name} className="flex min-h-11 items-center gap-3">
                <input type="checkbox" className="h-5 w-5" checked={Boolean(v)}
                  onChange={(e) => setValues({ ...values, [p.name]: e.target.checked })} />
                <span className="text-sm">{label}<span className="block text-xs" style={{ color: "var(--color-fg-muted)" }}>{p.help}</span></span>
              </label>
            );
          }
          return (
            <Field key={p.name} label={label} help={p.help}>
              <input className={inputClass} style={inputStyle} value={String(v)}
                inputMode={p.type === "int" || p.type === "decimal" ? "decimal" : "text"}
                placeholder={p.type === "hhmm" ? "15:10" : p.type === "symbol" ? "NSE:RELIANCE" : ""}
                onChange={(e) => setValues({ ...values, [p.name]: e.target.value })} />
            </Field>
          );
        })}

        <Field label="Market data">
          <select className={inputClass} style={inputStyle} value={source} onChange={(e) => pickSource(e.target.value as Source)}>
            {SOURCES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </Field>
        <Field label="Symbols" help="comma-separated, e.g. NSE:RELIANCE, NSE:NIFTY 50">
          <input className={inputClass} style={inputStyle} value={symbols} onChange={(e) => setSymbols(e.target.value)} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Option chain of" help="e.g. NIFTY (optional)">
            <input className={inputClass} style={inputStyle} value={chainUnderlying} onChange={(e) => setChainUnderlying(e.target.value)} />
          </Field>
          <Field label="Strikes each side">
            <input className={inputClass} style={inputStyle} inputMode="numeric" value={chainStrikes} onChange={(e) => setChainStrikes(e.target.value)} />
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="First day">
            <input type="date" className={inputClass} style={inputStyle} value={from} onChange={(e) => setFrom(e.target.value)} />
          </Field>
          <Field label="Last day">
            <input type="date" className={inputClass} style={inputStyle} value={to} onChange={(e) => setTo(e.target.value)} />
          </Field>
        </div>
        <Field label="How orders fill">
          <select className={inputClass} style={inputStyle} value={fill} onChange={(e) => setFill(e.target.value)}>
            {FILLS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </Field>
        <Field label="Prices" help="adjusted: corrected for splits and bonuses; actual: as traded">
          <select className={inputClass} style={inputStyle} value={basis} onChange={(e) => setBasis(e.target.value as "raw" | "adjusted")}>
            <option value="adjusted">Adjusted</option>
            <option value="raw">Actual (raw)</option>
          </select>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Starting money (₹)">
            <input className={inputClass} style={inputStyle} inputMode="decimal" value={capital} onChange={(e) => setCapital(e.target.value)} />
          </Field>
          <Field label="Seed" help="same seed, same result">
            <input className={inputClass} style={inputStyle} inputMode="numeric" value={seed} onChange={(e) => setSeed(e.target.value)} />
          </Field>
        </div>
        <label className="flex min-h-11 items-center gap-3">
          <input type="checkbox" className="h-5 w-5" checked={allowUnverified} onChange={(e) => setAllowUnverified(e.target.checked)} />
          <span className="text-sm">Allow charge rates not yet verified (the result is labelled)</span>
        </label>

        {problems.length > 0 && (
          <ul className="rounded-md border px-3 py-2 text-sm" style={{ borderColor: "var(--color-red)", color: "var(--color-red-soft)" }}>
            {problems.map((p) => <li key={p}>{p}</li>)}
          </ul>
        )}

        <button type="submit" disabled={busy} className="stk-mono-tight min-h-11 rounded-md border px-4 py-2" style={buttonStyle}>
          {busy ? "Starting…" : "Start test"}
        </button>
      </form>
    </Card>
  );
}
