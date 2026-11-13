import { Loading, Skeleton } from "@/components/ui";

/** Shown while /data fetches: the strip, then the day grid, in their own shapes. */
export default function DataLoading() {
  return (
    <div className="stk-page">
      <div className="stk-stack">
        <Skeleton size="title" />
        <Loading label="Loading the data coverage">
          <span className="sr-only">Loading the data coverage…</span>
          <Skeleton size="s" />
          <Skeleton size="l" />
          <Skeleton size="l" />
        </Loading>
      </div>
    </div>
  );
}
