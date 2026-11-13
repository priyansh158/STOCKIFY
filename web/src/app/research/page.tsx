import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { istWhenEpoch } from "@/lib/format";
import { familyHref, getFamilies, getJobs, jobHref } from "@/lib/research";
import type { FamilySummaryDTO, ResearchJobDTO } from "@/lib/types";
import { Chip, Empty, PageHead, Panel, Table } from "@/components/ui";

export const metadata: Metadata = { title: "Research - STOCKIFY" };
export const dynamic = "force-dynamic";

type Settled<T> = { ok: true; value: T } | { ok: false; message: string };

async function settle<T>(call: Promise<T>): Promise<Settled<T>> {
  try {
    return { ok: true, value: await call };
  } catch (e) {
    return { ok: false, message: e instanceof ApiError ? e.message : "the API did not answer" };
  }
}

function ErrorPanel({ children }: { children: ReactNode }) {
  return (
    <Panel tone="red">
      <div role="alert" className="stk-stack-s">
        {children}
      </div>
    </Panel>
  );
}

function plural(n: number, one: string, many: string): string {
  return `${n.toLocaleString("en-IN")} ${n === 1 ? one : many}`;
}

function Families({ rows }: { rows: FamilySummaryDTO[] }) {
  return (
    <Table
      caption="Trial families: every try of one idea, with the best try's chance of beating luck"
      head={[{ label: "Family" }, { label: "Tries", num: true }, { label: "Best try, after allowing for luck" }, { label: "Times the held-back days were opened", num: true }]}
    >
      {rows.map((f) => (
        <tr key={f.family_id}>
          <th scope="row" data-wrap>
            <Link href={familyHref(f.family_id)}>{f.description?.trim() || f.family_id}</Link>
            {f.description?.trim() && <div className="stk-label">{f.family_id}</div>}
            {f.n_other_runs > 0 && <div className="stk-label">and {plural(f.n_other_runs, "other run", "other runs")}</div>}
          </th>
          <td className="num">{f.n_trials.toLocaleString("en-IN")}</td>
          <td data-wrap>{f.best_plain || "—"}</td>
          <td className="num">{f.unlocks.toLocaleString("en-IN")}</td>
        </tr>
      ))}
    </Table>
  );
}

function statusTone(status: string): "up" | "down" | "warn" | undefined {
  if (status === "done") return "up";
  if (status === "failed") return "down";
  if (status === "running") return "warn";
  return undefined;
}

function Jobs({ caption, rows }: { caption: string; rows: ResearchJobDTO[] }) {
  return (
    <Table caption={caption} head={[{ label: "Name" }, { label: "State" }, { label: "Started (IST)" }]}>
      {rows.map((j) => {
        const href = jobHref(j.kind, j.job_id);
        const name = j.name?.trim() || j.job_id;
        return (
          <tr key={j.job_id}>
            <th scope="row" data-wrap>
              {href ? <Link href={href}>{name}</Link> : name}
            </th>
            <td data-wrap>
              <Chip tone={statusTone(j.status)}>{j.plain}</Chip>
            </td>
            <td>{istWhenEpoch(Math.floor(j.created_ns / 1e9))}</td>
          </tr>
        );
      })}
    </Table>
  );
}

export default async function ResearchPage() {
  const [fams, jobs] = await Promise.all([settle(getFamilies()), settle(getJobs())]);

  return (
    <div className="stk-page">
      <PageHead
        title="Research"
        lede="Every idea you have tried, how often, and whether the best try beats luck."
      />
      <div className="stk-stack">
        <section className="stk-section" aria-labelledby="fam-h">
          <h2 id="fam-h">Trial families</h2>
          {!fams.ok ? (
            <ErrorPanel>
              <p>Couldn&apos;t load the trial families: {fams.message}. Check that the API is running, then try again.</p>
              <p>
                <Link className="stk-link" href="/research">
                  Try again
                </Link>
              </p>
            </ErrorPanel>
          ) : fams.value.length === 0 ? (
            <Empty>
              No trial families yet. Run a parameter sweep with <code>python -m stockify sweep --config sweep.yaml</code> and its tries
              will be counted here.
            </Empty>
          ) : (
            <Families rows={fams.value} />
          )}
        </section>

        {!jobs.ok ? (
          <section className="stk-section" aria-labelledby="jobs-h">
            <h2 id="jobs-h">Parameter sweeps and walk-forward runs</h2>
            <ErrorPanel>
              <p>Couldn&apos;t load the sweeps and walk-forward runs: {jobs.message}.</p>
              <p>
                <Link className="stk-link" href="/research">
                  Try again
                </Link>
              </p>
            </ErrorPanel>
          </section>
        ) : (
          <>
            <section className="stk-section" aria-labelledby="sweep-h">
              <h2 id="sweep-h">Parameter sweeps</h2>
              {jobs.value.sweeps.length === 0 ? (
                <Empty>
                  No sweeps yet. Start one with <code>python -m stockify sweep --config sweep.yaml</code>.
                </Empty>
              ) : (
                <Jobs caption="Parameter sweeps, newest first" rows={jobs.value.sweeps} />
              )}
            </section>

            <section className="stk-section" aria-labelledby="wf-h">
              <h2 id="wf-h">Walk-forward runs</h2>
              {jobs.value.walkforwards.length === 0 ? (
                <Empty>
                  No walk-forward runs yet. Start one with <code>python -m stockify walkforward --sweep sweep.yaml</code>.
                </Empty>
              ) : (
                <Jobs caption="Walk-forward runs, newest first" rows={jobs.value.walkforwards} />
              )}
            </section>
          </>
        )}
      </div>
    </div>
  );
}
