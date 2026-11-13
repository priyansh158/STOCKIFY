import Link from "next/link";
import { growth, istWhen, pct, rupees, rupeesWhole } from "@/lib/format";
import { listStrategies, listTests } from "@/lib/tests";
import type { Strategy, TestRun } from "@/lib/types";
import NewTestForm from "./NewTestForm";
import { Card, LabelChips, Stat, StatusChip, runSummary } from "./ui";

export const dynamic = "force-dynamic";

async function load(): Promise<{ runs: TestRun[]; strategies: Strategy[]; error: string | null }> {
  try {
    const [runs, strategies] = await Promise.all([listTests(), listStrategies()]);
    return { runs, strategies, error: null };
  } catch (e) {
    return { runs: [], strategies: [], error: e instanceof Error ? e.message : String(e) };
  }
}

function RunCard({ run }: { run: TestRun }) {
  const g = growth(run.nav_start_paise, run.nav_end_paise);
  return (
    <li>
      <Link href={`/tests/${encodeURIComponent(run.run_id)}`} className="block min-h-11">
        <Card tone={run.status === "failed" ? "red" : undefined}>
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <StatusChip status={run.status} />
            <span className="stk-mono-tight text-xs" style={{ color: "var(--color-fg-muted)" }}>
              {istWhen(run.created_ist)}
            </span>
          </div>
          <div className="font-medium" style={{ color: "var(--color-fg)" }}>
            {run.name ?? run.run_id}
          </div>
          {runSummary(run) && (
            <div className="text-sm" style={{ color: "var(--color-fg-muted)" }}>
              {runSummary(run)}
            </div>
          )}
          {run.status === "done" && (
            <div className="mt-3 grid grid-cols-2 gap-3">
              <div className="col-span-2">
                <Stat label="Money, start → end" value={`${rupeesWhole(run.nav_start_paise)} → ${rupeesWhole(run.nav_end_paise)}`} />
              </div>
              <Stat label="Gain or loss" value={pct(g, true)} tone={g === null ? undefined : g >= 0 ? "up" : "down"} />
              <Stat label="Worst fall from a high" value={pct(run.max_drawdown)} />
              <Stat label="Trades" value={String(run.trades ?? 0)} />
              <Stat label="Costs paid" value={rupees(run.costs_total_paise)} />
            </div>
          )}
          {run.status === "failed" && run.error && (
            <p className="stk-mono mt-2 break-words text-xs" style={{ color: "var(--color-red-soft)" }}>{run.error}</p>
          )}
          <div className="mt-2">
            <LabelChips run={run} />
          </div>
        </Card>
      </Link>
    </li>
  );
}

export default async function TestsPage({ searchParams }: { searchParams: Promise<{ new?: string }> }) {
  const { new: openForm } = await searchParams;
  const { runs, strategies, error } = await load();
  return (
    <div className="mx-auto max-w-2xl px-4 py-8 sm:py-12">
      <div className="mb-6 flex items-center justify-between">
        <Link href="/" className="stk-mono-tight flex min-h-11 items-center" style={{ color: "var(--color-fg-muted)" }}>
          ← Home
        </Link>
        <div className="stk-mono-tight" style={{ color: "var(--color-fg-muted)" }}>
          {runs.length} test{runs.length === 1 ? "" : "s"}
        </div>
      </div>

      <h1 className="stk-mono mb-2 text-2xl font-bold sm:text-3xl" style={{ color: "var(--color-green)" }}>
        Strategy tests
      </h1>
      <p className="mb-6 text-sm" style={{ color: "var(--color-fg-muted)" }}>
        Backtests on recorded and downloaded NSE data, with real costs. Simulation only: no real orders.
      </p>

      {error && (
        <div className="mb-4">
          <Card tone="red">
            <p className="text-sm" style={{ color: "var(--color-red-soft)" }}>Could not reach the API: {error}</p>
          </Card>
        </div>
      )}

      <NewTestForm strategies={strategies} startOpen={openForm === "1"} />

      <h2 className="stk-mono-tight mb-3 mt-8" style={{ color: "var(--color-fg-muted)" }}>Recent tests</h2>
      {runs.length === 0 ? (
        <Card>
          <p className="py-6 text-center text-sm" style={{ color: "var(--color-fg-muted)" }}>
            No tests yet. Start one above.
          </p>
        </Card>
      ) : (
        <ul className="flex flex-col gap-3">
          {runs.map((r) => (
            <RunCard key={r.run_id} run={r} />
          ))}
        </ul>
      )}
    </div>
  );
}
