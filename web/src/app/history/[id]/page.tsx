import Link from "next/link";
import { notFound } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { HistoryRecord } from "@/lib/types";
import {
  OverviewView,
  FinancialsView,
  GeoView,
  TechnicalView,
  SummaryView,
} from "@/components/stage-views";

export const dynamic = "force-dynamic";

interface Props {
  params: Promise<{ id: string }>;
}

async function fetchRecord(id: number): Promise<HistoryRecord | null> {
  try {
    return await api<HistoryRecord>(`/api/history/${id}`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

export default async function HistoryDetailPage({ params }: Props) {
  const { id: idStr } = await params;
  const id = Number(idStr);
  if (!Number.isFinite(id)) notFound();

  const record = await fetchRecord(id);
  if (!record) notFound();

  return (
    <div className="mx-auto max-w-3xl px-3 sm:px-5 py-6 sm:py-10">
      <div className="flex items-center justify-between mb-4">
        <Link
          href="/history"
          className="stk-mono-tight"
          style={{ color: "var(--color-fg-muted)" }}
        >
          ← History
        </Link>
        <Link
          href={`/analyze/${encodeURIComponent(record.ticker)}`}
          className="stk-mono-tight rounded-md border px-3 py-1.5 text-xs"
          style={{
            borderColor: "var(--color-green)",
            background: "rgba(0,255,65,0.10)",
            color: "var(--color-green)",
          }}
        >
          ↻ Re-analyze {record.ticker}
        </Link>
      </div>

      <div
        className="stk-mono-tight mb-6"
        style={{ color: "var(--color-fg-muted)" }}
      >
        Stored analysis #{record.id} · {new Date(record.created_at * 1000).toLocaleString()}
      </div>

      {/* Stage 5 first — the headline is what users want when replaying. */}
      <SummaryView
        result={{
          summary: record.summary,
          verdict: record.verdict,
          verdict_reason: record.verdict_reason ?? "",
          bottom_line: record.bottom_line ?? "",
        }}
        company={record.company_name}
      />

      <hr className="my-8" style={{ borderColor: "rgba(0,255,65,0.15)" }} />

      <section className="mb-10">
        <OverviewView result={record.profile} ticker={record.ticker} />
      </section>
      <section className="mb-10">
        <FinancialsView result={record.financials} company={record.company_name} />
      </section>
      <section className="mb-10">
        <GeoView result={record.geo} company={record.company_name} />
      </section>
      <section className="mb-10">
        <TechnicalView result={record.technical} company={record.company_name} />
      </section>
    </div>
  );
}
