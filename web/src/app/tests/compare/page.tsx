/**
 * /tests/compare?ids=a&ids=b…: two to six finished runs side by side. The numbers come from GET /api/tests/compare; the
 * words about what each metric means are the server's `plain` text, never re-worded here. A wrong count or an unknown
 * run is a plain message, not an error page. Differences are marked with a word and a sign as well as a tint.
 */
import Link from "next/link";
import type { ReactNode } from "react";
import { Chip, Empty, PageHead, Panel, Table } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { pct, rupees } from "@/lib/format";
import {
  COMPARE_MAX, COMPARE_MIN, compareIds, dayNumber, getComparison, runLabels, sentenceOrDash, valuesDiffer,
} from "@/lib/research";
import type { CompareDiffDTO, CompareMetricDTO, CompareSeriesDTO, ComparisonDTO } from "@/lib/types";
import { labelText } from "../ui";
import CompareCharts from "./Charts";

export const dynamic = "force-dynamic";

type Load = { kind: "ok"; data: ComparisonDTO } | { kind: "refused"; text: string; notFound: boolean } | { kind: "down" };

async function load(ids: string[]): Promise<Load> {
  try {
    return { kind: "ok", data: await getComparison(ids) };
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 422)) {
      return { kind: "refused", text: e.message, notFound: e.status === 404 };
    }
    return { kind: "down" };
  }
}

function num(v: number | null, kind: string, signed = false): string {
  if (v === null || v === undefined) return "n/a";
  if (kind === "money") return `${signed && v > 0 ? "+" : ""}${rupees(v)}`;
  if (kind === "pct") return pct(v, signed);
  if (kind === "ratio") return v.toFixed(2);
  return Math.round(v).toLocaleString("en-IN");
}

/** A long hex fingerprint is cut to its first 12 characters (the whole one is the hover text); anything else in words. */
function Value({ v, shown, field }: { v: string | null; shown: string | null; field?: string }) {
  if (v === null || v === "") return <span className="stk-label">not recorded</span>;
  if (/^[0-9a-f]{32,}$/i.test(v)) {
    // the short form is for the eye; the whole value is in the text for screen readers, selection and copy
    return (
      <span className="stk-cmp-mono">
        <span aria-hidden="true" title={v}>{v.slice(0, 12)}…</span>
        <span className="sr-only">{v}</span>
      </span>
    );
  }
  // the server's words for the value (a strategy by its name, a setting in plain words); the stored value is the hover
  return (
    <span title={shown !== null && shown !== v ? `Saved as ${v}` : undefined} data-field={field}>
      {shown ?? v}
    </span>
  );
}

function Verdict({ differs }: { differs: boolean }) {
  return (
    <td className="stk-cmp-verdict" data-differs={differs ? "" : undefined}>
      <span aria-hidden="true">{differs ? "≠ " : "= "}</span>
      {differs ? "differs" : "same"}
    </td>
  );
}

function series(list: CompareSeriesDTO[], labels: string[], map: (v: number) => number): { label: string; points: [number, number][] }[] {
  return list.map((s, i) => ({
    label: labels[i] ?? s.run_id,
    points: s.dates.flatMap((d, k): [number, number][] => {
      const v = s.values[k];
      return typeof v === "number" && Number.isFinite(v) ? [[dayNumber(d), map(v)]] : [];
    }),
  }));
}

function Metrics({ rows, labels }: { rows: CompareMetricDTO[]; labels: string[] }) {
  return (
    <Table
      caption="Metrics of each run. The second column says whether the runs differ on that row."
      head={[{ label: "Metric" }, { label: "Across the runs" }, ...labels.map((l) => ({ label: l, num: true }))]}
    >
      {rows.map((m) => {
        const differs = valuesDiffer(m.values);
        return (
          <tr key={m.key}>
            <td data-wrap>
              {m.label}
              <p className="stk-label">{sentenceOrDash(m.plain)}</p>
            </td>
            <Verdict differs={differs} />
            {m.values.map((v, i) => (
              <td key={labels[i]} className="num" data-differs={differs ? "" : undefined}>
                {v === null
                  ? (m.reasons?.[i] ? <span className="stk-cmp-words" title="not shown for this run">{m.reasons[i]}</span> : <abbr className="stk-cmp-words" title="not available">n/a</abbr>)
                  : num(v, m.kind, m.key === "net_pnl" || m.key === "return")}
              </td>
            ))}
          </tr>
        );
      })}
    </Table>
  );
}

