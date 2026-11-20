/**
 * /research/fundamentals/[symbol] (FS 10): one stock against its industry. The plain reasons, its figures beside its
 * peers' median, one chart per figure by quarter, the filings each figure came from and who it is compared with.
 * A stock the screener covers but has left out shows its figures and why it is left out. No score, rank number or
 * account of how the list is ordered appears here (owner, 2026-11-17). Ideas to research, not orders.
 */
import type { Metadata } from "next";
import Link from "next/link";
import { dayWords, rupees } from "@/lib/format";
import {
  basisWords, broadcastWords, figureName, figureText, filingState, getFundamentalsDetail, medianLabel, MEANINGS,
  peerWords, periodKindWords, sentence,
} from "@/lib/fundamentals";
import type { CandidateDetailDTO } from "@/lib/types";
import StockChart from "@/components/StockChart";
import { Empty, Panel, Table } from "@/components/ui";
import FigureCharts from "./FigureCharts";

export const metadata: Metadata = { title: "Fundamentals - STOCKIFY" };
export const dynamic = "force-dynamic";

const LEDE = "Ideas to research, not orders.";

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

export default async function FundamentalsDetailPage({ params }: { params: Promise<{ symbol: string }> }) {
  const { symbol: raw } = await params;
  const symbol = safeDecode(raw).toUpperCase();
  const r = await getFundamentalsDetail(symbol);

  return (
    <div className="stk-page">
      <Link href="/research/fundamentals" className="stk-crumb">
        Fundamentals
      </Link>
      {r.kind === "ok" ? (
        <Detail d={r.value} />
      ) : (
        <div className="stk-stack">
          <div className="stk-head">
            <div>
              <h1>{symbol}</h1>
              <p className="stk-lede">{LEDE}</p>
            </div>
          </div>
          {r.kind === "missing" ? (
            <Empty>
              {symbol} is {r.reason.replace(/\.$/, "")}.{" "}
              <Link href="/research/fundamentals" className="stk-link">
                Back to the list
              </Link>
            </Empty>
          ) : (
            <Panel tone="red">
              <div role="alert" className="stk-stack-s">
                <p>
                  Couldn&apos;t load the figures for {symbol}: {r.message}. Check that the STOCKIFY API is running, then
                  try again.
                </p>
                <p>
                  <Link className="stk-link" href={`/research/fundamentals/${encodeURIComponent(symbol)}`}>
                    Try again
                  </Link>
                </p>
              </div>
            </Panel>
          )}
        </div>
      )}
    </div>
  );
}

function Detail({ d }: { d: CandidateDetailDTO }) {
  const left = d.left_out_reason != null;
  const median = medianLabel(d.peer_group);
  const peers = peerWords(d.peer_group);
  const hasMedians = d.ratios.some((x) => x.sector_median != null);
  // A left-out stock's one reason is left_out_reason. The API writes every reason in the page's figure style.
  const why = left ? [d.left_out_reason as string] : d.reasons;
  return (
    <div className="stk-stack" data-ruled>
      <div className="stk-head">
        <div>
          <p className="stk-label">
            {d.sector ?? "Industry not given"}
            {d.as_of && <> · As of the close on {dayWords(d.as_of)}</>}
          </p>
          <h1 className="stk-gap-s">{d.symbol}</h1>
          <p className="stk-lede">
            {d.company.replace(/\.$/, "")}. {LEDE}
          </p>
        </div>
      </div>

      <PriceSection d={d} />

      <section className="stk-section" aria-labelledby="fund-why">
        <h2 id="fund-why">{left ? "Why it is not on the list" : d.listed ? "Why it is on the list" : "What the figures show"}</h2>
        {why.length === 0 ? (
          <Empty>No reasons were given for this stock.</Empty>
        ) : (
          <ul className="stk-bullets">
            {why.map((t) => (
              <li key={t}>{sentence(t)}</li>
            ))}
          </ul>
        )}
        {peers && <p className="stk-fine stk-gap-top">{peers}</p>}
      </section>

      <section className="stk-section" aria-labelledby="fund-figures">
        <h2 id="fund-figures">The figures</h2>
        {!d.ratios.some((x) => x.value != null) ? (
          <Empty>No figures could be worked out for {d.symbol} yet: it needs results that could be read and a price.</Empty>
        ) : (
          <Table
            caption={hasMedians ? `${d.symbol}'s figures against the ${median.toLowerCase()}` : `${d.symbol}'s figures`}
            head={[
              { label: "Figure" }, { label: d.symbol, num: true }, ...(hasMedians ? [{ label: median, num: true }] : []),
              { label: "What it means" },
            ]}
          >
            {d.ratios.map((x) => (
              <tr key={x.key}>
                <th scope="row" className="stk-fund-top">{figureName(x.key, x.label)}</th>
                <td className="num">{figureText(x.unit, x.value)}</td>
                {hasMedians && <td className="num">{figureText(x.unit, x.sector_median)}</td>}
                <td data-wrap className="stk-fund-why stk-muted">{MEANINGS[x.key] ?? ""}</td>
              </tr>
            ))}
          </Table>
        )}
      </section>

      <section className="stk-section" aria-labelledby="fund-charts">
        <h2 id="fund-charts">Quarter by quarter</h2>
        {d.series.length === 0 ? (
          d.ratios.some((x) => x.value != null) ? (
            <Empty>Charts are drawn for stocks on the list.</Empty>
          ) : (
            <Empty>No quarter&apos;s figures have been read for this stock yet, so there is nothing to draw.</Empty>
          )
        ) : (
          <>
            <p className="stk-fine stk-gap-bottom">
              Each point is a quarter, marked by the month it ended; the {median.toLowerCase()} is for the same quarter.
            </p>
            <FigureCharts symbol={d.symbol} series={d.series} medianName={median} />
          </>
        )}
      </section>

      <Filings d={d} />
    </div>
  );
}

