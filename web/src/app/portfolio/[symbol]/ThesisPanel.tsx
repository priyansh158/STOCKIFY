/** The desk's suggestion for one holding: the verdict, why, the level that would change it, and who agrees. */
import { rupees } from "@/lib/format";
import { verdictClass } from "@/lib/desk";
import type { DeskViewDTO } from "@/lib/types";

function names(list: string[]): string {
  return list.length ? list.join(", ") : "None";
}

export default function ThesisPanel({ view }: { view: DeskViewDTO }) {
  const noSignal = view.verdict === "No reliable signal";
  return (
    <section className="stk-section" aria-labelledby="desk-thesis">
      <h2 id="desk-thesis">The suggestion</h2>
      <div className="stk-stack-s">
        <p className={`stk-desk-verdict ${verdictClass(view.verdict)}`}>{view.verdict}</p>
        <ul className="stk-bullets">
          {view.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
        {view.counts && <p className="stk-fine">{view.counts[0].toUpperCase() + view.counts.slice(1)}.</p>}

        <dl className="stk-kv stk-kv-wide stk-gap-xs">
          <dt>Closing price</dt>
          <dd>{rupees(view.price_paise)}</dd>
          <dt>Nearest exit level</dt>
          <dd data-plain={view.nearest_exit_paise == null ? "" : undefined}>{view.nearest_exit_paise != null ? rupees(view.nearest_exit_paise) : "None below the price"}</dd>
        </dl>
        {view.nearest_exit_paise != null && (
          <p className="stk-fine">
            {rupees(view.nearest_exit_paise)} is the highest exit level below the closing price among the rules that still say stay
            in and have a record on this stock. A close under it would turn that rule to get out.
          </p>
        )}

        {!noSignal && (
          <dl className="stk-desk-sides">
            <div>
              <dt className="stk-label">Rules that say stay in</dt>
              <dd>{names(view.agree)}</dd>
            </div>
            <div>
              <dt className="stk-label">Rules that say get out</dt>
              <dd>{names(view.disagree)}</dd>
            </div>
          </dl>
        )}

        <details className="stk-fine">
          <summary>How the suggestion is made</summary>
          <p className="stk-gap-xs">
            Each rule says stay in or get out. Its vote is weighted by its record on this stock: strong counts 3, some 2, weak 1,
            none 0. Exit when rules carrying at least 60% of the weight say get out and one of them has a strong or
            some record; Trim from 40%; Tighten stop when the price is within one day&apos;s typical move of the
            nearest exit level; otherwise Hold. With no weight at all, the desk gives no call. Your cost and profit
            play no part.
          </p>
        </details>
      </div>
    </section>
  );
}
