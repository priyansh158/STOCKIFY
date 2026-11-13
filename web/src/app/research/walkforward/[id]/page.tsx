/**
 * /research/walkforward/[id]: the walk-forward report page, the same content as the command's report.md. Every
 * sentence about the statistics (the ratio, the deflated Sharpe, stability, holdout, overfitting) is the server's
 * `plain` text; this file adds the numbers beside it, the stitched curve and the per-window table. A job that is not
 * finished gets a plain state, not an error page.
 */
import Link from "next/link";
import type { ReactNode } from "react";
import { Empty, PageHead, Panel, Stat, StatGrid, Table } from "@/components/ui";
import { rupees } from "@/lib/format";
import { dayNumber, getWalkforward, sentenceOrDash, wfCurve, type WalkforwardLoad } from "@/lib/research";
import Curve from "./Curve";
import type { WalkforwardDTO, WalkforwardWindowDTO } from "@/lib/types";

const DAY = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

function dayText(n: number): string {
  return DAY.format(new Date(n * 86_400_000));
}

function isoText(iso: string): string {
  return dayText(dayNumber(iso));
}

function sharpe(n: unknown): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toFixed(2) : "—";
}

/** A headline figure that can be withheld by the server (too few out-of-sample days): words, never a bare dash; the
 * server's sentence about why sits right above it. */
function figure(n: unknown): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toFixed(2) : "Not available";
}

function signedRupees(paise: unknown): string {
  if (typeof paise !== "number") return "—";
  return `${paise > 0 ? "+" : ""}${rupees(paise)}`;
}

/** A server clause with a full stop unless it has one, as it came (a lead-in label goes before it); a dash when there is none. */
function clause(s: string | null | undefined): string {
  const t = sentenceOrDash(s);
  return t === "—" || /[.!?]$/.test(t) ? t : `${t}.`;
}

/** A server sentence standing alone: capitalised, with a full stop. */
function sentence(s: string | null | undefined): string {
  const t = clause(s);
  return t === "—" ? t : t[0].toUpperCase() + t.slice(1);
}

