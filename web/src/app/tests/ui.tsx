/** Small presentational pieces shared by /tests and /tests/[id]. The label wording itself is in lib/labels.ts. */
import { Chip } from "@/components/ui";
import { dayWords } from "@/lib/format";
import { labelMeaning, labelText, marginStep } from "@/lib/labels";
import { statusWords } from "@/lib/names";
import type { TestRun, TestStatus } from "@/lib/types";

const TONE: Record<TestStatus, "up" | "down" | "warn" | "info" | undefined> = {
  queued: undefined,
  running: "info",
  done: "up",
  failed: "down",
  cancelled: "warn",
};

export { labelMeaning, labelText, marginStep };

export function StatusChip({ status }: { status: TestStatus }) {
  return <Chip tone={TONE[status]}>{statusWords(status)}</Chip>;
}

export function LabelChips({ run }: { run: Pick<TestRun, "labels" | "price_basis"> }) {
  const items = run.labels.map(labelText);
  if (run.price_basis === "adjusted") items.push({ text: "Adjusted prices", warn: true });
  if (items.length === 0) return null;
  return (
    <div className="stk-chips">
      {items.map((l) => (
        <Chip key={l.text} tone={l.warn ? "warn" : undefined}>
          {l.text}
        </Chip>
      ))}
    </div>
  );
}

/** "SMA Crossover, 2024-01-01 to 2026-09-30", leaving out whatever the run does not say. The strategy's name is the server's. */
export function runSummary(run: Pick<TestRun, "strategy_name" | "start_date" | "end_date">, from?: string | null,
                           to?: string | null): string {
  const a = run.start_date ?? from;
  const b = run.end_date ?? to;
  const parts = [run.strategy_name || null, a && b ? `${dayWords(a)} to ${dayWords(b)}` : null];
  return parts.filter(Boolean).join(", ");
}
