/** Option ideas priced from the chain: what each costs, its worst and best case, where it breaks even, and its margin.
 * Ideas to consider only: STOCKIFY places no orders, and nothing here places one. */
import { rupees } from "@/lib/format";
import type { DeskIdeaDTO, DeskViewDTO } from "@/lib/types";
import { Empty, Table } from "@/components/ui";

function premiumWords(p: number): { label: string; value: string } {
  if (p > 0) return { label: "You pay", value: rupees(p) };
  if (p < 0) return { label: "You receive", value: rupees(-p) };
  return { label: "Premium", value: rupees(0) };
}

function marginWords(i: DeskIdeaDTO): { text: string; tone?: string } {
  if (i.margin_paise == null) return { text: "Kite did not say" };
  if (i.margin_ok === false) return { text: `${rupees(i.margin_paise)}, needs more margin than you have`, tone: "stk-warn" };
  if (i.margin_ok === true) return { text: `${rupees(i.margin_paise)}, within your margin` };
  return { text: rupees(i.margin_paise) };
}

function Idea({ idea, n }: { idea: DeskIdeaDTO; n: number }) {
  const prem = premiumWords(idea.premium_paise);
  const margin = marginWords(idea);
  return (
    <article className="stk-subblock" aria-labelledby={`desk-idea-${n}`}>
      <h3 id={`desk-idea-${n}`}>{idea.name}</h3>
      {!idea.possible ? (
        <p className="stk-muted stk-gap-xs">Not possible now: {idea.not_possible_reason}</p>
      ) : (
        <div className="stk-stack-s stk-gap-xs stk-desk-idea">
          <p className="stk-desk-measure">{idea.why}</p>
          {idea.caution && <p className="stk-desk-measure stk-warn">{idea.caution}</p>}
          <Table
            caption={`The legs of the ${idea.name.toLowerCase()}`}
            head={[{ label: "Leg" }, { label: "Contract" }, { label: "Lots", num: true }, { label: "Price per share", num: true }]}
          >
            {idea.legs.map((l, k) => (
              <tr key={`${k}-${l.tradingsymbol ?? ""}`}>
                <td className={l.side === "BUY" ? "stk-up" : "stk-down"}>{l.side === "BUY" ? "Buy" : "Sell"}</td>
                <td className="stk-mono">{l.tradingsymbol ?? "—"}</td>
                <td className="num">{l.lots.toLocaleString("en-IN")}</td>
                <td className="num">{rupees(l.price_paise)}</td>
              </tr>
            ))}
          </Table>
          <dl className="stk-kv stk-kv-wide">
            <dt>{prem.label}, for the lots shown</dt>
            <dd>{prem.value}</dd>
            <dt>Most it can lose</dt>
            <dd data-plain={idea.max_loss_paise == null ? "" : undefined}>{idea.max_loss_paise != null ? rupees(idea.max_loss_paise) : "No limit"}</dd>
            <dt>Most it can make</dt>
            <dd data-plain={idea.max_gain_paise == null ? "" : undefined}>{idea.max_gain_paise != null ? rupees(idea.max_gain_paise) : "No limit"}</dd>
            <dt>Breaks even at</dt>
            <dd data-plain={idea.breakeven_paise.length ? undefined : ""}>{idea.breakeven_paise.length ? idea.breakeven_paise.map((b) => rupees(b)).join(", ") : "Not known"}</dd>
            <dt>Margin Kite asks for</dt>
            <dd className={margin.tone} data-plain={idea.margin_paise == null ? "" : undefined}>{margin.text}</dd>
          </dl>
        </div>
      )}
    </article>
  );
}

export default function IdeasList({ view, note }: { view: DeskViewDTO; note?: string }) {
  if (!view.fno) return null; // a stock without options: the chain section says so, and there are no ideas
  return (
    <section className="stk-section" aria-labelledby="desk-ideas">
      <h2 id="desk-ideas">Option ideas</h2>
      <p className="stk-fine">Ideas to weigh, priced from the chain. STOCKIFY places no orders; any trade is yours to make in Kite.</p>
      {view.ideas.length === 0 ? (
        <div className="stk-gap-xs">
          <Empty>{note || "No option idea fits this stock right now."}</Empty>
        </div>
      ) : (
        <div className="stk-gap-xs">
          {view.ideas.map((i, n) => (
            <Idea key={`${n}-${i.name}`} idea={i} n={n} />
          ))}
        </div>
      )}
    </section>
  );
}
