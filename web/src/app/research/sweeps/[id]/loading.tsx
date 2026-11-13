import { Loading, Skeleton } from "@/components/ui";

/** Shown while the sweep loads: the heading, the axis pickers and the grid. */
export default function SweepLoading() {
  return (
    <div className="stk-page">
      <div className="stk-stack">
        <Skeleton size="title" />
        <Loading label="Loading the sweep">
          <span className="sr-only">Loading the sweep…</span>
          <Skeleton size="s" />
          <Skeleton size="l" />
          <Skeleton size="m" />
        </Loading>
      </div>
    </div>
  );
}
