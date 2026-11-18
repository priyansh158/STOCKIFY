/** The option chain read as a gauge: open interest, max pain, implied volatility, in figures and plain sentences. */
import { dayWords, rupees } from "@/lib/format";
import { NO_OPTIONS, nsWhen } from "@/lib/desk";
import type { DeskViewDTO } from "@/lib/types";
import { Empty, Stat, StatGrid } from "@/components/ui";

function strikes(list: number[]): string {
  return list.length ? list.map((p) => rupees(p)).join(", ") : "none";
}

export default function ChainGauge({ view }: { view: DeskViewDTO }) {
  const g = view.gauge;
  return (
    <section className="stk-section" aria-labelledby="desk-chain">
      <h2 id="desk-chain">The option chain</h2>
      {!view.fno ? (
        <Empty>{NO_OPTIONS}</Empty>
      ) : !g ? (
        <Empty>No option chain has been read for this stock yet; it is read about once a minute during the session.</Empty>
      ) : (
        <div className="stk-stack-s">
          <p className="stk-fine">
            {g.live ? "Live: " : "Last read "}
            {nsWhen(g.taken_ns)}
            {g.expiry ? `, for the expiry of ${dayWords(g.expiry)}` : ""}
            {view.lot ? `. One lot is ${view.lot.toLocaleString("en-IN")} shares.` : "."}
          </p>
          <StatGrid>
            <Stat size="s" label="Put/call open interest" value={g.pcr_oi != null ? g.pcr_oi.toFixed(2) : "not known"} />
            <Stat size="s" label="Max pain" value={g.max_pain_paise != null ? rupees(g.max_pain_paise) : "not known"} />
            <Stat size="s" label="Implied volatility near the price" value={g.atm_iv != null ? `${(g.atm_iv * 100).toFixed(1)}%` : "not known"} />
            <Stat size="s" label="Against the past year" value={g.iv_percentile != null ? `at or above ${Math.round(g.iv_percentile)}% of days` : "too few days to say"} />
          </StatGrid>
          <dl className="stk-desk-sides">
            <div>
              <dt className="stk-label">Most open calls above the price (resistance)</dt>
              <dd className={g.call_walls.length ? "stk-mono" : undefined}>{strikes(g.call_walls)}</dd>
            </div>
            <div>
              <dt className="stk-label">Most open puts below the price (support)</dt>
              <dd className={g.put_walls.length ? "stk-mono" : undefined}>{strikes(g.put_walls)}</dd>
            </div>
          </dl>
          {g.sentences.length > 0 && (
            <ul className="stk-bullets">
              {g.sentences.map((s) => (
                <li key={s}>{s}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
