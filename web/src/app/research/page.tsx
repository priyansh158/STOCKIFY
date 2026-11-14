import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { istWhenEpoch } from "@/lib/format";
import { runIdTail } from "@/lib/runid";
import { listStrategies, listTests } from "@/lib/tests";
import { familyHref, getFamilies, getJobs, getResearchCounts, jobHref, listCapNote } from "@/lib/research";
import type { FamilySummaryDTO, ResearchJobDTO } from "@/lib/types";
import { Chip, Empty, PageHead, Panel, Table } from "@/components/ui";
import NewSweep from "./NewSweep";

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

function CapNote({ note }: { note: string | null }) {
  return note ? <p className="stk-fine stk-gap-xs">{note}</p> : null;
}

function plural(n: number, one: string, many: string): string {
  return `${n.toLocaleString("en-IN")} ${n === 1 ? one : many}`;
}

function Families({ rows }: { rows: FamilySummaryDTO[] }) {
  return (
    <Table
      caption="Trial families: every try of one idea, the best try's chance of beating luck, and how many times the held-back days were looked at (holdout looks)"
      head={[{ label: "Family" }, { label: "Tries", num: true }, { label: "Best try, after allowing for luck" }, { label: "Holdout looks", num: true }]}
    >
      {rows.map((f) => (
        <tr key={f.family_id}>
          <th scope="row" data-wrap>
            <Link href={familyHref(f.family_id)} title={f.family_id}>{f.title || f.description?.trim() || f.family_id}</Link>
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
  if (status === "failed" || status === "interrupted") return "down";
  if (status === "running") return "warn";
  return undefined;
}

function Jobs({ caption, rows }: { caption: string; rows: ResearchJobDTO[] }) {
  return (
    <Table caption={caption} head={[{ label: "Name" }, { label: "State" }, { label: "Started (IST)" }]}>
      {rows.map((j) => {
        const href = jobHref(j.kind, j.job_id);
        const name = j.name_label || j.name || j.job_id;
        return (
          <tr key={j.job_id}>
            <th scope="row" data-wrap>
              {href ? <Link href={href} title={j.name ? `Saved as ${j.name}` : undefined}>{name}</Link> : name}
              {j.name && <div className="stk-label">Job {runIdTail(j.job_id, j.name)}</div>}
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
  const [fams, jobs, counts, tests, strategies] = await Promise.all([
    settle(getFamilies()), settle(getJobs()), settle(getResearchCounts()), settle(listTests(200)), settle(listStrategies()),
  ]);
  const finished = tests.ok ? tests.value.filter((t) => t.status === "done") : [];
  const total = counts.ok ? counts.value : null;

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
              No trial families yet. Start a parameter sweep under &ldquo;Parameter sweeps&rdquo; below and its tries will be counted here.
            </Empty>
          ) : (
            <>
              <Families rows={fams.value} />
              <CapNote note={listCapNote(fams.value.length, total?.families ?? null, "families", "first")} />
            </>
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
              {tests.ok && strategies.ok ? (
                <NewSweep runs={finished} strategies={strategies.value} />
              ) : (
                <ErrorPanel>
                  <p>Couldn&apos;t load what a new sweep needs: {tests.ok ? (strategies.ok ? "" : strategies.message) : tests.message}. Reload the page to try again.</p>
                </ErrorPanel>
              )}
              {jobs.value.sweeps.length === 0 ? (
                <p className="stk-fine stk-gap-top">No sweeps yet. The ones you run will be listed here.</p>
              ) : (
                <div className="stk-gap-top">
                  <Jobs caption="Parameter sweeps, newest first" rows={jobs.value.sweeps} />
                  <CapNote note={listCapNote(jobs.value.sweeps.length, total?.sweeps ?? null, "sweeps", "latest")} />
                </div>
              )}
            </section>

            <section className="stk-section" aria-labelledby="wf-h">
              <h2 id="wf-h">Walk-forward runs</h2>
              {jobs.value.walkforwards.length === 0 ? (
                <Empty>
                  No walk-forward runs yet.{" "}
                  {jobs.value.sweeps.some((s) => s.status === "done")
                    ? "Open a finished sweep above and choose “Walk forward”."
                    : "Run a sweep first; when it has finished, open it and choose “Walk forward”."}
                </Empty>
              ) : (
                <>
                  <Jobs caption="Walk-forward runs, newest first" rows={jobs.value.walkforwards} />
                  <CapNote note={listCapNote(jobs.value.walkforwards.length, total?.walkforwards ?? null, "walk-forward runs", "latest")} />
                </>
              )}
            </section>
          </>
        )}
      </div>
    </div>
  );
}
