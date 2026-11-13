"use client";

import { useEffect, useState, type ReactNode } from "react";
import { pct, rupees } from "@/lib/format";
import { bucketTone, getRunResearch, getSensitivity, sentenceOrDash } from "@/lib/research";
import type { RegimeBucketDTO, RunResearchDTO, SensitivityDTO } from "@/lib/types";
import { IntervalBar } from "@/components/charts";
import { Chip, Loading, Panel, Skeleton, Table } from "@/components/ui";

/** The five regime splits: key in the DTO, its heading and the first column's name. What each split is for comes from
 * the server (plain.regime_*), never from this file. */
const REGIMES: { key: "month" | "weekday" | "expiry" | "vix" | "gap"; title: string; first: string }[] = [
  { key: "month", title: "By calendar month", first: "Month" },
  { key: "weekday", title: "By day of the week", first: "Day" },
  { key: "expiry", title: "Expiry days and other days", first: "Group" },
  { key: "vix", title: "By the day before's India VIX", first: "VIX band" },
  { key: "gap", title: "By how far the market opened from the previous close", first: "Opening gap" },
];

const VERDICT: Record<string, { word: string; tone: "up" | "down" | "warn" | "info" }> = {
  fragile: { word: "Fragile", tone: "warn" },
  robust: { word: "Held up", tone: "up" },
  inconclusive: { word: "Not conclusive", tone: "info" },
  already_losing: { word: "Already losing", tone: "down" },
};

function upper1(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** A sentence from the server, capitalised, with a full stop unless it already ends in one (or is the dash). */
function sentence(s: string | null | undefined): string {
  const t = upper1(sentenceOrDash(s));
  return t === "—" || /[.!?]$/.test(t) ? t : `${t}.`;
}

function signedRupees(paise: number | null): string {
  if (paise === null) return "—";
  return `${paise > 0 ? "+" : ""}${rupees(paise)}`;
}

type Load<T> = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; value: T };

/** The run page's "How much to trust this result" panel. Every sentence is the server's `plain` text; this file adds only
 * the numbers beside it. Each region (headline, sensitivity, swing, regimes) has its own loading, error, empty and full
 * state. The answer comes first: the headline sentence, then whether the result survives worse conditions. */
export default function ResearchPanel({ runId }: { runId: string }) {
  const [research, setResearch] = useState<Load<RunResearchDTO>>({ state: "loading" });
  const [sens, setSens] = useState<Load<SensitivityDTO | null>>({ state: "loading" });

  useEffect(() => {
    let stop = false;
    const msg = (e: unknown) => (e instanceof Error ? e.message : String(e));
    getRunResearch(runId).then(
      (value) => !stop && setResearch({ state: "ready", value }),
      (e) => !stop && setResearch({ state: "error", message: msg(e) }),
    );
    getSensitivity(runId).then(
      (value) => !stop && setSens({ state: "ready", value }),
      (e) => !stop && setSens({ state: "error", message: msg(e) }),
    );
    return () => {
      stop = true;
    };
  }, [runId]);

  return (
    <section className="stk-section stk-trust" aria-labelledby="trust-h">
      <h2 id="trust-h">How much to trust this result</h2>
      <p className="stk-lede">
        A good-looking result can be luck. These checks say how likely that is. They describe this run&apos;s own days and are not a forecast.
      </p>
      <div className="stk-stack stk-gap-top">
        {research.state === "loading" && (
          <Loading label="Loading the trust checks">
            <Skeleton size="title" />
            <Skeleton size="l" />
          </Loading>
        )}
        {research.state === "error" && <LoadError what="the research for this run" detail={research.message} />}
        {research.state === "ready" && <Headline r={research.value} />}
        <Sensitivity runId={runId} load={sens} />
        {research.state === "ready" && <Swing r={research.value} />}
        {research.state === "ready" && <Regimes r={research.value} />}
      </div>
    </section>
  );
}

