import Link from "next/link";
import { growth, istWhen, pct, rupees, rupeesWhole } from "@/lib/format";
import { listStrategies, listTests } from "@/lib/tests";
import { runIdTail } from "@/lib/runid";
import type { Strategy, TestRun } from "@/lib/types";
import { Empty, PageHead, Panel, Table, toneClass } from "@/components/ui";
import CompareSelect from "./CompareSelect";
import NewTestForm from "./NewTestForm";
import { LabelChips, StatusChip, runSummary } from "./ui";

export const dynamic = "force-dynamic";

async function load(): Promise<{ runs: TestRun[]; strategies: Strategy[]; error: string | null }> {
  try {
    const [runs, strategies] = await Promise.all([listTests(), listStrategies()]);
    return { runs, strategies, error: null };
  } catch (e) {
    return { runs: [], strategies: [], error: e instanceof Error ? e.message : String(e) };
  }
}

function RunRow({ run }: { run: TestRun }) {
  const done = run.status === "done";
  const g = done ? growth(run.nav_start_paise, run.nav_end_paise) : null;
  return (
    <tr>
      <td className="stk-pick">
        {done ? (
          <input type="checkbox" name="ids" value={run.run_id} className="stk-check" aria-label={`Compare ${run.name_label || run.name || run.run_id}`} />
        ) : null}
      </td>
      <td data-wrap>
        <Link href={`/tests/${encodeURIComponent(run.run_id)}`} title={run.name ? `Saved as ${run.name}` : undefined}>{run.name_label || run.name || run.run_id}</Link>
        {run.name && <div className="stk-label" title={run.run_id}>Run {runIdTail(run.run_id, run.name)}</div>}
        {runSummary(run) && <div className="stk-label">{runSummary(run)}</div>}
        {run.status === "failed" && run.error && <div className="stk-down stk-label">{run.error}</div>}
        <div className="stk-gap-xs stk-run-chips">
          <LabelChips run={run} />
        </div>
      </td>
      <td>
        <StatusChip status={run.status} />
      </td>
      <td className="stk-label">{istWhen(run.created_ist)}</td>
      <td className="num">{done ? rupeesWhole(run.nav_start_paise) : "—"}</td>
      <td className="num">{done ? rupeesWhole(run.nav_end_paise) : "—"}</td>
      <td className={`num ${toneClass(g === null ? undefined : g >= 0 ? "up" : "down") ?? ""}`}>{done ? pct(g, true) : "—"}</td>
      <td className="num">{done ? pct(run.max_drawdown) : "—"}</td>
      <td className="num">{done ? String(run.trades ?? 0) : "—"}</td>
      <td className="num">{done ? rupees(run.costs_total_paise) : "—"}</td>
    </tr>
  );
}

export default async function TestsPage({ searchParams }: { searchParams: Promise<{ new?: string }> }) {
  const { new: openForm } = await searchParams;
  const { runs, strategies, error } = await load();
  return (
    <div className="stk-page">
      <PageHead
        title="Strategy tests"
        lede="Backtests on recorded and downloaded NSE data, with real costs."
      />

      <div className="stk-stack">
        {error && (
          <Panel tone="red">
            <p role="alert">
              Could not reach the STOCKIFY API. Check that the app is running, then reload this page.{" "}
              <span className="stk-fine stk-mono">{error}</span>
            </p>
          </Panel>
        )}

        <NewTestForm strategies={strategies} startOpen={openForm === "1"} />

        <section className="stk-section">
          <h2>Recent tests{runs.length > 0 ? ` (${runs.length})` : ""}</h2>
          {runs.length === 0 ? (
            <Empty>No tests yet. Start one above.</Empty>
          ) : (
            <CompareSelect>
            <Table
              tall
              caption="Recent strategy tests. Tick two to six finished tests to compare them."
              head={[
                { label: "Compare" },
                { label: "Test" },
                { label: "Status" },
                { label: "Started" },
                { label: "Started with", num: true },
                { label: "Ended with", num: true },
                { label: "Gain or loss", num: true },
                { label: "Worst fall from a high", num: true },
                { label: "Trades", num: true },
                { label: "Costs paid", num: true },
              ]}
            >
              {runs.map((r) => (
                <RunRow key={r.run_id} run={r} />
              ))}
            </Table>
            </CompareSelect>
          )}
        </section>
      </div>
    </div>
  );
}
