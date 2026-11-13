import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { ApiError } from "@/lib/api";
import {
  RANGE_CHOICES, addDays, bytesWords, cellNote, cellSymbol, cellTone, cellWords, dayLabel, durationWords, gapReasonWords,
  gapScopeWords, istToday, parseDay, rangeDays, worstCoverage,
} from "@/lib/data";
import { istWhen } from "@/lib/format";
import { getCoverage, getGaps, getQuality } from "@/lib/research";
import type { BarRangeDTO, CoverageCellDTO, CoverageDayDTO, CoverageMatrixDTO, GapPageDTO, StorageDTO } from "@/lib/types";
import { Chip, Empty, PageHead, Panel, Stat, StatGrid, Table } from "@/components/ui";
import RecorderStrip from "./RecorderStrip";

export const metadata: Metadata = { title: "Data coverage - STOCKIFY" };
export const dynamic = "force-dynamic";

type Settled<T> = { ok: true; value: T } | { ok: false; message: string };

async function settle<T>(call: Promise<T>): Promise<Settled<T>> {
  try {
    return { ok: true, value: await call };
  } catch (e) {
    return { ok: false, message: e instanceof ApiError ? e.message : "the API did not answer" };
  }
}

/** What each state means; the chips beside the words are drawn by the same function as the grid's cells. */
const STATE_LEGEND: { state: string; detail?: string; means: string }[] = [
  { state: "ok", detail: "complete", means: "recorded, archived or downloaded, and complete enough" },
  { state: "partial", detail: "partly there", means: "something is there, but not all of it" },
  { state: "missing", means: "nothing was recorded or downloaded" },
  { state: "not_reported", means: "recorded, but its quality report has not run: open the day for the command" },
  { state: "closed", means: "weekend or exchange holiday" },
];

function StateChip({ cell, datasetKey }: { cell: CoverageCellDTO; datasetKey?: string }) {
  return (
    <Chip tone={cellTone(cell.state) ?? undefined}>
      <span aria-hidden>{cellSymbol(cell.state)}</span>&nbsp;{cellWords(cell, datasetKey)}
    </Chip>
  );
}

