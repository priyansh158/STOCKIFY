import { Loading, Skeleton } from "@/components/ui";

/** Shown while /research/family fetches: the tries table, then the rest. */
export default function FamilyLoading() {
  return (
    <div className="stk-page">
      <div className="stk-stack">
        <Skeleton size="title" />
        <Loading label="Loading the trial family">
          <span className="sr-only">Loading the trial family…</span>
          <Skeleton size="l" />
          <Skeleton size="m" />
        </Loading>
      </div>
    </div>
  );
}
