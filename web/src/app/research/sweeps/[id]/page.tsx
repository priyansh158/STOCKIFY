import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { stockifyCommand } from "@/lib/commands";
import { pct } from "@/lib/format";
import { getSweep, heatFormat, heatGridProps } from "@/lib/research";
import type { SweepDTO, SweepRowDTO } from "@/lib/types";
import { resolveTrial, trialHref, trialWords } from "@/lib/chart";
import { Chip, Empty, PageHead, Panel, Table } from "@/components/ui";
import AxisPicker from "./AxisPicker";
import SweepGrid from "./SweepGrid";
import TrialChart from "./TrialChart";

export const metadata: Metadata = { title: "Sweep - STOCKIFY" };
export const dynamic = "force-dynamic";

const upperFirst = (t: string) => (t ? t[0].toUpperCase() + t.slice(1) : t);

function plural(n: number, one: string, many: string): string {
  return `${n.toLocaleString("en-IN")} ${n === 1 ? one : many}`;
}

/** A setting's value as the API labels it (none, true, false, or the text). */
function valueLabel(v: unknown): string {
  return v === null || v === undefined ? "none" : typeof v === "boolean" ? (v ? "true" : "false") : String(v);
}

function Page({ title, lede, children }: { title: ReactNode; lede?: ReactNode; children?: ReactNode }) {
  return (
    <div className="stk-page">
      <PageHead
        title={title}
        lede={lede}
        actions={
          <Link className="stk-btn" href="/research">
            All research
          </Link>
        }
      />
      <div className="stk-stack">{children}</div>
    </div>
  );
}

function Notice({ tone, title, children }: { tone: "red" | "amber" | "info"; title: string; children: ReactNode }) {
  return (
    <Panel tone={tone}>
      <div role={tone === "red" ? "alert" : "status"} className="stk-stack-s">
        <p>
          <strong>{title}</strong>
        </p>
        <p>{children}</p>
      </div>
    </Panel>
  );
}

function Pick({ sweep, x, y }: { sweep: SweepDTO; x: string; y: string | null }) {
  if (sweep.axes.length < 2) {
    const a = sweep.axes[0];
    return (
      <p className="stk-fine">
        This sweep changes one setting, {a.label}
        {a.label !== a.path && (
          <>
            {" "}
            (<span className="stk-mono">{a.path}</span>)
          </>
        )}
        , so the grid is a single row.
      </p>
    );
  }
  return <AxisPicker axes={sweep.axes.map((a) => ({ path: a.path, label: a.label }))} x={x} y={y ?? sweep.axes.find((a) => a.path !== x)?.path ?? x} />;
}

function Results({ sweep, scored, pickedRunId }: { sweep: SweepDTO; scored: string; pickedRunId: string | null }) {
  const axes = sweep.axes;
  const fmt = heatGridProps(sweep.cells).format;
  const money = heatFormat("paise", true);
  const unfinished = sweep.rows.filter((r) => r.status !== "done").length;
  return (
    <section className="stk-section" aria-labelledby="results-h">
      <h2 id="results-h">Every trial</h2>
      <p className="stk-fine stk-gap-xs">
        {plural(sweep.rows.length, "trial", "trials")}, one for each combination of settings.
        {unfinished > 0 ? ` ${unfinished.toLocaleString("en-IN")} did not finish.` : ""}
      </p>
      <div className="stk-gap-s">
        <Table
          caption="Every trial of this sweep: its settings, its score and what it did"
          head={[
            ...axes.map((a) => ({ label: a.label, sub: a.label === a.path ? undefined : a.path })),
            { label: upperFirst(scored), num: true },
            ...(sweep.objective === "sharpe" ? [] : [{ label: "Sharpe ratio", num: true }]),
            ...(sweep.objective === "net_pnl" ? [] : [{ label: "Net profit or loss", num: true }]),
            { label: "Trades", num: true },
            { label: "Biggest drop", num: true },
            { label: "Result" },
          ]}
        >
          {sweep.rows.map((r: SweepRowDTO) => {
            const best = r.run_id !== null && r.run_id === sweep.best_run_id;
            const done = r.status === "done";
            return (
              <tr key={r.index} data-best={best ? "true" : undefined} data-picked={r.run_id !== null && r.run_id === pickedRunId ? "true" : undefined}>
                {axes.map((a) => (
                  <td key={a.path}>{valueLabel(r.params[a.path])}</td>
                ))}
                <td className="num">{r.objective_value === null ? "—" : fmt(r.objective_value)}</td>
                {sweep.objective !== "sharpe" && <td className="num">{r.sharpe === null ? "—" : r.sharpe.toFixed(2)}</td>}
                {sweep.objective !== "net_pnl" && <td className="num">{r.net_pnl_paise === null ? "—" : money(r.net_pnl_paise)}</td>}
                <td className="num">{r.trades === null ? "—" : r.trades.toLocaleString("en-IN")}</td>
                <td className="num">{r.max_drawdown === null ? "—" : pct(r.max_drawdown)}</td>
                <td data-wrap>
                  {r.run_id !== null && r.run_id === pickedRunId && <Chip tone="info">Chosen</Chip>}
                  {best && <Chip tone="up">Best</Chip>}
                  {!done && <Chip tone="warn">Did not finish</Chip>}
                  {!done && r.error && <div className="stk-label">{r.error}</div>}
                  {done && r.run_id && (
                    <div className="stk-row">
                      <Link href={trialHref(sweep.job_id, sweep.cells.x, sweep.cells.y, r.run_id)} aria-current={r.run_id === pickedRunId ? "true" : undefined}>
                        Show chart
                        <span className="sr-only"> for {trialWords(axes, r.params)}</span>
                      </Link>
                      <Link href={`/tests/${encodeURIComponent(r.run_id)}`}>
                        Open run
                        <span className="sr-only"> for {trialWords(axes, r.params)}</span>
                      </Link>
                    </div>
                  )}
                </td>
              </tr>
            );
          })}
        </Table>
      </div>
    </section>
  );
}

