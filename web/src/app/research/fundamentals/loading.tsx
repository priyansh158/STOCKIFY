import { Loading, Skeleton } from "@/components/ui";

/** Shown while a fundamentals page fetches: the title, then the tables in the shape they come in. */
export default function FundamentalsLoading() {
  return (
    <div className="stk-page">
      <div className="stk-stack">
        <Skeleton size="title" />
        <Loading label="Loading the fundamentals">
          <span className="sr-only">Loading the fundamentals…</span>
          <Skeleton size="m" />
          <Skeleton size="l" />
          <Skeleton size="l" />
        </Loading>
      </div>
    </div>
  );
}