function href(days: number, day?: string): string {
  return `/data?days=${days}${day ? `&day=${day}` : ""}`;
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

function Grid({ matrix, days, selected }: { matrix: CoverageMatrixDTO; days: number; selected?: string }) {
  const rows = [...matrix.days].reverse();
  return (
    <div className="stk-table-wrap" tabIndex={0} role="region" aria-label="Coverage by day">
      <table className="stk-table stk-grid">
        <caption className="sr-only">
          Recording, archive and NSE file coverage for each day, newest first. Choose a day to see its gaps and files.
        </caption>
        <thead>
          <tr>
            <th scope="col">Day</th>
            {matrix.datasets.map((d) => (
              <th key={d.key} scope="col">
                {d.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((day: CoverageDayDTO) => (
            <tr key={day.date} data-closed={day.trading ? undefined : ""} aria-current={day.date === selected ? "true" : undefined}>
              <th scope="row">
                <Link href={href(days, day.date)}>{dayLabel(day.date)}</Link> <span className="stk-label">{day.date.slice(0, 4)}</span>
                {day.date === selected && <span className="stk-label"> (open)</span>}
              </th>
              {matrix.datasets.map((d) => {
                const cell = day.cells[d.key];
                const note = cell ? cellNote(cell, d.key) : null;
                return (
                  <td key={d.key}>
                    {cell ? <StateChip cell={cell} datasetKey={d.key} /> : <span className="stk-label">no information</span>}
                    {note && <div className="stk-label">{note}</div>}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Legend() {
  return (
    <ul className="stk-legend" aria-label="What each state means">
      {STATE_LEGEND.map((l) => (
        <li key={l.state}>
          <StateChip cell={{ state: l.state, detail: l.detail ?? null }} />
          <span className="stk-label">{l.means}</span>
        </li>
      ))}
    </ul>
  );
}

function BarsTable({ bars }: { bars: BarRangeDTO[] }) {
  return (
    <Table
      caption="Price bars saved from Kite, per instrument and interval"
      head={[{ label: "Instrument" }, { label: "Interval" }, { label: "From" }, { label: "To" }, { label: "Days spanned", num: true }, { label: "Rows saved", num: true }]}
    >
      {bars.map((b) => (
        <tr key={`${b.instrument_id}-${b.interval}`}>
          <th scope="row">{b.symbol ?? `id ${b.instrument_id}`}</th>
          <td>{b.interval}</td>
          <td>{istWhen(b.from_ist)}</td>
          <td>{istWhen(b.to_ist)}</td>
          <td className="num">{b.days.toLocaleString("en-IN")}</td>
          <td className="num">{b.rows.toLocaleString("en-IN")}</td>
        </tr>
      ))}
    </Table>
  );
}

/** `breakdown` is false when a store could not be read: its sizes would read as 0 B, so only the disk figures are shown. */
function Storage({ s, breakdown }: { s: StorageDTO; breakdown: boolean }) {
  const rows: [string, number][] = [
    ["Tick recordings still on the landing disk", s.landing_bytes],
    ["Tick archive", s.archive_bytes],
    ["Reference data", s.refdata_bytes],
    ["Lab and price-bar databases", s.lab_bytes],
    ["NSE files downloaded (size as logged)", s.nse_bytes],
  ];
  return (
    <div className="stk-stack-s">
      {s.warnings.map((w) => (
        <Panel key={w} tone="amber">
          <p role="alert">{w}</p>
        </Panel>
      ))}
      <StatGrid cols={2}>
        <Stat label="Free on this disk" value={bytesWords(s.free_bytes)} />
        <Stat label="Disk size" value={bytesWords(s.total_bytes)} />
      </StatGrid>
      {breakdown && (
        <dl className="stk-kv stk-kv-wide">
          {rows.map(([label, n]) => (
            <div key={label} className="stk-kv-row">
              <dt>{label}</dt>
              <dd>{bytesWords(n)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

/** The server's sentences quote commands in backticks; show those parts as code. */
function withCode(text: string): ReactNode[] {
  return text.split("`").map((part, i) => (i % 2 === 1 ? <code key={i}>{part}</code> : part));
}

function num(v: number | undefined, digits: number): string {
  return typeof v === "number" && Number.isFinite(v)
    ? v.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })
    : "—";
}

function GapsBlock({ gaps }: { gaps: Settled<GapPageDTO | null> }) {
  if (!gaps.ok) {
    return (
      <ErrorPanel>
        <p>Couldn&apos;t load the gaps for this day: {gaps.message}.</p>
      </ErrorPanel>
    );
  }
  if (gaps.value === null) return <Empty>Nothing was recorded on this day, so there are no gaps to list.</Empty>;
  const g = gaps.value;
  if (g.total === 0) return <Empty>The recording had no gaps on this day.</Empty>;
  return (
    <>
      <Table
        caption="Gaps in the recording, in the order they started"
        head={[{ label: "Started (IST)" }, { label: "Length", num: true }, { label: "Why" }, { label: "What it covers" }]}
      >
        {g.gaps.map((x) => (
          <tr key={`${x.start_ns}-${x.scope}-${x.reason}`}>
            <td>{istWhen(x.start_ist)}</td>
            <td className="num">{durationWords(x.duration_s)}</td>
            <td>{gapReasonWords(x.reason)}</td>
            <td>{gapScopeWords(x.scope)}</td>
          </tr>
        ))}
      </Table>
      {g.total > g.gaps.length + g.offset && (
        <p className="stk-fine stk-gap-xs">
          Showing the first {g.gaps.length} of {g.total.toLocaleString("en-IN")} gaps.
        </p>
      )}
    </>
  );
}

function WorstBlock({ quality }: { quality: Settled<Awaited<ReturnType<typeof getQuality>>> }) {
  if (!quality.ok) {
    return (
      <ErrorPanel>
        <p>Couldn&apos;t load the quality report for this day: {quality.message}.</p>
      </ErrorPanel>
    );
  }
  if (!quality.value.report) return <Empty>{withCode(quality.value.reason ?? "There is no quality report for this day yet.")}</Empty>;
  const worst = worstCoverage(quality.value.report.instruments, 10);
  if (worst.length === 0) return <Empty>The quality report lists no instruments for this day.</Empty>;
  return (
    <Table
      caption="The ten instruments with the lowest coverage on this day"
      head={[
        { label: "Instrument" },
        { label: "Seconds with a packet (%)", num: true },
        { label: "Longest silence (s)", num: true },
        { label: "Packets", num: true },
      ]}
    >
      {worst.map((i) => (
        <tr key={i.instrument_id}>
          <th scope="row">{i.symbol ?? `id ${i.instrument_id}`}</th>
          <td className="num">{num(i.metrics.coverage_pct, 1)}</td>
          <td className="num">{num(i.metrics.longest_gap_s, 0)}</td>
          <td className="num">{num(i.metrics.packets, 0)}</td>
        </tr>
      ))}
    </Table>
  );
}

async function DayDetail({ day, days, matrix, hideNse }: { day: string; days: number; matrix: CoverageMatrixDTO; hideNse: boolean }) {
  const inRange = matrix.days.find((d) => d.date === day);
  const [own, gaps, quality] = await Promise.all([
    inRange || hideNse
      ? Promise.resolve<Settled<CoverageDayDTO | undefined>>({ ok: true, value: inRange })
      : settle(getCoverage(day, day).then((m) => m.days[0])),
    settle(getGaps(day)),
    settle(getQuality(day)),
  ]);
  const cells = own.ok && own.value ? own.value.cells : null;
  const files = matrix.datasets.filter((d) => d.key.startsWith("nse_"));
  return (
    <section className="stk-section stk-daydetail" aria-labelledby="day-h">
      <div className="stk-row-between">
        <h2 id="day-h">
          {dayLabel(day)} {day.slice(0, 4)}
        </h2>
        <Link className="stk-btn" href={href(days)}>
          Back to all days
        </Link>
      </div>

      <h3 className="stk-gap-top">Gaps in the recording</h3>
      <GapsBlock gaps={gaps} />

      <h3 className="stk-gap-top">Instruments with the least coverage</h3>
      <WorstBlock quality={quality} />

      <h3 className="stk-gap-top">NSE files for this day</h3>
      {hideNse ? (
        <ErrorPanel>
          <p>The NSE file status is not shown, because it comes from a store that could not be read (see above).</p>
        </ErrorPanel>
      ) : !cells || files.length === 0 ? (
        <ErrorPanel>
          <p>Couldn&apos;t load the NSE file status for this day.</p>
        </ErrorPanel>
      ) : (
        <Table caption={`NSE files for ${day}`} head={[{ label: "File" }, { label: "State" }, { label: "Detail" }]}>
          {files.map((f) => {
            const c = cells[f.key];
            return (
              <tr key={f.key}>
                <th scope="row">{f.label}</th>
                <td>{c ? <StateChip cell={c} datasetKey={f.key} /> : "no information"}</td>
                <td>{c?.detail ?? "—"}</td>
              </tr>
            );
          })}
        </Table>
      )}
      {!hideNse && cells && files.some((f) => cells[f.key]?.state === "missing") && (
        <p className="stk-fine stk-gap-xs">
          To download what is missing, run <code>python -m stockify nse fetch --date {day}</code>.
        </p>
      )}
    </section>
  );
}

function nothingStored(m: CoverageMatrixDTO): boolean {
  return (
    m.bars.length === 0 &&
    !m.recorder &&
    m.days.every((d) => Object.values(d.cells).every((c) => c.state === "missing" || c.state === "closed" || c.state === "not_applicable"))
  );
}

export default async function DataPage({ searchParams }: { searchParams: Promise<{ days?: string; day?: string }> }) {
  const sp = await searchParams;
  const days = rangeDays(sp.days);
  const day = parseDay(sp.day) ?? undefined;
  const badDay = sp.day !== undefined && sp.day !== "" && !day;
  const today = istToday(new Date());
  const cov = await settle(getCoverage(addDays(today, -(days - 1)), today));
  const matrix = cov.ok ? cov.value : null;
  const stores = matrix?.errors ?? [];
  const badStores = matrix?.error_stores ?? [];
  const noDays = badStores.includes("refdata");
  const noBars = badStores.includes("bars");
  const hidden = [noDays && "the day grid and the NSE file status", noBars && "the price bar list"].filter(Boolean).join(" and ");

  return (
    <div className="stk-page">
      <PageHead
        title="Data coverage"
        lede="What STOCKIFY has recorded, archived and downloaded, day by day, and what is missing."
        actions={
          <nav className="stk-row" aria-label="How many days to show">
            {RANGE_CHOICES.map((n) => (
              <Link key={n} className="stk-btn" href={href(n, day)} aria-current={n === days ? "true" : undefined}>
                Last {n} days
              </Link>
            ))}
          </nav>
        }
      />

      <div className="stk-stack">
        {!matrix ? (
          <ErrorPanel>
            <p>Couldn&apos;t load the data coverage{cov.ok ? "" : `: ${cov.message}`}.</p>
            <p>
              <Link className="stk-link" href={href(days, day)}>
                Try again
              </Link>
            </p>
          </ErrorPanel>
        ) : (
          <>
            <RecorderStrip initial={matrix.recorder ?? null} />

            {badDay && (
              <Panel tone="info">
                <p>&ldquo;{sp.day}&rdquo; is not a date. Showing all days instead; choose a day from the grid.</p>
              </Panel>
            )}

            {stores.length > 0 && (
              <ErrorPanel>
                {stores.map((s) => (
                  <p key={s}>{s}</p>
                ))}
                <p className="stk-fine">
                  Check that the lab data folder can be read and that nothing else is locking it, then reload this page.
                  {hidden && ` ${hidden[0].toUpperCase()}${hidden.slice(1)} ${noDays ? "are" : "is"} hidden until the data can be read.`}
                </p>
              </ErrorPanel>
            )}

            {day && <DayDetail day={day} days={days} matrix={matrix} hideNse={noDays} />}

            {!noDays && (
              <section className="stk-section">
                <h2>Day by day</h2>
                {stores.length === 0 && nothingStored(matrix) ? (
                  <Empty>
                    Nothing has been recorded or downloaded in this range yet. Start the recorder with <code>.\run.ps1 -DaemonOnly</code>, or download
                    NSE files with <code>python -m stockify nse fetch --date YYYY-MM-DD</code>.
                  </Empty>
                ) : (
                  <div className="stk-stack-s">
                    <Legend />
                    <p className="stk-fine">Choose a day to see its gaps, its least-covered instruments and its NSE files.</p>
                    <Grid matrix={matrix} days={days} selected={day} />
                  </div>
                )}
              </section>
            )}

            {!noBars && (
              <section className="stk-section">
                <h2>Price bars</h2>
                {matrix.bars.length === 0 ? <Empty>No price bars are saved yet.</Empty> : <BarsTable bars={matrix.bars} />}
              </section>
            )}

            <section className="stk-section">
              <h2>Storage</h2>
              <Storage s={matrix.storage} breakdown={!noDays} />
            </section>
          </>
        )}
      </div>
    </div>
  );
}
