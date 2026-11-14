import type { WebJobDTO } from "@/lib/types";

/** One job's state: its sentence from the server, a progress bar while it runs, and a plain line when the progress
 * could not be read. Announced politely to a screen reader as it changes. */
export default function JobStatus({ job, error }: { job: WebJobDTO; error?: string | null }) {
  const known = job.total != null && job.done != null && job.total > 0;
  return (
    <div className="stk-job" data-status={job.status} role="status" aria-live="polite">
      <p>{job.plain}</p>
      {job.status === "running" && known && (
        <progress className="stk-meter" max={job.total ?? 1} value={job.done ?? 0} aria-label="How much has finished" />
      )}
      {error && <p className="stk-fine">{error}</p>}
    </div>
  );
}
