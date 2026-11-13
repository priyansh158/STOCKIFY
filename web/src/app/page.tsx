import Link from "next/link";
import { Logo } from "@/components/Logo";
import { SearchBox } from "@/components/SearchBox";

const NAV = [
  { href: "/portfolio", label: "💼 Portfolio" },
  { href: "/alerts", label: "Geo alerts" },
  { href: "/tests", label: "Strategy tests" },
];

export default function Home() {
  return (
    <div className="mx-auto max-w-2xl px-4 py-10 sm:py-16">
      <Logo />
      <p
        className="stk-mono-tight mt-3"
        style={{ color: "var(--color-fg-muted)" }}
      >
        NSE simulation lab
      </p>

      <p className="mt-6 leading-relaxed" style={{ color: "var(--color-fg)" }}>
        Record Kite market data, backtest equity and F&amp;O strategies with
        realistic costs, fills and margin, and paper-trade them live with no
        real orders. Paper trading and data coverage pages arrive with the next
        milestones.
      </p>

      <section className="mt-10">
        <SearchBox />
      </section>

      <nav className="mt-10 flex flex-col gap-3 sm:flex-row sm:flex-wrap">
        {NAV.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className="stk-mono-tight flex min-h-11 items-center justify-center rounded-md border px-4 py-2 text-sm"
            style={{
              borderColor: "rgba(0,255,65,0.25)",
              color: "var(--color-green-soft)",
              background: "rgba(0,255,65,0.04)",
            }}
          >
            {item.label}
          </Link>
        ))}
      </nav>

      <footer
        className="mt-16 text-xs"
        style={{ color: "var(--color-fg-muted)" }}
      >
        Simulation only: STOCKIFY never places real orders. Not investment advice.
      </footer>
    </div>
  );
}
