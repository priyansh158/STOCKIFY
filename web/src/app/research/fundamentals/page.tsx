/**
 * /research/fundamentals (FS 10): results coming up, then the stocks that look cheap and healthy against their
 * industry, best first. Ideas to research, not orders. The list's order is the judgement: no score, rank number or
 * account of how the order is made appears on the page (owner, 2026-11-17).
 */
import type { Metadata } from "next";
import Link from "next/link";
import { dayWords } from "@/lib/format";
import { getFundamentals } from "@/lib/fundamentals";
import { jobHref } from "@/lib/research";
import type { FundamentalsListDTO } from "@/lib/types";
import { Empty, PageHead, Panel } from "@/components/ui";
import CandidateList from "./CandidateList";
import UpcomingResults from "./UpcomingResults";

export const metadata: Metadata = { title: "Fundamentals - STOCKIFY" };
export const dynamic = "force-dynamic";

const LEDE = "Stocks that look cheap and healthy against their industry. Ideas to research, not orders.";

export default async function FundamentalsPage() {
  const r = await getFundamentals();

  return (
    <div className="stk-page">
      <Link href="/research" className="stk-crumb">
        Research
      </Link>
      <div className="stk-stack" data-ruled>
        <PageHead title="Fundamentals" lede={LEDE} />

        {r.kind !== "ok" ? (
          <Panel tone="red">
            <div role="alert" className="stk-stack-s">
              <p>
                Couldn&apos;t load the fundamentals:{" "}
                {r.kind === "error" ? r.message : r.reason}. Check that the STOCKIFY API is running, then try again.
              </p>
              <p>
                <Link className="stk-link" href="/research/fundamentals">
                  Try again
                </Link>
              </p>
            </div>
          </Panel>
        ) : (
          <Loaded list={r.value} />
        )}
      </div>
    </div>
  );
}

function Loaded({ list }: { list: FundamentalsListDTO }) {
  const record = list.test_record_id ? jobHref("walkforward", list.test_record_id) : null;
  const firstRun = list.as_of == null && list.items.length === 0;

  return (
    <>
      <UpcomingResults rows={list.upcoming} />

      <section className="stk-section" aria-labelledby="fund-list">
        <div className="stk-row-between stk-fund-h">
          <h2 id="fund-list">Worth a closer look</h2>
          {list.as_of && <span className="stk-label">As of the close on {dayWords(list.as_of)}</span>}
        </div>
        {firstRun ? (
          <Empty>
            The first download of company results hasn&apos;t run yet. Run{" "}
            <code className="stk-mono">python -m stockify fundamentals backfill</code> once: it reads every quarterly
            result filed with NSE since April 2021, about one request a second, so it takes a few hours. After that the
            daemon brings it up to date each evening after the close, and the list appears here.
          </Empty>
        ) : list.items.length === 0 ? (
          <Empty>
            No stock is on the list for {dayWords(list.as_of)}: none had enough recent results and a price to compare. The
            list is made again each evening after the close.
          </Empty>
        ) : (
          <CandidateList items={list.items} />
        )}
        {list.held_count > 0 && !firstRun && (
          <p className="stk-fine stk-gap-xs">
            {list.held_count.toLocaleString("en-IN")} more {list.held_count === 1 ? "stock is" : "stocks are"} covered
            but not on the list: no recent results that could be read, or no price yet.

          </p>
        )}
      </section>

      <footer className="stk-section stk-fund-fine" aria-label="About this list">
        <p className="stk-fine">
          {record ? (
            <Link href={record} className="stk-link">
              How this list has done in tests
            </Link>
          ) : (
            "How this list has done in tests will be linked here once the test has run."
          )}
        </p>
        {list.notes.length > 0 && <p className="stk-fine">{list.notes.join(" ")}</p>}
      </footer>
    </>
  );
}
