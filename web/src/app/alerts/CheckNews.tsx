"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import JobStatus from "@/components/JobStatus";
import { checkBlockedReason } from "@/lib/monitor";
import { startFailure, startNewsCheck, useJob } from "@/lib/jobs";

/** "Check the news now": runs one monitor cycle on the server (what the background monitor does on a timer,
 * so it may store and email alerts), shows its progress and result in words, and refreshes the list when it ends. */
export default function CheckNews({ checkingElsewhere }: { checkingElsewhere: boolean }) {
  const router = useRouter();
  const [problem, setProblem] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const { job, error, begin, clear, running } = useJob("geo", "news", () => router.refresh());
  const blocked = checkBlockedReason(running, checkingElsewhere);
  async function go() {
    setStarting(true);
    setProblem(null);
    clear();
    try {
      begin(await startNewsCheck());
    } catch (e) {
      setProblem(startFailure(e));
    } finally {
      setStarting(false);
    }
  }
  return (
    <div className="stk-stack-s">
      <div className="stk-row">
        <button type="button" className="stk-btn" data-tone="primary" disabled={starting || running || blocked !== null} onClick={() => void go()}>
          {starting ? "Starting…" : running ? "Checking the news…" : job && job.status !== "done" ? "Check the news again" : "Check the news now"}
        </button>
        {blocked ? (
          <span className="stk-fine">{blocked}</span>
        ) : (
          !job && !problem && (
            <span className="stk-fine">Reads Google News once, the same check the background monitor makes. New High and Critical alerts are emailed.</span>
          )
        )}
      </div>
      {job && <JobStatus job={job} error={error} />}
      {problem && <p role="alert" className="stk-warn">{problem}</p>}
    </div>
  );
}