/** The price in facts: the last close and the 52-week range, then the year's candles. No score, rank or method words. */
function PriceSection({ d }: { d: CandidateDetailDTO }) {
  const p = d.price;
  const facts: { label: string; value: string; day?: string | null }[] = [];
  if (p?.last_close_paise != null) facts.push({ label: "Last close", value: rupees(p.last_close_paise), day: p.last_close_date });
  if (p?.high_paise != null) facts.push({ label: "52-week high", value: rupees(p.high_paise), day: p.high_date });
  if (p?.low_paise != null) facts.push({ label: "52-week low", value: rupees(p.low_paise), day: p.low_date });
  return (
    <section className="stk-section stk-stack-s" aria-labelledby="fund-price">
      <h2 id="fund-price">Price</h2>
      {facts.length > 0 && (
        <dl className="stk-row-between" style={{ justifyContent: "flex-start", gap: "var(--space-lg)" }}>
          {facts.map((f) => (
            <div key={f.label}>
              <dt className="stk-label">{f.label}</dt>
              <dd className="stk-mono">{f.value}</dd>
              {f.day && <dd className="stk-label">{dayWords(f.day)}</dd>}
            </div>
          ))}
        </dl>
      )}
      <StockChart symbol={d.symbol} />
    </section>
  );
}

function Filings({ d }: { d: CandidateDetailDTO }) {
  return (
    <section className="stk-section" aria-labelledby="fund-filings">
      <h2 id="fund-filings">The filings</h2>
      {d.filings.length === 0 ? (
        <Empty>No results filed with NSE have been found for {d.symbol} yet.</Empty>
      ) : (
        <>
          <p className="stk-fine stk-gap-bottom">Every result {d.symbol} filed with NSE since April 2021, newest first.</p>
          <Table
            caption={`Results ${d.symbol} filed with NSE, newest first`}
            head={[
              { label: "Period ended" }, { label: "Period" }, { label: "Accounts" }, { label: "Published by NSE" },
              { label: "State" }, { label: "Filing" },
            ]}
            tall={d.filings.length > 16}
          >
            {d.filings.map((f, i) => {
              const s = filingState(f);
              return (
                <tr key={`${f.period_end}-${f.period_kind}-${f.basis}-${f.broadcast_ist ?? ""}-${i}`}>
                  <th scope="row" className="stk-nowrap stk-fund-top">{dayWords(f.period_end)}</th>
                  <td>{periodKindWords(f.period_kind)}</td>
                  <td>{basisWords(f.basis)}</td>
                  <td className="stk-nowrap">{broadcastWords(f.broadcast_ist)}</td>
                  <td data-wrap>
                    <span className={s.cls}>{s.text}</span>
                    {f.status === "held" && f.reason && <div className="stk-label">{sentence(f.reason)}</div>}
                  </td>
                  <td>
                    {f.xbrl_url ? (
                      <a href={f.xbrl_url} className="stk-link" target="_blank" rel="noopener noreferrer">
                        XBRL file on NSE
                      </a>
                    ) : (
                      <span className="stk-muted">No link</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </Table>
        </>
      )}
    </section>
  );
}
