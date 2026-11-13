"use client";

import { useEffect, useState, type ReactNode } from "react";
import { pct, rupees } from "@/lib/format";
import { bucketTone, getRunResearch, getSensitivity, sentenceOrDash } from "@/lib/research";
import type { RegimeBucketDTO, RunResearchDTO, SensitivityDTO } from "@/lib/types";
import { IntervalBar } from "@/components/charts";
import { Chip, Loading, Panel, Skeleton, Table } from "@/components/ui";

/** What each regime split is for. These are the server's own one-line meanings (stockify/research/plain.py), not new wording. */
const REGIMES: { key: "month" | "weekday" | "expiry" | "vix" | "gap"; title: string; meaning: string; first: string }[] = [
  { key: "month", title: "By calendar month", first: "Month",
    meaning: "Results split by calendar month, to show whether the profit comes from only a few months." },
  { key: "weekday", title: "By day of the week", first: "Day",
    meaning: "Results split by day of the week, to show whether the profit sits on one or two days." },
  { key: "expiry", title: "Expiry days and other days", first: "Group",
    meaning: "Results on expiry days compared with all other days." },
  { key: "vix", title: "By the day before's India VIX", first: "VIX band",
    meaning: "Results split into four bands of the day before's India VIX (calm to jumpy), plus a no-data group." },
  { key: "gap", title: "By how far the market opened from the previous close", first: "Opening gap",
    meaning: "Results split into five bands of how far the market opened from the previous close, plus no data." },
];

const HOLDOUT_MEANING = "Times you looked at the recent days you had set aside. Each look makes them a less fair test.";

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
 * the numbers beside it. Each region (research, sensitivity) has its own loading, error, empty and full state. */
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
        {research.state === "error" && (
          <div role="alert">
            <Panel tone="red">
              <p>The trust checks could not be loaded: {research.message}. Reload the page to try again.</p>
            </Panel>
          </div>
        )}
        {research.state === "ready" && <Research r={research.value} />}
        {research.state === "ready" && research.value.regimes && <Regimes r={research.value} />}
        <Sensitivity runId={runId} load={sens} />
      </div>
    </section>
  );
}

function Research({ r }: { r: RunResearchDTO }) {
  const p = r.plain;
  const b = r.bootstrap;
  return (
    <>
      <div className="stk-trust-head">
        <p className="stk-trust-line">{sentence(p.dsr)}</p>
        <dl className="stk-trust-facts">
          <div><dt>Tries</dt><dd>{r.n_trials.toLocaleString("en-IN")}</dd></div>
          {r.dsr && r.is_best && <div><dt>Deflated Sharpe</dt><dd>{pct(r.dsr.dsr)}</dd></div>}
          {r.dsr && <div><dt>Sharpe above zero, this run alone</dt><dd>{pct(r.dsr.psr)}</dd></div>}
        </dl>
        {r.dsr && p.psr && <p className="stk-fine">{sentence(p.psr)}</p>}
        {r.is_best && (
          <p className="stk-fine">
            Deflated Sharpe: the chance the real Sharpe beats what the luckiest of all your tries would show by chance alone.
          </p>
        )}
      </div>

      {r.holdout_unlocks > 0 && (
        <Panel tone="amber">
          <p>
            <strong>
              You have looked at the days you set aside {r.holdout_unlocks.toLocaleString("en-IN")} {r.holdout_unlocks === 1 ? "time" : "times"}.
            </strong>{" "}
            {HOLDOUT_MEANING}
          </p>
        </Panel>
      )}

      <div className="stk-trust-block">
        <h3>How far the numbers could swing</h3>
        {b === null ? (
          <p className="stk-muted">No range for this run: it has no daily results to re-draw.</p>
        ) : (
          <>
            {!b.enough_days && <p className="stk-warn">This run has only {b.n_days} days, too few for a reliable range.</p>}
            <div className="stk-trust-intervals">
              <IntervalRow name="Sharpe" iv={b.sharpe} format={(n) => n.toFixed(2)} text={p.bootstrap_sharpe} />
              <IntervalRow name="Average day" iv={b.mean_daily_return} format={(n) => pct(n, true)} text={p.bootstrap_mean_return} />
              <IntervalRow name="Worst drop" iv={b.max_drawdown} format={(n) => pct(n)} text={p.bootstrap_max_drawdown} />
            </div>
          </>
        )}
      </div>
    </>
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
  const reg = r.regimes!;
  return (
    <div className="stk-trust-block">
      <h3>Where the profit came from</h3>
      {r.plain.regimes_caveat && <p className="stk-fine">{r.plain.regimes_caveat}</p>}
      <div className="stk-stack">
        {REGIMES.map(({ key, title, meaning, first }) => (
          <div key={key} className="stk-stack-s">
            <h4>{title}</h4>
            <p className="stk-fine">{meaning}</p>
            <RegimeTable rows={reg[key]} first={first} caption={`${title}: days, gain or loss, and share of up days`} />
          </div>
        ))}
        {reg.notes.length > 0 && <ul className="stk-bullets">{reg.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
      </div>
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
        { label: "Average a day", num: true }, { label: "Up days", num: true },
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
    body = (
      <div role="alert">
        <Panel tone="red"><p>The sensitivity check could not be loaded: {load.message}. Reload the page to try again.</p></Panel>
      </div>
    );
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
                    : row.error ? `Did not finish: ${row.error}` : upper1(row.status)}
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
        {s.reasons.length > 0 && <ul className="stk-bullets">{s.reasons.map((x) => <li key={x}>{sentence(x)}</li>)}</ul>}
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
