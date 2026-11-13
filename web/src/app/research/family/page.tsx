import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { istWhenEpoch, rupees } from "@/lib/format";
import { familyIdParam, getFamily, objectiveText, pboHowText, pboNoteLine, roleWords, scoreRanks, sessionRows, unlockRows } from "@/lib/research";
import type { FamilyDetailDTO, TrialRowDTO } from "@/lib/types";
import { Chip, Empty, PageHead, Panel, Table, toneClass } from "@/components/ui";

export const metadata: Metadata = { title: "Trial family - STOCKIFY" };
export const dynamic = "force-dynamic";

function ErrorPanel({ children }: { children: ReactNode }) {
  return (
    <Panel tone="red">
      <div role="alert" className="stk-stack-s">
        {children}
      </div>
    </Panel>
  );
}

function when(ns: number | null): string {
  return ns === null ? "—" : istWhenEpoch(Math.floor(ns / 1e9));
}

/** A server sentence starts with a lower-case letter; the page capitalises it and ends it with a full stop. */
function sentence(s: string | null | undefined): string | null {
  const t = s?.trim();
  return t ? `${t[0].toUpperCase()}${t.slice(1)}${/[.!?]$/.test(t) ? "" : "."}` : null;
}

function runStatusTone(status: string): "up" | "down" | "warn" | undefined {
  // a finished run is the normal case and stays plain, so the one "picked" mark is the only green chip in a table
  if (status === "failed") return "down";
  if (status === "running" || status === "queued") return "warn";
  return undefined;
}

function RunName({ t }: { t: TrialRowDTO }) {
  const name = t.name?.trim() || t.run_id;
  return (
    <>
      <Link href={`/tests/${encodeURIComponent(t.run_id)}`}>{name}</Link>
      {t.name?.trim() && <div className="stk-label">{t.run_id}</div>}
    </>
  );
}

function Net({ paise }: { paise: number | null }) {
  const tone = paise === null || paise === 0 ? undefined : paise > 0 ? "up" : "down";
  return <td className={["num", toneClass(tone)].filter(Boolean).join(" ")}>{rupees(paise)}</td>;
}

function Tries({ fam }: { fam: FamilyDetailDTO }) {
  const ranks = scoreRanks(fam.trials.map((t) => t.objective_value));
  const measure = fam.plain.objective ? ` Score is the ${fam.plain.objective}.` : "";
  return (
    <Table
      caption={`The tries of this family. The picked try is marked. Scores are before allowing for luck, rank 1 is best.${measure}`}
      head={[{ label: "Try" }, { label: "State" }, { label: "Score before allowing for luck", num: true }, { label: "Profit after costs", num: true }, { label: "Days", num: true }]}
    >
      {fam.trials.map((t, i) => (
        <tr key={t.run_id}>
          <th scope="row" data-wrap>
            <RunName t={t} />
            {t.run_id === fam.best_run_id && (
              <div>
                <Chip tone="up">picked</Chip>
              </div>
            )}
          </th>
          <td>
            <Chip tone={runStatusTone(t.status)}>{t.status}</Chip>
          </td>
          <td className="num">
            {objectiveText(t.objective_value, fam.objective)}
            {ranks[i] !== null && <span className="stk-label"> (rank {ranks[i]})</span>}
          </td>
          <Net paise={t.net_pnl_paise} />
          <td className="num">{t.days.toLocaleString("en-IN")}</td>
        </tr>
      ))}
    </Table>
  );
}

function Others({ rows }: { rows: TrialRowDTO[] }) {
  return (
    <Table
      caption="Other runs of this family. They are not counted as tries."
      head={[{ label: "Run" }, { label: "What it is" }, { label: "State" }, { label: "Profit after costs", num: true }, { label: "Days", num: true }]}
    >
      {rows.map((t) => (
        <tr key={t.run_id}>
          <th scope="row" data-wrap>
            <RunName t={t} />
          </th>
          <td data-wrap>{roleWords(t.role)}</td>
          <td>
            <Chip tone={runStatusTone(t.status)}>{t.status}</Chip>
          </td>
          <Net paise={t.net_pnl_paise} />
          <td className="num">{t.days.toLocaleString("en-IN")}</td>
        </tr>
      ))}
    </Table>
  );
}