function settings(params: WalkforwardWindowDTO["chosen_params"], short: boolean): string {
  if (!params || Object.keys(params).length === 0) return "—";
  return Object.entries(params)
    .map(([k, v]) => `${short ? k.split(".").pop() : k} = ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
}

export default async function WalkforwardPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let load: WalkforwardLoad | null;
  try {
    load = await getWalkforward(id);
  } catch {
    load = null;
  }

  if (load === null) {
    return (
      <Shell title="Walk-forward report">
        <div role="alert">
          <Panel tone="red">
            <p>Couldn&apos;t reach the lab&apos;s server to load this report. Start it, then reload the page.</p>
          </Panel>
        </div>
      </Shell>
    );
  }
  if (load.kind === "unknown") {
    return (
      <Shell title="Walk-forward report">
        <Empty>There is no walk-forward job called {id}. Check the id and try again.</Empty>
      </Shell>
    );
  }
  if (load.kind === "unreadable") {
    return (
      <Shell title="Walk-forward report" lede={`Job ${id}`}>
        <Panel tone="red">
          <p>This job finished, but its result file is missing or can&apos;t be read, so there is no report to show.</p>
        </Panel>
      </Shell>
    );
  }
  if (load.kind === "not_done") {
    const running = load.status === "queued" || load.status === "running";
    return (
      <Shell title="Walk-forward report" lede={`Job ${id}`}>
        <Panel tone={running ? "info" : load.status === "failed" ? "red" : "amber"}>
          {running ? (
            <p role="status">Still running. The report appears here once every window has finished; reload to check.</p>
          ) : load.status === "failed" ? (
            <p>Failed{load.reason ? `: ${load.reason}` : ""}. A failed job has no report.</p>
          ) : (
            <p>This job is {load.status}, so it has no report.</p>
          )}
        </Panel>
      </Shell>
    );
  }
  return <Report r={load.report} />;
}

function Shell({ title, lede, children }: { title: string; lede?: string; children: ReactNode }) {
  return (
    <div className="stk-stack">
      <PageHead title={title} lede={lede} />
      {children}
    </div>
  );
}

function Report({ r }: { r: WalkforwardDTO }) {
  const p = r.plain;
  const s = r.summary;
  const tested = r.windows.filter((w) => w.test).length;
  const curve = wfCurve(r.stitched_dates, r.stitched_index, r.windows);
  const keys = new Set(r.windows.flatMap((w) => Object.keys(w.chosen_params ?? {})));
  const short = new Set([...keys].map((k) => k.split(".").pop())).size === keys.size;
  const familyHref = `/research/family?id=${encodeURIComponent(r.family_id)}`;

  return (
    <div className="stk-stack">
      <PageHead
        title={`Walk-forward: ${r.name}`}
        lede={clause(p.setup)}
        actions={<Link href={familyHref} className="stk-btn">See the whole family of tries</Link>}
      />

      <section className="stk-section" aria-labelledby="wf-ratio">
        <h2 id="wf-ratio">In-sample against out-of-sample</h2>
        <div className="stk-stack-s">
          <p className="stk-answer">Out-of-sample against in-sample: {clause(p.ratio)}</p>
          <StatGrid>
            <Stat label="In-sample Sharpe over a year" value={figure(s.is_sharpe)} />
            <Stat label="Out-of-sample Sharpe over a year" value={figure(s.oos_sharpe)} />
            <Stat label="Windows tested" value={`${tested.toLocaleString("en-IN")} of ${s.windows.toLocaleString("en-IN")}`} />
            <Stat label="Out-of-sample days" value={s.oos_days.toLocaleString("en-IN")} />
          </StatGrid>
          <p className="stk-fine">{clause(p.flatter)}</p>
          <p className="stk-fine">{clause(p.ratio_caveat)}</p>
          <p className="stk-fine">{clause(p.coverage)}</p>
          {p.few_days && (
            <Panel tone="amber">
              <p>{clause(p.few_days)}</p>
            </Panel>
          )}
        </div>
      </section>

      <section className="stk-section" aria-labelledby="wf-curve">
        <h2 id="wf-curve">Out-of-sample equity</h2>
        <p className="stk-lede">{clause(p.curve)} Each tested window is its own line; the days between windows are left empty.</p>
        <div className="stk-gap-top">
          {curve.points.length === 0 ? (
            <Empty>No out-of-sample days, so there is no curve to draw.</Empty>
          ) : (
            <Curve curve={curve} />
          )}
        </div>
      </section>

      <section className="stk-section" aria-labelledby="wf-dsr">
        <h2 id="wf-dsr">Deflated Sharpe</h2>
        <div className="stk-stack-s">
          <p className="stk-answer">{sentence(p.dsr)}</p>
          <p className="stk-fine">{clause(p.n_note)}</p>
        </div>
      </section>

      <section className="stk-section" aria-labelledby="wf-windows">
        <h2 id="wf-windows">Settings per window</h2>
        <div className="stk-stack-s">
          <p className="stk-answer">Stability: {clause(p.stability)}</p>
          <Table
            caption="Each window's training days, test days, the settings chosen and the results"
            head={[
              { label: "Window", num: true },
              { label: "Training days" },
              { label: "Test days" },
              { label: "Settings chosen" },
              { label: "In-sample Sharpe", num: true },
              { label: "Out-of-sample Sharpe", num: true },
              { label: "Out-of-sample net P&L", num: true },
              { label: "Result" },
            ]}
          >
            {r.windows.map((w) => (
              <tr key={w.index}>
                <th scope="row" className="num">{w.index + 1}</th>
                <td>{isoText(w.train_from)} to {isoText(w.train_to)}</td>
                <td>{isoText(w.test_from)} to {isoText(w.test_to)}</td>
                <td className="stk-mono">{settings(w.chosen_params, short)}</td>
                <td className="num">{sharpe(w.train?.sharpe)}</td>
                <td className="num">{sharpe(w.test?.sharpe)}</td>
                <td className="num">{w.test ? signedRupees(w.test.net_pnl_paise) : "—"}</td>
                <td>{w.result}</td>
              </tr>
            ))}
          </Table>
        </div>
      </section>

      <section className="stk-section" aria-labelledby="wf-holdout">
        <h2 id="wf-holdout">Holdout</h2>
        <p>{sentence(p.holdout)}</p>
      </section>

      <section className="stk-section" aria-labelledby="wf-pbo">
        <h2 id="wf-pbo">Chance of overfitting</h2>
        <p>Overfitting: {clause(p.pbo)}</p>
      </section>
    </div>
  );
}
