"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import JobStatus from "@/components/JobStatus";
import { Field, Panel } from "@/components/ui";
import { getResearchLimits, startFailure, startWalkforward, useJob } from "@/lib/jobs";
import { jobHref } from "@/lib/researchHelpers";
import { OBJECTIVE_CHOICES, walkforwardRequest } from "@/lib/sweepForm";
import type { ResearchLimitsDTO } from "@/lib/types";

/** "Walk forward" on a finished sweep: pick the best settings on a stretch of days, test them on the days right after,
 * slide forward and repeat. The sizes start from research.json (read from the server) and can be changed; the days
 * default to the sweep's own. It runs on the server; progress shows here. */
export default function WalkforwardPanel({ sweepId }: { sweepId: string }) {
  const [limits, setLimits] = useState<ResearchLimitsDTO | null>(null);
  const [failed, setFailed] = useState(false);
  const [f, setF] = useState({ train: "", test: "", step: "", objective: "sharpe", first: "", last: "" });
  const [problem, setProblem] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const { job, error, begin, clear, stop, running } = useJob("walkforward", sweepId);

  useEffect(() => {
    getResearchLimits().then(
      (l) => {
        setLimits(l);
        setF((x) => ({ ...x, train: String(l.train_days), test: String(l.test_days), step: String(l.step_days), objective: l.wf_objective }));
      },
      () => setFailed(true),
    );
  }, []);

  const req = walkforwardRequest(sweepId, f);
  const reason = limits === null ? (failed ? "The defaults could not be read. Reload the page." : "Loading…") : req.problem;

  async function run() {
    if (!req.body || reason) return;
    setStarting(true);
    setProblem(null);
    clear();
    try {
      begin(await startWalkforward(req.body as never));
    } catch (e) {
      setProblem(startFailure(e));
    } finally {
      setStarting(false);
    }
  }

  return (
    <section className="stk-section" aria-labelledby="wf-run-h">
      <h2 id="wf-run-h">Walk forward</h2>
      <Panel>
        <form className="stk-stack-s" noValidate onSubmit={(e) => { e.preventDefault(); void run(); }} aria-label="Walk forward this sweep">
          <p className="stk-fine">
            A walk-forward checks whether the best settings of this sweep would have held up on days they were not picked on. It picks the
            best settings on a stretch of training days, tests them on the days right after, then slides forward and repeats. All lengths
            are calendar days.
          </p>
          <div className="stk-form">
            <Field label="Training days" help="Days used to pick the best settings.">
              <input className="stk-field" inputMode="numeric" value={f.train} onChange={(e) => setF({ ...f, train: e.target.value })} />
            </Field>
            <Field label="Test days" help="Days right after, used to test the pick.">
              <input className="stk-field" inputMode="numeric" value={f.test} onChange={(e) => setF({ ...f, test: e.target.value })} />
            </Field>
            <Field label="Step days" help="How far each window slides. At least the test days.">
              <input className="stk-field" inputMode="numeric" value={f.step} onChange={(e) => setF({ ...f, step: e.target.value })} />
            </Field>
            <Field label="Pick by">
              <select className="stk-field" value={f.objective} onChange={(e) => setF({ ...f, objective: e.target.value })}>
                {OBJECTIVE_CHOICES.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </Field>
            <Field label="First day (optional)" help="Left empty: the sweep's first day, like 2026-01-01.">
              <input className="stk-field" value={f.first} placeholder="YYYY-MM-DD" onChange={(e) => setF({ ...f, first: e.target.value })} />
            </Field>
            <Field label="Last day (optional)" help="Left empty: the sweep's last day.">
              <input className="stk-field" value={f.last} placeholder="YYYY-MM-DD" onChange={(e) => setF({ ...f, last: e.target.value })} />
            </Field>
          </div>
          {job && <JobStatus job={job} error={error} onStop={stop} />}
          {job?.status === "done" && (
            <p>
              <Link className="stk-link" href={jobHref("walkforward", job.job_id) ?? "/research"}>Open the walk-forward report</Link>
            </p>
          )}
          {problem && <p role="alert" className="stk-warn">{problem}</p>}
          <div className="stk-row">
            <button type="submit" className="stk-btn" data-tone="primary" disabled={Boolean(reason) || starting || running} aria-describedby={reason && !running ? "wf-why" : undefined}>
              {starting ? "Starting…" : running ? "Walk-forward running…" : "Run walk-forward"}
            </button>
            {reason && !running && <span id="wf-why" className="stk-fine">{reason}</span>}
          </div>
        </form>
      </Panel>
    </section>
  );
}