function Body({ fam }: { fam: FamilyDetailDTO }) {
  const unlocks = unlockRows(fam.holdout_unlocks);
  const sessions = sessionRows(fam.sessions);
  const pbo = sentence(fam.plain.pbo);
  const named = fam.description?.trim();
  const dsr = sentence(fam.plain.dsr);
  const trialsMeaning = sentence(fam.plain.trials);
  const dsrMeaning = sentence(fam.plain.dsr_meaning);
  const caveat = sentence(fam.plain.scores_caveat);
  const pboNote = pboNoteLine(fam.pbo_note, Boolean(pbo));
  return (
    <>
      <PageHead
        title={named || fam.family_id}
        lede={
          <>
            {named ? <span className="stk-label">{fam.family_id}. </span> : null}
            Every try of this idea, which one was picked, and how often the held-back days were looked at.
          </>
        }
      />
      <div className="stk-stack">
        <section className="stk-section" aria-labelledby="tries-h">
          <h2 id="tries-h">Tries</h2>
          {trialsMeaning && <p className="stk-fine">{trialsMeaning}</p>}
          {dsr && <p className="stk-gap-xs">{dsr}</p>}
          {dsr && dsrMeaning && <p className="stk-fine stk-gap-xs">{dsrMeaning}</p>}
          {fam.trials.length === 0 ? (
            <div className="stk-gap-s">
              <Empty>No finished tries yet, so there is nothing to compare.</Empty>
            </div>
          ) : (
            <div className="stk-gap-s">
              <Tries fam={fam} />
              {caveat && <p className="stk-fine stk-gap-xs">{caveat}</p>}
            </div>
          )}
        </section>

        <section className="stk-section" aria-labelledby="pbo-h">
          <h2 id="pbo-h">Chance of overfitting</h2>
          {pbo ? (
            <>
              <p>{pbo}</p>
              {pboNote && <p className="stk-fine stk-gap-xs">{pboNote}.</p>}
              {fam.pbo && (
                <details className="stk-trust-details stk-gap-xs">
                  <summary>How it was computed</summary>
                  <p className="stk-fine">{pboHowText(fam.pbo)}</p>
                </details>
              )}
            </>
          ) : (
            <p>{pboNote ? `No estimate: ${pboNote[0].toLowerCase()}${pboNote.slice(1)}.` : "No estimate for this family."}</p>
          )}
        </section>

        <section className="stk-section" aria-labelledby="holdout-h">
          <h2 id="holdout-h">Holdout looks</h2>
          {fam.plain.holdout_unlocks && <p className="stk-fine">{fam.plain.holdout_unlocks}</p>}
          <div className="stk-gap-s">
            {unlocks.length === 0 ? (
              <Empty>The held-back days have not been looked at for this family.</Empty>
            ) : (
              <Table
                caption="Each time the held-back days were unlocked for this family, oldest first"
                head={[{ label: "When (IST)" }, { label: "Run" }, { label: "Reason you gave" }]}
              >
                {unlocks.map((u, i) => (
                  <tr key={`${i}-${u.when_ns}`}>
                    <td>{when(u.when_ns)}</td>
                    <td data-wrap>{u.run_id ? <Link href={`/tests/${encodeURIComponent(u.run_id)}`}>{u.run_id}</Link> : "—"}</td>
                    <td data-wrap>{u.reason || "—"}</td>
                  </tr>
                ))}
              </Table>
            )}
          </div>
        </section>

        {fam.others.length > 0 && (
          <section className="stk-section" aria-labelledby="others-h">
            <h2 id="others-h">Other runs, not counted as tries</h2>
            <p className="stk-fine">
              Sensitivity checks, out-of-sample tests and re-runs belong to the family but are not extra tries, so they do not change the luck allowance above.
            </p>
            <div className="stk-gap-s">
              <Others rows={fam.others} />
            </div>
          </section>
        )}

        <section className="stk-section" aria-labelledby="sess-h">
          <h2 id="sess-h">Paper sessions</h2>
          {sessions.length === 0 ? (
            <Empty>No paper session runs this family.</Empty>
          ) : (
            <>
              <p className="stk-fine">Paper sessions trade live prices. They are listed apart and are not tries.</p>
              <div className="stk-gap-s">
                <Table caption="Paper sessions of this family" head={[{ label: "Session" }, { label: "State" }, { label: "Created (IST)" }]}>
                  {sessions.map((s) => (
                    <tr key={s.session_id}>
                      <th scope="row" data-wrap>
                        <Link href={`/trade/${encodeURIComponent(s.session_id)}`}>{s.name?.trim() || s.session_id}</Link>
                      </th>
                      <td>{s.status ? s.status.toLowerCase() : "—"}</td>
                      <td>{when(s.created_ns)}</td>
                    </tr>
                  ))}
                </Table>
              </div>
            </>
          )}
        </section>
      </div>
    </>
  );
}

export default async function FamilyPage({ searchParams }: { searchParams: Promise<{ id?: string | string[] }> }) {
  const id = familyIdParam((await searchParams).id);
  let fam: FamilyDetailDTO | null = null;
  let failure: string | null = null;
  if (id !== null) {
    try {
      fam = await getFamily(id);
    } catch (e) {
      failure = e instanceof ApiError ? e.message : "the API did not answer";
    }
  }

  return (
    <div className="stk-page">
      <Link href="/research" className="stk-crumb">
        Research
      </Link>
      {id === null ? (
        <ErrorPanel>
          <p>No trial family was chosen. Pick one from the research overview.</p>
        </ErrorPanel>
      ) : failure !== null ? (
        <ErrorPanel>
          <p>Couldn&apos;t load this trial family: {failure}. Check that the API is running, then try again.</p>
        </ErrorPanel>
      ) : fam === null ? (
        <Empty>There is no trial family called &ldquo;{id}&rdquo;. The research overview lists the ones that exist.</Empty>
      ) : (
        <Body fam={fam} />
      )}
    </div>
  );
}
