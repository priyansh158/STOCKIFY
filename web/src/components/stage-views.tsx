"use client";

/**
 * Per-stage renderers — small composable bits of HTML that turn the parsed
 * JSON dict from each stage into a readable section. Mirror the Streamlit
 * `render_overview_page` / `render_financials_page` / etc. structure, minus
 * the Matrix CSS hacks (we use Tailwind + token CSS vars).
 */

import { asArray, asDict, asInt, asStr } from "@/lib/json-helpers";

// ── Reusable primitives ───────────────────────────────────────────────────────

export function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3
      className="stk-mono-tight mb-2 mt-6 first:mt-0"
      style={{ color: "var(--color-green)" }}
    >
      {children}
    </h3>
  );
}

export function Body({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="leading-relaxed"
      style={{ color: "var(--color-fg)", fontSize: "0.98rem" }}
    >
      {children}
    </div>
  );
}

export function Chip({
  children,
  tone = "green",
}: {
  children: React.ReactNode;
  tone?: "green" | "red" | "amber" | "blue";
}) {
  const colorMap: Record<string, [string, string, string]> = {
    green: ["rgba(0,255,65,0.5)", "rgba(0,255,65,0.06)", "var(--color-green-soft)"],
    red:   ["rgba(255,68,68,0.55)", "rgba(255,68,68,0.08)", "var(--color-red-soft)"],
    amber: ["rgba(255,170,64,0.55)", "rgba(255,170,64,0.08)", "var(--color-amber)"],
    blue:  ["rgba(79,176,255,0.55)", "rgba(79,176,255,0.08)", "var(--color-blue)"],
  };
  const [border, bg, fg] = colorMap[tone];
  return (
    <span
      className="stk-mono-tight inline-block rounded px-2 py-1 mr-2 mb-2"
      style={{ border: `1px solid ${border}`, background: bg, color: fg, fontSize: "0.7rem" }}
    >
      {children}
    </span>
  );
}

