import { Loading, Skeleton } from "@/components/ui";

/** Shown while the watchlist page fetches: the title, then the table in the shape it comes in. */
export default function WatchlistLoading() {
  return (
    <div className="stk-page">
      <div className="stk-stack">
        <Skeleton size="title" />
        <Loading label="Loading the watchlist">
          <span className="sr-only">Loading the watchlist…</span>
          <Skeleton size="m" />
          <Skeleton size="l" />
        </Loading>
      </div>
    </div>
  );
}