function DiffTable({ rows, labels, caption }: { rows: CompareDiffDTO[]; labels: string[]; caption: string }) {
  return (
    <Table caption={caption} head={[{ label: "What" }, { label: "Across the runs" }, ...labels.map((l) => ({ label: l }))]}>
      {rows.map((r) => (
        <tr key={r.field}>
          <td data-wrap title={r.field}>{r.label || r.field}</td>
          <Verdict differs={r.differs} />
          {r.values.map((v, i) => (
            <td key={labels[i]} data-wrap data-differs={r.differs ? "" : undefined}>
              <Value v={v} shown={r.shown?.[i] ?? null} field={r.field} />
            </td>
          ))}
        </tr>
      ))}
    </Table>
  );
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="stk-page">
      <PageHead
        title="Compare tests"
        lede="Two to six finished tests side by side: how they did, and what was set up differently."
        actions={<Link href="/tests" className="stk-btn">Back to the tests</Link>}
      />
      <div className="stk-stack">{children}</div>
    </div>
  );
}

export default async function ComparePage({ searchParams }: { searchParams: Promise<{ ids?: string | string[] }> }) {
  const ids = compareIds((await searchParams).ids);

  if (ids.length < COMPARE_MIN || ids.length > COMPARE_MAX) {
    return (
      <Shell>
        <Empty>
          {ids.length === 0 ? "No tests chosen." : `${ids.length} test${ids.length === 1 ? "" : "s"} chosen.`} Comparing takes{" "}
          {COMPARE_MIN} to {COMPARE_MAX}. Go back to <Link href="/tests">the tests</Link>, tick the finished ones you want and press Compare.
        </Empty>
      </Shell>
    );
  }

  const got = await load(ids);
  if (got.kind === "down") {
    return (
      <Shell>
        <div role="alert">
          <Panel tone="red">
            <p>Couldn&apos;t reach the lab&apos;s server to compare these tests. Start it, then reload the page.</p>
          </Panel>
        </div>
      </Shell>
    );
  }
  if (got.kind === "refused") {
    return (
      <Shell>
        <div role="alert">
          <Panel tone="red">
            <p>{got.notFound ? "One of these tests does not exist" : "These tests can't be compared"}: {got.text}</p>
            <p className="stk-label">
              Go back to <Link href="/tests">the tests</Link> and choose again. Only finished tests can be compared.
            </p>
          </Panel>
        </div>
      </Shell>
    );
  }

  const c = got.data;
  const labels = runLabels(c.name_labels.length === c.names.length ? c.name_labels : c.names, c.run_ids);
  const diffs = c.manifest_diff.filter((d) => d.differs);
  const same = c.manifest_diff.filter((d) => !d.differs);
  const labelRows = Object.entries(c.labels);
  return (
    <Shell>
      <ul className="stk-cmp-pick" aria-label="The tests being compared">
        {c.run_ids.map((id, i) => (
          <li key={id}>
            <Link href={`/tests/${encodeURIComponent(id)}`}>{labels[i]}</Link>
          </li>
        ))}
      </ul>

      {c.notes.length > 0 && (
        <Panel tone="amber">
          {c.notes.map((n) => (
            <p key={n}>{n}</p>
          ))}
        </Panel>
      )}

      <section className="stk-section stk-cmp">
        <h2>How each test did</h2>
        <Metrics rows={c.metrics} labels={labels} />
      </section>

      <CompareCharts equity={series(c.equity, labels, (v) => v)} drawdown={series(c.drawdown, labels, (v) => -v)} />

      <section className="stk-section stk-cmp">
        <h2>Caveats on each test</h2>
        {labelRows.length === 0 ? (
          <p className="stk-label">None of these tests carries a caveat.</p>
        ) : (
          <Table
            caption="Caveats on the results, by test. Rows where the tests differ are marked."
            head={[{ label: "Caveat" }, { label: "Across the runs" }, ...labels.map((l) => ({ label: l }))]}
          >
            {labelRows.map(([key, flags]) => {
              const differs = c.differing_labels.includes(key);
              const t = labelText(key);
              return (
                <tr key={key}>
                  <td data-wrap>{t.text}</td>
                  <Verdict differs={differs} />
                  {flags.map((f, i) => (
                    <td key={labels[i]} data-differs={differs ? "" : undefined}>
                      {f ? <Chip tone={t.warn ? "warn" : undefined}>yes</Chip> : <span className="stk-label">no</span>}
                    </td>
                  ))}
                </tr>
              );
            })}
          </Table>
        )}
      </section>

      <section className="stk-section stk-cmp">
        <h2>What was set up differently</h2>
        {diffs.length === 0 ? (
          <p className="stk-label">Nothing differs: these tests share one setup.</p>
        ) : (
          <DiffTable rows={diffs} labels={labels} caption="Setup that differs between the tests" />
        )}
        {same.length > 0 && (
          <details className="stk-cmp-matched stk-gap-s">
            <summary>What matched ({same.length})</summary>
            <DiffTable rows={same} labels={labels} caption="Setup that is the same in every test" />
          </details>
        )}
      </section>
    </Shell>
  );
}