export function KV({ rows }: { rows: Array<[string, React.ReactNode]> }) {
  return (
    <dl className="divide-y" style={{ borderColor: "rgba(0,255,65,0.12)" }}>
      {rows.map(([k, v]) => (
        <div
          key={k}
          className="grid grid-cols-[minmax(140px,1fr)_2fr] gap-4 py-2 border-t first:border-t-0"
          style={{ borderColor: "rgba(0,255,65,0.12)" }}
        >
          <dt
            className="stk-mono-tight"
            style={{ color: "var(--color-fg-muted)" }}
          >
            {k}
          </dt>
          <dd style={{ color: "var(--color-fg)" }}>{v ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function BulletList({ items }: { items: unknown }) {
  const arr = asArray<unknown>(items);
  if (!arr.length) {
    return (
      <p className="italic" style={{ color: "var(--color-fg-muted)" }}>
        No data available.
      </p>
    );
  }
  return (
    <ul className="ml-5 list-disc space-y-1">
      {arr.map((x, i) => (
        <li key={i} style={{ color: "var(--color-fg)" }}>
          {String(x)}
        </li>
      ))}
    </ul>
  );
}

export function Hero({
  title,
  subtitle,
  summary,
  chips,
}: {
  title: string;
  subtitle: string;
  summary?: string;
  chips?: React.ReactNode;
}) {
  return (
    <header
      className="mb-6 border-b pb-6"
      style={{ borderColor: "rgba(0,255,65,0.18)" }}
    >
      <h1
        className="stk-mono text-2xl sm:text-3xl font-bold"
        style={{
          color: "var(--color-green)",
          textShadow: "0 0 14px rgba(0,255,65,0.25)",
        }}
      >
        {title}
      </h1>
      <div className="stk-mono-tight mt-1" style={{ color: "var(--color-fg-muted)" }}>
        {subtitle}
      </div>
      {chips && <div className="mt-3">{chips}</div>}
      {summary && (
        <p
          className="mt-4 leading-relaxed"
          style={{ color: "var(--color-fg)", fontSize: "1rem" }}
        >
          {summary}
        </p>
      )}
    </header>
  );
}

// ── Stage 1: Overview ─────────────────────────────────────────────────────────

export function OverviewView({ result, ticker }: { result: Record<string, unknown>; ticker: string }) {
  const company = asStr(result.company_name) || ticker;
  const sector = asStr(result.sector);
  const business = asStr(result.business_model);

  return (
    <article>
      <Hero
        title={company}
        subtitle="Company intelligence profile"
        summary={business}
        chips={
          <>
            {sector && <Chip>{sector}</Chip>}
            {ticker && <Chip tone="blue">{ticker}</Chip>}
          </>
        }
      />
      {asArray(result.key_products).length > 0 && (
        <section>
          <SectionTitle>Key products &amp; services</SectionTitle>
          <BulletList items={result.key_products} />
        </section>
      )}
      {asArray(result.key_markets).length > 0 && (
        <section>
          <SectionTitle>Key markets</SectionTitle>
          <BulletList items={result.key_markets} />
        </section>
      )}
      {asStr(result.leadership) && (
        <section>
          <SectionTitle>Leadership</SectionTitle>
          <Body>{asStr(result.leadership)}</Body>
        </section>
      )}
      {asArray(result.what_moves_the_stock).length > 0 && (
        <section>
          <SectionTitle>What moves this stock</SectionTitle>
          <BulletList items={result.what_moves_the_stock} />
        </section>
      )}
      {asArray(result.supply_chain).length > 0 && (
        <section>
          <SectionTitle>Supply chain dependencies</SectionTitle>
          <BulletList items={result.supply_chain} />
        </section>
      )}
      {asArray(result.geopolitical_exposures).length > 0 && (
        <section>
          <SectionTitle>Geopolitical exposures</SectionTitle>
          <BulletList items={result.geopolitical_exposures} />
        </section>
      )}
      {asArray(result.risk_keywords).length > 0 && (
        <section>
          <SectionTitle>Risk monitoring keywords</SectionTitle>
          <div>
            {asArray<string>(result.risk_keywords).map((k, i) => (
              <Chip key={i} tone="amber">{k}</Chip>
            ))}
          </div>
        </section>
      )}
    </article>
  );
}

// ── Stage 2: Financials ───────────────────────────────────────────────────────

export function FinancialsView({ result, company }: { result: Record<string, unknown>; company: string }) {
  const score = asInt(result.score);
  const summary = asStr(result.summary);
  const source = asStr(result.source_tag);
  const tone = score >= 60 ? "green" : score >= 40 ? "amber" : "red";
  const km = asDict(result.key_metrics);
  const rev = asDict(result.revenue_trend);
  const cf  = asDict(result.cash_flow);
  const deep = asDict(result.deep_analysis);

  return (
    <article>
      <Hero
        title={`${company} · Financials`}
        subtitle="Stage 1 — Ind-AS / SEBI scoring"
        summary={summary}
        chips={
          <>
            <Chip tone={tone}>Score {score}/100</Chip>
            {asStr(rev.status) && <Chip>Revenue {asStr(rev.status)}</Chip>}
            {asStr(cf.status) && <Chip>Cash flow {asStr(cf.status)}</Chip>}
            {source && <Chip>Source: {source}</Chip>}
          </>
        }
      />

      {Object.keys(km).length > 0 && (
        <section>
          <SectionTitle>Key metrics</SectionTitle>
          <KV
            rows={[
              ["Revenue TTM",         asStr(km.revenue_ttm) || "—"],
              ["Net income TTM",      asStr(km.net_income_ttm) || "—"],
              ["EPS TTM",             asStr(km.eps_ttm) || "—"],
              ["Gross margin",        asStr(km.gross_margin_latest) || "—"],
              ["Operating margin",    asStr(km.operating_margin_latest) || "—"],
              ["Debt-to-equity",      asStr(km.debt_to_equity) || "—"],
              ["Current ratio",       asStr(km.current_ratio) || "—"],
              ["Operating CF (TTM)",  asStr(km.operating_cash_flow_ttm) || "—"],
              ["Free CF (TTM)",       asStr(km.free_cash_flow_ttm) || "—"],
            ]}
          />
        </section>
      )}

      {(asArray(result.green_flags).length > 0 || asArray(result.red_flags).length > 0) && (
        <section>
          <SectionTitle>Flags</SectionTitle>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <div className="stk-mono-tight mb-2" style={{ color: "var(--color-green)" }}>
                Strengths
              </div>
              {asArray<string>(result.green_flags).map((f, i) => (
                <div
                  key={i}
                  className="border-l-2 pl-3 py-1 my-1"
                  style={{ borderColor: "var(--color-green)", color: "var(--color-fg)" }}
                >
                  {f}
                </div>
              ))}
              {asArray(result.green_flags).length === 0 && (
                <p style={{ color: "var(--color-fg-muted)" }}>None noted.</p>
              )}
            </div>
            <div>
              <div className="stk-mono-tight mb-2" style={{ color: "var(--color-red-soft)" }}>
                Concerns
              </div>
              {asArray<string>(result.red_flags).map((f, i) => (
                <div
                  key={i}
                  className="border-l-2 pl-3 py-1 my-1"
                  style={{ borderColor: "var(--color-red)", color: "#ffdada" }}
                >
                  {f}
                </div>
              ))}
              {asArray(result.red_flags).length === 0 && (
                <p style={{ color: "var(--color-fg-muted)" }}>None noted.</p>
              )}
            </div>
          </div>
        </section>
      )}

      {Object.keys(deep).length > 0 && (
        <section>
          <SectionTitle>Deep analysis</SectionTitle>
          {[
            ["Revenue",       deep.revenue_section],
            ["Profitability", deep.profitability_section],
            ["Balance sheet", deep.balance_sheet_section],
            ["Cash flow",     deep.cash_flow_section],
            ["Risks",         deep.risks_section],
            ["Outlook",       deep.outlook_section],
          ]
            .filter(([, v]) => asStr(v))
            .map(([title, body]) => (
              <div key={String(title)} className="mb-4">
                <h4
                  className="stk-mono-tight mb-1"
                  style={{ color: "var(--color-green)" }}
                >
                  {title as string}
                </h4>
                <Body>{asStr(body)}</Body>
              </div>
            ))}
        </section>
      )}

      {asStr(result.recommendation) && (
        <section>
          <SectionTitle>Recommendation</SectionTitle>
          <Body>{asStr(result.recommendation)}</Body>
        </section>
      )}
    </article>
  );
}

// ── Stage 3: Geo ──────────────────────────────────────────────────────────────

export function GeoView({ result, company }: { result: Record<string, unknown>; company: string }) {
  const score = asInt(result.risk_score);
  const level = asStr(result.risk_level).toUpperCase();
  const summary = asStr(result.summary);
  const tone =
    level === "LOW" ? "green" :
    level === "MEDIUM" ? "amber" :
    level === "HIGH" || level === "CRITICAL" ? "red" : "green";

  return (
    <article>
      <Hero
        title={`${company} · Geopolitical Risk`}
        subtitle="Stage 2 — collateral macro & news risk"
        summary={summary}
        chips={
          <>
            <Chip tone={tone}>Risk {score}/100</Chip>
            {level && <Chip tone={tone}>{level}</Chip>}
          </>
        }
      />

      {asArray(result.collateral_risks).length > 0 && (
        <section>
          <SectionTitle>Collateral risks</SectionTitle>
          <BulletList items={result.collateral_risks} />
        </section>
      )}
      {asArray(result.safe_signals).length > 0 && (
        <section>
          <SectionTitle>Safe signals</SectionTitle>
          <BulletList items={result.safe_signals} />
        </section>
      )}

      {(asStr(result.sector_exposure) ||
        asStr(result.geographic_exposure) ||
        asStr(result.supply_chain_risk) ||
        asStr(result.regulatory_risk) ||
        asStr(result.consumer_sentiment_risk)) && (
        <section>
          <SectionTitle>Exposure breakdown</SectionTitle>
          <KV
            rows={[
              ["Sector exposure",     asStr(result.sector_exposure)],
              ["Geographic exposure", asStr(result.geographic_exposure)],
              ["Supply chain",        asStr(result.supply_chain_risk)],
              ["Regulatory",          asStr(result.regulatory_risk)],
              ["Consumer sentiment",  asStr(result.consumer_sentiment_risk)],
            ].filter(([, v]) => v) as Array<[string, React.ReactNode]>}
          />
        </section>
      )}

      {asArray(result.relevant_articles).length > 0 && (
        <section>
          <SectionTitle>Relevant articles</SectionTitle>
          <div className="flex flex-col gap-3">
            {asArray<Record<string, unknown>>(result.relevant_articles).map((a, i) => (
              <div
                key={i}
                className="rounded-lg border p-4"
                style={{
                  borderColor: "rgba(0,255,65,0.18)",
                  background: "rgba(255,255,255,0.025)",
                }}
              >
                <div
                  className="stk-mono mb-1"
                  style={{ color: "var(--color-green)" }}
                >
                  {asStr(a.title) || "Untitled"}
                </div>
                <div
                  className="stk-mono-tight mb-2"
                  style={{ color: "var(--color-fg-muted)" }}
                >
                  {asStr(a.source)}
                </div>
                <div className="mb-2">
                  {asStr(a.impact) && (
                    <Chip
                      tone={
                        asStr(a.impact).toUpperCase() === "NEGATIVE" ? "red" :
                        asStr(a.impact).toUpperCase() === "POSITIVE" ? "green" : "blue"
                      }
                    >
                      {asStr(a.impact)}
                    </Chip>
                  )}
                  {asStr(a.relevance) && (
                    <Chip>{asStr(a.relevance)} relevance</Chip>
                  )}
                </div>
                {asStr(a.explanation) && (
                  <Body>{asStr(a.explanation)}</Body>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {asStr(result.recommendation) && (
        <section>
          <SectionTitle>Recommendation</SectionTitle>
          <Body>{asStr(result.recommendation)}</Body>
        </section>
      )}
    </article>
  );
}

// ── Stage 4: Technical ────────────────────────────────────────────────────────

export function TechnicalView({ result, company }: { result: Record<string, unknown>; company: string }) {
  const rec = asStr(result.recommendation).toUpperCase();
  const conf = asInt(result.confidence);
  const trend = asStr(result.trend);
  const price = asStr(result.current_price);
  const summary = asStr(result.summary);
  const recTone = rec === "AVOID" ? "red" : "green";

  return (
    <article>
      <Hero
        title={`${company} · Technical Analysis`}
        subtitle="Stage 3 — candlestick classification"
        summary={summary}
        chips={
          <>
            <Chip tone={recTone}>{rec.replace(/_/g, " ")}</Chip>
            <Chip>{conf}% confidence</Chip>
            {trend && <Chip>{trend}</Chip>}
            {price && <Chip tone="blue">₹{price}</Chip>}
          </>
        }
      />

      {(asStr(result.entry_point) ||
        asStr(result.target_price) ||
        asStr(result.stop_loss) ||
        asStr(result.risk_reward_ratio)) && (
        <section>
          <SectionTitle>Trade plan</SectionTitle>
          <KV
            rows={[
              ["Entry",       asStr(result.entry_point)],
              ["Target",      asStr(result.target_price)],
              ["Stop loss",   asStr(result.stop_loss)],
              ["Risk/Reward", asStr(result.risk_reward_ratio)],
            ].filter(([, v]) => v) as Array<[string, React.ReactNode]>}
          />
        </section>
      )}

      {asArray(result.patterns_found).length > 0 && (
        <section>
          <SectionTitle>Patterns found</SectionTitle>
          <BulletList items={result.patterns_found} />
        </section>
      )}

      {(asArray(result.support_levels).length > 0 || asArray(result.resistance_levels).length > 0) && (
        <section>
          <SectionTitle>Levels</SectionTitle>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <div className="stk-mono-tight mb-2" style={{ color: "var(--color-green)" }}>
                Support
              </div>
              <BulletList items={result.support_levels} />
            </div>
            <div>
              <div className="stk-mono-tight mb-2" style={{ color: "var(--color-red-soft)" }}>
                Resistance
              </div>
              <BulletList items={result.resistance_levels} />
            </div>
          </div>
        </section>
      )}

      {asStr(result.volume_analysis) && (
        <section>
          <SectionTitle>Volume analysis</SectionTitle>
          <Body>{asStr(result.volume_analysis)}</Body>
        </section>
      )}
      {asStr(result.momentum) && (
        <section>
          <SectionTitle>Momentum</SectionTitle>
          <Body>{asStr(result.momentum)}</Body>
        </section>
      )}
      {asArray(result.trading_notes).length > 0 && (
        <section>
          <SectionTitle>Trading notes</SectionTitle>
          <BulletList items={result.trading_notes} />
        </section>
      )}
    </article>
  );
}

// ── Stage 5: Summary ──────────────────────────────────────────────────────────

export function SummaryView({
  result,
  company,
}: {
  result: Record<string, unknown>;
  company: string;
}) {
  const verdict = asStr(result.verdict) || "—";
  const reason = asStr(result.verdict_reason);
  const bottom = asStr(result.bottom_line);
  const summary = asDict(result.summary);
  const fin = asDict(summary.financial);
  const geo = asDict(summary.geo);
  const tech = asDict(summary.technical);

  const verdictTone: "green" | "amber" | "red" =
    verdict.toUpperCase().includes("AVOID")
      ? "red"
      : verdict.toUpperCase().includes("REVIEW") || verdict.toUpperCase().includes("WATCH")
      ? "amber"
      : "green";

  return (
    <article>
      <Hero
        title={`${company} · Recommendation`}
        subtitle="Stage 4 — synthesized verdict"
        chips={<Chip tone={verdictTone}>{verdict}</Chip>}
      />

      {reason && (
        <section>
          <SectionTitle>Why</SectionTitle>
          <Body>{reason}</Body>
        </section>
      )}

      {bottom && (
        <section>
          <SectionTitle>Bottom line</SectionTitle>
          <div
            className="rounded-md border-l-4 px-5 py-4"
            style={{
              borderLeftColor: "var(--color-green)",
              background: "rgba(0,255,65,0.04)",
              fontFamily: "Georgia, serif",
              fontStyle: "italic",
              color: "var(--color-fg)",
              fontSize: "1.05rem",
              lineHeight: 1.6,
            }}
          >
            {bottom}
          </div>
        </section>
      )}

      <section>
        <SectionTitle>Scorecard</SectionTitle>
        <KV
          rows={[
            ["Financial score",      `${asInt(fin.score)}/100 (${fin.passed ? "PASS" : "FAIL"})`],
            ["Geopolitical risk",    `${asInt(geo.risk_score)}/100 ${asStr(geo.risk_level) ? `(${asStr(geo.risk_level)})` : ""}`],
            ["Technical recommendation", `${asStr(tech.recommendation)} · ${asInt(tech.confidence)}% confidence`],
            ["Technical trend",      asStr(tech.trend) || "—"],
            ["Current price",        asStr(tech.current_price) ? `₹${asStr(tech.current_price)}` : "—"],
          ]}
        />
      </section>
    </article>
  );
}
