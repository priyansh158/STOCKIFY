/** Each tested rule on this stock: what it says now, its exit level, and how it has done here before. */
import { dayWords, pct, rupees } from "@/lib/format";
import { GRADE_WORDS, gradeClass, signalWords } from "@/lib/desk";
import type { DeskViewDTO } from "@/lib/types";
import { Empty, Table } from "@/components/ui";

export default function SignalsTable({ view }: { view: DeskViewDTO }) {
  const from = view.evidence_from;
  const to = view.evidence_to;
  return (
    <section className="stk-section" aria-labelledby="desk-rules">
      <h2 id="desk-rules">The rules on this stock</h2>
      {from && to && (
        <p className="stk-fine stk-gap-bottom">
          Tested on daily prices from {dayWords(from)} to {dayWords(to)}; the months after {dayWords(to)} are kept aside
          for research and not used to grade.
        </p>
      )}
      {view.signals.length === 0 ? (
        <Empty>No rule has been run on this stock yet; the desk runs them after the close.</Empty>
      ) : (
        <>
          <Table
            caption="What each tested rule says now and its record on this stock"
            head={[
              { label: "Rule" },
              { label: "Says now" },
              { label: "Exit level", num: true },
              { label: "Record on this stock" },
              { label: "Years it beat holding", num: true },
              { label: "Trades", num: true },
              { label: "Per year", sub: "the rule", num: true },
              { label: "Per year", sub: "just holding", num: true },
              { label: "Why" },
            ]}
          >
            {view.signals.map((s) => {
              const now = signalWords(s);
              return (
                <tr key={s.display_name}>
                  <td>{s.display_name}</td>
                  <td className={now.out ? "stk-down" : undefined}>{now.text}</td>
                  <td className="num">{s.exit_level_paise != null ? rupees(s.exit_level_paise) : "—"}</td>
                  <td className={gradeClass(s.grade)}>{GRADE_WORDS[s.grade]}</td>
                  <td className="num">
                    {s.windows_total ? `${s.windows_won ?? 0} of ${s.windows_total}` : "—"}
                  </td>
                  <td className="num">{s.trades != null ? s.trades.toLocaleString("en-IN") : "—"}</td>
                  <td className="num">{pct(s.cagr, true)}</td>
                  <td className="num">{pct(s.buy_hold_cagr, true)}</td>
                  <td data-wrap className="stk-desk-why">{s.note || <span className="stk-muted">No note</span>}</td>
                </tr>
              );
            })}
          </Table>
          <p className="stk-fine stk-gap-top">
            Record on this stock: none when the rule made fewer than 8 trades or the stock has under 3 years of prices; strong
            when it beat just holding the stock, risk for risk, in at least 70% of the yearly windows and overall; some
            from 55%; weak below that. A rule with no record carries no weight in the suggestion.
          </p>
        </>
      )}
    </section>
  );
}