/** A failed region: a plain sentence for the reader, the server's raw message behind a disclosure. */
function LoadError({ what, detail }: { what: string; detail: string }) {
  return (
    <div role="alert">
      <Panel tone="red">
        <p>Couldn&apos;t load {what}. Reload the page to try again.</p>
        <details className="stk-trust-details">
          <summary>Technical details</summary>
          <p className="stk-mono stk-break">{detail}</p>
        </details>
      </Panel>
    </div>
  );
}

/** The one-line answer first: where this run stands among its tries. The number is in the sentence, so it is not repeated. */
function Headline({ r }: { r: RunResearchDTO }) {
  const p = r.plain;
  return (
    <div className="stk-trust-head">
      <p className="stk-trust-line">{sentence(p.dsr)}</p>
      {r.dsr && p.psr && <p>{sentence(p.psr)}</p>}
      <p className="stk-fine">
        <strong>{r.n_trials.toLocaleString("en-IN")} {r.n_trials === 1 ? "try" : "tries"}.</strong>{" "}
        {p.trials ? sentence(p.trials) : null} {r.is_best && p.dsr_meaning ? sentence(p.dsr_meaning) : null}
      </p>
      {r.holdout_unlocks > 0 && (
        <Panel tone="amber">
          <p>
            <strong>
              You have looked at the days you set aside {r.holdout_unlocks.toLocaleString("en-IN")} {r.holdout_unlocks === 1 ? "time" : "times"}.
            </strong>{" "}
            {p.holdout_unlocks ? sentence(p.holdout_unlocks) : null}
          </p>
        </Panel>
      )}
    </div>
  );
}

function Swing({ r }: { r: RunResearchDTO }) {
  const p = r.plain;
  const b = r.bootstrap;
  return (
    <div className="stk-trust-block">
      <h3>How far the numbers could swing</h3>
      {b === null ? (
        <p className="stk-muted">No range for this run: it has no daily results to re-draw.</p>
      ) : (
        <>
          {!b.enough_days && <p className="stk-warn">This run has only {b.n_days === 1 ? "1 day" : `${b.n_days} days`}, too few for a reliable range.</p>}
          <div className="stk-trust-intervals">
            <IntervalRow name="Sharpe" iv={b.sharpe} format={(n) => n.toFixed(2)} text={p.bootstrap_sharpe} />
            <IntervalRow name="Average day" iv={b.mean_daily_return} format={(n) => pct(n, true)} text={p.bootstrap_mean_return} />
            <IntervalRow name="Worst drop" iv={b.max_drawdown} format={(n) => pct(n)} text={p.bootstrap_max_drawdown} />
          </div>
        </>
      )}
    </div>
  );
}

function IntervalRow({ name, iv, format, text }: {
  name: string; iv: { point: number | null; lo: number | null; hi: number | null }; format: (n: number) => string; text?: string;
}) {
  return (
    <div className="stk-trust-interval">
      <IntervalBar label={name} point={iv.point} lo={iv.lo} hi={iv.hi} format={format} />
      <p className="stk-fine">{sentence(text)}</p>
    </div>
  );
}

