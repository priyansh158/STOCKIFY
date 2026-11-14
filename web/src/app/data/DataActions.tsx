"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import JobStatus from "@/components/JobStatus";
import { startBackfill, startFailure, startQualityReport, useJob } from "@/lib/jobs";

/** What the user sees while either button runs, ends, or cannot start: the job's sentence, a bar, a plain error. */
function Action({
  label, busyLabel, hint, run, kind, target,
}: {
  label: string;
  busyLabel: string;
  hint: string;
  run: () => ReturnType<typeof startBackfill>;
  kind: "backfill" | "report";
  target: string;
}) {
  const router = useRouter();
  const [problem, setProblem] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const { job, error, begin, clear, stop, running } = useJob(kind, target, (j) => j.status === "done" && router.refresh());
  async function go() {
    setStarting(true);
    setProblem(null);
    clear();
    try {
      begin(await run());
    } catch (e) {
      setProblem(startFailure(e));
    } finally {
      setStarting(false);
    }
  }
  return (
    <div className="stk-stack-s">
      <div className="stk-row">
        <button type="button" className="stk-btn" data-tone="primary" disabled={starting || running} onClick={() => void go()}>
          {starting ? "Starting…" : running ? busyLabel : job && job.status !== "done" ? `${label} again` : label}
        </button>
        {!job && !problem && <span className="stk-fine">{hint}</span>}
      </div>
      {job && <JobStatus job={job} error={error} onStop={stop} />}
      {problem && <p role="alert" className="stk-warn">{problem}</p>}
    </div>
  );
}

/** Download the NSE files of the trading days from `from` to `to` (days already fetched are skipped) on the server. */
export function DownloadDays({ from, to, label = "Download missing days" }: { from: string; to: string; label?: string }) {
  return (
    <Action
      label={label}
      busyLabel="Downloading…"
      hint={from === to ? "Fetches this day's files from NSE." : "Fetches the files from NSE for the trading days in this range; days you already have are skipped."}
      kind="backfill"
      target={`${from}..${to}`}
      run={() => startBackfill({ first_day: from, last_day: to })}
    />
  );
}

/** Make one day's data-quality report from its recording. */
export function QualityReportButton({ day }: { day: string }) {
  return (
    <Action
      label="Make quality report"
      busyLabel="Making the report…"
      hint="Checks the day's recording for gaps and thin instruments."
      kind="report"
      target={day}
      run={() => startQualityReport({ day })}
    />
  );
}
