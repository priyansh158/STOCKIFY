import { Loading, Skeleton } from "@/components/ui";

/** Shown while /research fetches: the families table, then the job lists. */
export default function ResearchLoading() {
  return (
    <div className="stk-page">
      <div className="stk-stack">
        <Skeleton size="title" />
        <Loading label="Loading the research overview">
          <span className="sr-only">Loading the research overview…</span>
          <Skeleton size="l" />
          <Skeleton size="m" />
          <Skeleton size="m" />
        </Loading>
      </div>
    </div>
  );
}
