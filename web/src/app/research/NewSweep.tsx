"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import JobStatus from "@/components/JobStatus";
import { Field, Panel } from "@/components/ui";
import { getResearchLimits, startFailure, startSweep, useJob } from "@/lib/jobs";
import { wordsOf } from "@/lib/names";
import { jobHref } from "@/lib/researchHelpers";
import { fractionToPercentText } from "@/lib/params";
import { OBJECTIVE_CHOICES, boolChoice, parseValues, planSweep, triesWords } from "@/lib/sweepForm";
import { getTest } from "@/lib/tests";
import type { ResearchLimitsDTO, Strategy, TestRun, TestRunDetail } from "@/lib/types";

type Named = Strategy & { display_name?: string };
type BoolChoice = "keep" | "both" | "true" | "false";

function strategyName(s: Strategy | undefined, fallback: string): string {
  return s ? ((s as Named).display_name ?? s.name) : fallback;
}

/** "New parameter sweep": start from a finished test, say which settings to vary and how, see how many tries that
 * makes, and run it. The sweep runs on the server (a few minutes for a big one); progress shows here and survives a
 * reload of the page. */
export default function NewSweep({ runs, strategies }: { runs: TestRun[]; strategies: Strategy[] }) {
  const router = useRouter();
  const [limits, setLimits] = useState<ResearchLimitsDTO | null>(null);
  const [limitsError, setLimitsError] = useState(false);
  const [runId, setRunId] = useState("");
  const [detail, setDetail] = useState<TestRunDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [texts, setTexts] = useState<Record<string, string>>({});
  const [bools, setBools] = useState<Record<string, BoolChoice>>({});
  const [objective, setObjective] = useState("sharpe");
  const [name, setName] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const { job, error, begin, clear, stop, running } = useJob("sweep", null, () => router.refresh());

  useEffect(() => {
    getResearchLimits().then(setLimits, () => setLimitsError(true));
  }, []);

  function choose(id: string) {
    clear();
    setRunId(id);
    setDetail(null);
    setDetailError(null);
    setTexts({});
    setBools({});
    setConfirmed(false);
  }

  useEffect(() => {
    if (!runId) return;
    let stop = false;
    getTest(runId).then(
      (d) => !stop && setDetail(d),
      () => !stop && setDetailError("Could not read this test. Choose another, or reload the page."),
    );
    return () => {
      stop = true;
    };
  }, [runId]);

  const strategy = useMemo(() => {
    const s = detail?.config?.strategy as { kind?: string; ref?: string } | undefined;
    return strategies.find((x) => x.kind === s?.kind && x.ref === s?.ref);
  }, [detail, strategies]);
  const current = ((detail?.config?.strategy as { params?: Record<string, unknown> } | undefined)?.params ?? {}) as Record<string, unknown>;

  const entries = useMemo(
    () =>
      (strategy?.params ?? []).map((param) => {
        if (param.type === "bool") return { param, values: boolChoice(bools[param.name] ?? "keep"), problem: null };
        return { param, ...parseValues(param, texts[param.name] ?? "") };
      }),
    [strategy, texts, bools],
  );
  const plan = useMemo(
    () => planSweep(entries, limits ?? { max_trials: 1000, confirm_above: 50 }, Boolean(strategy), confirmed),
    [entries, limits, strategy, confirmed],
  );
  const reason = limits === null ? (limitsError ? "The limits could not be read. Reload the page." : "Loading…") : plan.reason;
  const bad = new Map(entries.filter((e) => e.problem).map((e) => [e.param.name, e.problem as string]));

  async function run() {
    if (reason) return;
    setStarting(true);
    setProblem(null);
    try {
      begin(
        await startSweep({
          base_run_id: runId,
          name: name.trim() || null,
          axes: plan.axes,
          objective: objective as never,
          confirm: plan.needsConfirm && confirmed,
        }),
      );
    } catch (e) {
      setProblem(startFailure(e));
    } finally {
      setStarting(false);
    }
  }

  const lead = runs.find((r) => r.run_id === runId);

  return (
    <Panel>
      <form className="stk-stack-s" aria-label="New parameter sweep" noValidate onSubmit={(e) => { e.preventDefault(); void run(); }}>
        <h3>New parameter sweep</h3>
        <p className="stk-fine">
          A sweep runs the same test again and again, changing the settings you choose, and shows which combination did best. Every
          try counts: the more you try, the more likely the best one is just luck, and the research pages allow for that.
        </p>

        {runs.length === 0 ? (
          <p>
            A sweep starts from a finished test. <Link className="stk-link" href="/tests">Run a test first</Link>.
          </p>
        ) : (
          <div className="stk-form">
            <Field label="Start from this test" help="Its strategy, days and data are kept; only the settings below change.">
              <select className="stk-field" value={runId} onChange={(e) => choose(e.target.value)}>
                <option value="">Choose a finished test…</option>
                {runs.map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {r.name?.trim() || r.run_id}
                    {r.start_date && r.end_date ? ` (${r.start_date} to ${r.end_date})` : ""}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Judge the tries by" help="The best try is the one that scores highest on this.">
              <select className="stk-field" value={objective} onChange={(e) => setObjective(e.target.value)}>
                {OBJECTIVE_CHOICES.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </Field>
            <Field label="Name (optional)" help="Letters, digits, dashes. Left empty, one is made from the test.">
              <input className="stk-field" value={name} maxLength={40} onChange={(e) => setName(e.target.value)} />
            </Field>
          </div>
        )}

        {detailError && <p role="alert" className="stk-warn">{detailError}</p>}
        {runId && !detail && !detailError && <p className="stk-muted">Reading the test…</p>}
        {detail && !strategy && (
          <p role="alert" className="stk-warn">
            This test used a strategy that is not one of the built-in ones, so its settings cannot be swept here.
          </p>
        )}

        {strategy && (
          <fieldset className="stk-stack-s" style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="stk-label">
              Settings of {strategyName(strategy, strategy.name)}{lead ? ` in ${lead.name?.trim() || lead.run_id}` : ""}. Leave a setting empty to keep it as it is.
            </legend>
            <div className="stk-sweep-grid">
              {strategy.params.map((p) => {
                const now = current[p.name];
                const nowChoice = p.choices?.find((c) => String(c.value) === String(now ?? p.default));
                const nowText = now === undefined || now === null ? "not set" : nowChoice ? nowChoice.label : p.percent ? `${now} (${fractionToPercentText(String(now))} %)` : String(now);
                const label = p.label || wordsOf(p.name);
                if (p.type === "bool") {
                  return (
                    <Field key={p.name} label={label} help={`Now: ${now === undefined ? (p.default ? "yes" : "no") : now ? "yes" : "no"}.`}>
                      <select className="stk-field" value={bools[p.name] ?? "keep"} onChange={(e) => setBools({ ...bools, [p.name]: e.target.value as BoolChoice })}>
                        <option value="keep">Keep as it is</option>
                        <option value="both">Try yes and no</option>
                        <option value="true">Always yes</option>
                        <option value="false">Always no</option>
                      </select>
                    </Field>
                  );
                }
                const hint = p.choices?.length
                  ? `Now: ${nowText}. Try several, separated by commas: ${p.choices.map((c) => `${String(c.value)} (${c.label})`).join(", ")}.`
                  : p.percent
                    ? `Now: ${nowText}. Type fractions such as 0.5, 0.75, 0.95 (0.95 is 95 %).`
                    : p.type === "int" || p.type === "decimal"
                      ? `Now: ${nowText}. Try values like 5, 10, 15 or a range like 5 to 30 by 5.`
                      : `Now: ${nowText}. Try values separated by commas.`;
                return (
                  <div key={p.name} className="stk-fieldwrap">
                    <Field label={label} help={bad.get(p.name) ? undefined : hint}>
                      <input
                        className="stk-field"
                        value={texts[p.name] ?? ""}
                        aria-invalid={bad.has(p.name)}
                        aria-describedby={bad.has(p.name) ? `err-${p.name}` : undefined}
                        placeholder="Keep as it is"
                        onChange={(e) => setTexts({ ...texts, [p.name]: e.target.value })}
                      />
                    </Field>
                    {bad.get(p.name) && <span id={`err-${p.name}`} role="alert" className="stk-warn stk-fine">{bad.get(p.name)}</span>}
                  </div>
                );
              })}
            </div>
          </fieldset>
        )}

        {strategy && (
          <p className="stk-tries" aria-live="polite">
            {plan.tries > 1 || Object.keys(plan.axes).length > 0 ? `That makes ${triesWords(plan.tries)}.` : "Nothing is changing yet: 1 try."}
          </p>
        )}
        {strategy && plan.needsConfirm && plan.tries <= (limits?.max_trials ?? Infinity) && (
          <label className="stk-checkrow">
            <input type="checkbox" className="stk-check" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
            <span>Yes, run all {plan.tries.toLocaleString("en-IN")} tries. It will take a while.</span>
          </label>
        )}

        {job && <JobStatus job={job} error={error} onStop={stop} />}
        {job?.status === "done" && (
          <p>
            <Link className="stk-link" href={jobHref("sweep", job.job_id) ?? "/research"}>Open the sweep</Link>
          </p>
        )}
        {problem && <p role="alert" className="stk-warn">{problem}</p>}

        <div className="stk-row">
          <button type="submit" className="stk-btn" data-tone="primary" disabled={Boolean(reason) || starting || running} aria-describedby={reason && !running ? "sweep-why" : undefined}>
            {starting ? "Starting…" : running ? "Sweep running…" : "Run sweep"}
          </button>
          {reason && !running && <span id="sweep-why" className="stk-fine">{reason}</span>}
        </div>
      </form>
    </Panel>
  );
}
