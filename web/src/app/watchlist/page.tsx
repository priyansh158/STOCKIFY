/**
 * /watchlist (CW 2): the stocks the owner follows. Live prices while the app is open and Kite is logged in, the last
 * close otherwise, and each stock's distance from its 52-week high and low. Nothing here places an order.
 */
import type { Metadata } from "next";
import { api } from "@/lib/api";
import type { WatchlistDTO } from "@/lib/types";
import { PageHead, Panel } from "@/components/ui";
import WatchlistView from "./WatchlistView";

export const metadata: Metadata = { title: "Watchlist - STOCKIFY" };
export const dynamic = "force-dynamic";

const LEDE = "Stocks you follow, with how far each is from its 52-week high and low.";

export default async function WatchlistPage() {
  const list = await api<WatchlistDTO>("/api/watchlist").catch(() => null);

  return (
    <div className="stk-page">
      <PageHead title="Watchlist" lede={LEDE} />
      {list === null ? (
        <Panel tone="red">
          <div role="alert" className="stk-stack-s">
            <p>Couldn&apos;t load the watchlist. Check that the STOCKIFY API is running, then reload the page.</p>
          </div>
        </Panel>
      ) : (
        <WatchlistView initial={list} />
      )}
    </div>
  );
}
