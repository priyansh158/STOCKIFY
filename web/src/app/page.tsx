import Link from "next/link";
import { Logo } from "@/components/Logo";
import { SearchBox } from "@/components/SearchBox";

export default function Home() {
  return (
    <div className="mx-auto max-w-2xl px-4 py-10 sm:py-16">
      <Logo />
      <p
        className="stk-mono-tight mt-3"
        style={{ color: "var(--color-fg-muted)" }}
      >
        Indian-equity AI analysis &middot; NSE / BSE
      </p>

      <p className="mt-6 leading-relaxed" style={{ color: "var(--color-fg)" }}>
        Multi-stage Claude-Opus pipeline: Ind-AS financial scoring, collateral
        macro &amp; news risk, candlestick technicals — synthesized into a
        bottom-line verdict. Powered by live Kite Connect data.
      </p>

      <section className="mt-10">
        <SearchBox />
      </section>

      <nav className="mt-10 flex flex-wrap gap-3">
        <Link
          href="/history"
          className="stk-mono-tight rounded-md border px-4 py-2 text-sm"
          style={{
            borderColor: "rgba(0,255,65,0.25)",
            color: "var(--color-green-soft)",
            background: "rgba(0,255,65,0.04)",
          }}
        >
          📚 History
        </Link>
        <Link
          href="/portfolio"
          className="stk-mono-tight rounded-md border px-4 py-2 text-sm"
          style={{
            borderColor: "rgba(0,255,65,0.25)",
            color: "var(--color-green-soft)",
            background: "rgba(0,255,65,0.04)",
          }}
        >
          💼 Portfolio
        </Link>
        <Link
          href="/alerts"
          className="stk-mono-tight rounded-md border px-4 py-2 text-sm"
          style={{
            borderColor: "rgba(0,255,65,0.25)",
            color: "var(--color-green-soft)",
            background: "rgba(0,255,65,0.04)",
          }}
        >
          Geo alerts
        </Link>
      </nav>

      <footer
        className="mt-16 text-xs"
        style={{ color: "var(--color-fg-muted)" }}
      >
        Scope: NSE / BSE-listed Indian equities only. Not investment advice.
      </footer>
    </div>
  );
}