function Regimes({ r }: { r: RunResearchDTO }) {
  const reg = r.regimes;
  return (
    <div className="stk-trust-block">
      <h3>Where the profit came from</h3>
      {reg === null ? (
        <p className="stk-muted">No split by month, weekday, expiry, volatility or opening gap for this run: it has no daily results.</p>
      ) : (
        <>
          {r.plain.regimes_caveat && <p className="stk-fine">{r.plain.regimes_caveat}</p>}
          <div className="stk-stack">
            {REGIMES.map(({ key, title, first }) => (
              <details key={key} className="stk-regime">
                <summary>
                  <h4>{title}</h4>
                  <span className="stk-label">{reg[key].length === 1 ? "1 row" : `${reg[key].length} rows`}</span>
                </summary>
                <div className="stk-stack-s">
                  {r.plain[`regime_${key}`] && <p className="stk-fine">{r.plain[`regime_${key}`]}</p>}
                  <RegimeTable rows={reg[key]} first={first} caption={`${title}: days, gain or loss, and share of up days`} />
                </div>
              </details>
            ))}
            {reg.notes.length > 0 && <ul className="stk-bullets">{reg.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
          </div>
        </>
      )}
    </div>
  );
}

function RegimeTable({ rows, first, caption }: { rows: RegimeBucketDTO[]; first: string; caption: string }) {
  if (rows.length === 0) return <p className="stk-muted">No days fall in this split.</p>;
  return (
    <Table
      caption={caption}
      head={[
        { label: first }, { label: "Days", num: true }, { label: "Total gain or loss", num: true },
        { label: "Average a day", num: true }, { label: "Up days", num: true }, { label: "In words" },
      ]}
    >
      {rows.map((b) => {
        const tone = bucketTone(b);
        const cls = tone === "up" ? "stk-up" : tone === "down" ? "stk-down" : undefined;
        return (
          <tr key={b.label}>
            <td>
              {b.label}
              {b.thin && <>{" "}<Chip tone="warn">few days</Chip></>}
            </td>
            <td className="num">{b.days.toLocaleString("en-IN")}</td>
            <td className={cls ? `num ${cls}` : "num"}>{signedRupees(b.pnl_paise)}</td>
            <td className="num">{signedRupees(b.mean_pnl_paise)}</td>
            <td className="num">{b.thin || b.up_share === null ? "—" : `${b.up_days.toLocaleString("en-IN")} (${pct(b.up_share)})`}</td>
            <td data-wrap>{sentenceOrDash(b.plain)}</td>
          </tr>
        );
      })}
    </Table>
  );
}

function Sensitivity({ runId, load }: { runId: string; load: Load<SensitivityDTO | null> }) {
  let body: ReactNode;
  if (load.state === "loading") {
    body = <Loading label="Loading the sensitivity check"><Skeleton size="l" /></Loading>;
  } else if (load.state === "error") {
    body = <LoadError what="the sensitivity check" detail={load.message} />;
  } else if (load.value === null) {
    body = (
      <div className="stk-stack-s">
        <p>No sensitivity check for this run yet.</p>
        <p className="stk-fine">
          To run it, use this command in a terminal in the STOCKIFY folder. It re-runs the test with higher costs and worse fills.
        </p>
        <code className="stk-mono stk-break">.\.venv\Scripts\python.exe -m stockify sensitivity --run {runId}</code>
      </div>
    );
  } else {
    const s = load.value;
    const v = VERDICT[s.verdict];
    body = (
      <div className="stk-stack-s">
        <p className="stk-row">
          {v && <Chip tone={v.tone}>{v.word}</Chip>}
          <span>{sentence(s.plain)}</span>
        </p>
        {s.reasons.length > 0 && <ul className="stk-bullets">{s.reasons.map((x) => <li key={x}>{sentence(x)}</li>)}</ul>}
        <Table
          caption="The test re-run with higher costs and worse fills"
          head={[
            { label: "What changed" }, { label: "Result" }, { label: "Net gain or loss", num: true },
            { label: "Difference from this run", num: true }, { label: "Sharpe", num: true },
          ]}
        >
          {s.rows.map((row) => {
            const d = row.delta_net_paise;
            return (
              <tr key={row.key}>
                <td data-wrap>{row.label}</td>
                <td data-wrap>
                  {row.status === "baseline" ? "This run" : row.status === "done" ? "Finished"
                    : row.error ? "Did not finish" : upper1(row.status)}
                </td>
                <td className="num">{signedRupees(row.net_pnl_paise)}</td>
                <td className={d !== null && d > 0 ? "num stk-up" : d !== null && d < 0 ? "num stk-down" : "num"}>
                  {row.status === "baseline" ? "—" : signedRupees(d)}
                </td>
                <td className="num">{row.sharpe === null ? "—" : row.sharpe.toFixed(2)}</td>
              </tr>
            );
          })}
        </Table>
      </div>
    );
  }
  return (
    <div className="stk-trust-block">
      <h3>Does it survive worse conditions?</h3>
      {body}
    </div>
  );
}
