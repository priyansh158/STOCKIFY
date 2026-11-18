/**
 * "Worth a closer look": the stocks best first, each with its industry, a few words of why and a way to its details.
 * The order is the judgement: no score, rank number or account of how the order is made is shown (owner, 2026-11-17).
 */
import Link from "next/link";
import { detailHref } from "@/lib/fundamentals";
import type { CandidateDTO } from "@/lib/types";
import { Table } from "@/components/ui";

export default function CandidateList({ items }: { items: CandidateDTO[] }) {
  return (
    <Table
      caption="Stocks worth a closer look: industry and why"
      head={[{ label: "Stock" }, { label: "Industry" }, { label: "Why" }, { label: "" }]}
      tall={items.length > 25}
    >
      {items.map((c) => (
        <tr key={c.symbol}>
          <th scope="row">
            {c.symbol}
            <div className="stk-label">{c.company}</div>
          </th>
          <td>{c.sector || <span className="stk-muted">Not given</span>}</td>
          <td data-wrap className="stk-fund-why">{c.short_reason}</td>
          <td className="stk-fund-go">
            <Link href={detailHref(c.symbol)} className="stk-btn" aria-label={`Details of ${c.symbol}`}>
              Details
            </Link>
          </td>
        </tr>
      ))}
    </Table>
  );
}
