/** "Results coming up": the board meetings for results NSE lists over the next four weeks, soonest first. */
import Link from "next/link";
import { dayWords } from "@/lib/format";
import { detailHref } from "@/lib/fundamentals";
import type { ResultsDateDTO } from "@/lib/types";
import { Empty, Table } from "@/components/ui";

export default function UpcomingResults({ rows }: { rows: ResultsDateDTO[] }) {
  return (
    <section className="stk-section" aria-labelledby="fund-upcoming">
      <h2 id="fund-upcoming">Results coming up</h2>
      {rows.length === 0 ? (
        <Empty>No company the screener covers has a results date on NSE&apos;s calendar in the next four weeks.</Empty>
      ) : (
        <Table
          caption="Results coming up in the next four weeks, soonest first"
          head={[{ label: "Board meeting" }, { label: "Stock" }, { label: "Company" }, { label: "Results for" }]}
        >
          {rows.map((r) => (
            <tr key={`${r.symbol}-${r.date}`}>
              <td className="stk-nowrap">{dayWords(r.date)}</td>
              <th scope="row">
                <Link href={detailHref(r.symbol)}>{r.symbol}</Link>
              </th>
              <td>{r.company}</td>
              <td>{r.period_hint || "—"}</td>
            </tr>
          ))}
        </Table>
      )}
    </section>
  );
}