function TrialSection({ sweep, trial }: { sweep: SweepDTO; trial: ReturnType<typeof resolveTrial<SweepRowDTO>> }) {
  const row = trial.row;
  return (
    <section id="trial-chart" className="stk-section stk-trial" aria-label="Chart of the chosen trial">
      {row === null || row.run_id === null ? (
        <>
          <h2>Trial chart</h2>
          <Empty>
            {trial.kind === "none"
              ? "No trial finished with a usable result, so there is no chart to show."
              : "There is no finished trial with this id, so there is no chart to show. Choose a square in the grid or a row in the table."}
          </Empty>
        </>
      ) : (
        <div className="stk-stack-s">
          <p className="stk-fine">
            {trial.kind === "best" ? "This is the best trial. Choose another square or row to see its chart." : "This is the trial you chose."}{" "}
            <Link href={`/tests/${encodeURIComponent(row.run_id)}`}>Open its run page</Link>
          </p>
          <Panel>
            <TrialChart key={row.run_id} runId={row.run_id} pick={trial.kind === "picked"} title={`Trial: ${trialWords(sweep.axes, row.params)}`} />
          </Panel>
          <p>
            <Link href="#grid-h">Back to the grid</Link>
          </p>
        </div>
      )}
    </section>
  );
}

function Full({ sweep, warning, trialParam }: { sweep: SweepDTO; warning: boolean; trialParam: string | undefined }) {
  const trial = resolveTrial(trialParam, sweep.rows, sweep.best_run_id);
  const pickedRunId = trial.row?.run_id ?? null;
  const c = sweep.cells;
  const scored = sweep.objective_label;
  const none = c.cells.flat().every((v) => v === null);
  const caption = `${upperFirst(scored)}${c.hidden_axes.length > 0 ? ", best over the other settings" : ""}`;
  return (
    <Page title={sweep.name || "Parameter sweep"} lede={`${c.y === null ? "Each square is one setting" : "Each square is one pair of settings"}. Scored by: ${scored}.`}>
      {warning && (
        <Notice tone="amber" title="That choice of settings was not used.">
          Either this sweep has no setting by that name, or the same setting was chosen for both columns and rows. The grid below shows
          the first two settings instead.
        </Notice>
      )}
      <section className="stk-section" aria-labelledby="grid-h">
        <h2 id="grid-h">Heatmap</h2>
        <div className="stk-stack-s stk-gap-xs">
          <Pick sweep={sweep} x={c.x} y={c.y} />
          <p className="stk-lede">{sweep.plain}</p>
        </div>
        <div className="stk-gap-s">
          {none ? (
            <Empty>No trial finished with a usable result, so there is nothing to shade. The table below says what happened to each one.</Empty>
          ) : (
            <SweepGrid sweepId={sweep.job_id} pickedRunId={pickedRunId} cells={c} labels={Object.fromEntries(sweep.axes.map((a) => [a.path, a.label]))} scored={upperFirst(scored)} caption={caption} />
          )}
        </div>
      </section>
      <TrialSection sweep={sweep} trial={trial} />
      <Results sweep={sweep} scored={scored} pickedRunId={pickedRunId} />
    </Page>
  );
}

export default async function SweepPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ x?: string | string[]; y?: string | string[]; trial?: string | string[] }>;
}) {
  const { id } = await params;
  const sp = await searchParams;
  const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v) || undefined;
  let res: Awaited<ReturnType<typeof getSweep>>;
  let warning = false;
  try {
    res = await getSweep(id, first(sp.x), first(sp.y));
    // A choice the sweep cannot draw (an unknown setting, or one setting twice): say so and show the default grid.
    if (res.kind === "bad_axes") {
      warning = true;
      res = await getSweep(id);
    }
  } catch (e) {
    return (
      <Page title="Parameter sweep">
        <Notice tone="red" title="Couldn't load this sweep.">
          {e instanceof ApiError ? "The API answered with an error." : "The API did not answer."} Reload the page to try again.
        </Notice>
      </Page>
    );
  }
  switch (res.kind) {
    case "ready":
      return <Full sweep={res.sweep} warning={warning} trialParam={first(sp.trial)} />;
    case "running":
      return (
        <Page title="This sweep is still running">
          <Notice tone="info" title="There is no grid yet.">
            The trials have not all finished. Reload this page in a little while.
          </Notice>
        </Page>
      );
    case "failed":
      return (
        <Page title="This sweep failed">
          <Notice tone="red" title="It stopped before it finished, so there is no grid to show.">
            {res.reason ? `Why: ${res.reason}. ` : ""}Run it again with{" "}
            <code className="stk-mono">{stockifyCommand("sweep --config <your sweep.yaml>")}</code>, or open All research to see
            your other sweeps.
          </Notice>
        </Page>
      );
    case "unreadable":
      return (
        <Page title="This sweep's results can't be read">
          <Notice tone="red" title="The sweep finished, but its saved results are damaged or incomplete.">
            Run the sweep again to get a fresh set.
          </Notice>
        </Page>
      );
    default:
      return (
        <Page title="No sweep with this id">
          <Empty>There is no sweep with this id. It may have been mistyped. All research lists the sweeps that exist.</Empty>
        </Page>
      );
  }
}
