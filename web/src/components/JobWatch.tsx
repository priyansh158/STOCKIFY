"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import JobStatus from "@/components/JobStatus";
import { getJob } from "@/lib/jobs";
import type { WebJobDTO } from "@/lib/types";

/** A research job that is still running, followed live: its progress sentence and bar, and the page reloads itself
 * when the job ends. Used where a page would otherwise say "reload in a while". */
export default function JobWatch({ jobId }: { jobId: string }) {
  const router = useRouter();
  const [job, setJob] = useState<WebJobDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const next = await getJob(jobId);
        if (stop) return;
        setJob(next);
        setError(null);
        if (next.status !== "running") router.refresh();
      } catch {
        if (!stop) setError("Could not read the progress just now; trying again.");
      }
    };
    void tick();
    const timer = setInterval(tick, 2000);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, [jobId, router]);
  return job ? <JobStatus job={job} error={error} /> : <p className="stk-muted" role="status">{error ?? "Reading the progress…"}</p>;
}
