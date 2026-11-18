"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import JobStatus from "@/components/JobStatus";
import { startFailure, startFundamentalsBackfill, useJob } from "@/lib/jobs";

/** "Start the first download": the fundamentals backfill as a job on the server (every company's results filed with
 * NSE since April 2021, then the day's list). Its progress shows in words; the page refreshes when it ends. */
export default function StartDownload() {
  const router = useRouter();
  const [problem, setProblem] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const { job, error, begin, clear, stop, running } = useJob("fundamentals", "backfill", () => router.refresh());
  async function go() {
    setStarting(true);
    setProblem(null);
    clear();
    try {
      begin(await startFundamentalsBackfill());
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
          {starting ? "Starting…" : running ? "Downloading…" : job && job.status !== "done" ? "Start the download again" : "Start the first download"}
        </button>
        {running && (
          <button type="button" className="stk-btn" onClick={() => void stop()}>
            Stop
          </button>
        )}
      </div>
      <p className="stk-fine">
        Downloads every company&apos;s results since April 2021 from NSE. This takes a few hours; you can leave the page.
      </p>
      {job && <JobStatus job={job} error={error} />}
      {problem && <p role="alert" className="stk-warn">{problem}</p>}
    </div>
  );
}
