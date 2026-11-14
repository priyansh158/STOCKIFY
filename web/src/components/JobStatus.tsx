import { useState } from "react";
import type { WebJobDTO } from "@/lib/types";

/** One job's state: its sentence from the server, a progress bar while it runs, and a plain line when the progress
 * could not be read. Announced politely to a screen reader as it changes. */
export default function JobStatus({ job, error, onStop }: { job: WebJobDTO; error?: string | null; onStop?: () => void }) {
  const [asking, setAsking] = useState(false);
  const canStop = Boolean(onStop) && job.status === "running" && !job.stopping && job.kind !== "report";
  const known = job.total != null && job.done != null && job.total > 0;
  return (
    <div className="stk-job" data-status={job.status} role="status" aria-live="polite">
      <p>{job.plain}</p>
      {job.status === "running" && known && (
        <progress className="stk-meter" max={job.total ?? 1} value={job.done ?? 0} aria-label="How much has finished" />
      )}
      {error && <p className="stk-fine">{error}</p>}
      {canStop && !asking && (
        <div>
          <button type="button" className="stk-btn" data-tone="quiet" onClick={() => setAsking(true)}>
            Stop
          </button>
        </div>
      )}
      {canStop && asking && (
        <div className="stk-row" role="group" aria-label="Confirm stopping">
          <span className="stk-fine">Stop it now? The tries that have finished are kept.</span>
          <button type="button" className="stk-btn" data-tone="primary" onClick={() => { setAsking(false); onStop?.(); }}>
            Yes, stop
          </button>
          <button type="button" className="stk-btn" data-tone="quiet" onClick={() => setAsking(false)}>
            Keep running
          </button>
        </div>
      )}
    </div>
  );
}
