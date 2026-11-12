import Link from "next/link";
import { Logo } from "@/components/Logo";
import { SearchBox } from "@/components/SearchBox";

const NAV = [
  { href: "/portfolio", label: "Portfolio", note: "Your Zerodha holdings, read-only." },
  { href: "/alerts", label: "Geo alerts", note: "World events that could disrupt Indian stocks, found by the background news monitor." },
  { href: "/tests", label: "Strategy tests", note: "Backtests on recorded and downloaded NSE data, with real costs." },
  { href: "/trade", label: "Paper trading", note: "Strategies trade live prices with pretend money." },
];

export default function Home() {
  return (
    <div className="stk-page">
      <div className="stk-head">
        <div>
          <Logo />
          <p className="stk-lede">
            A practice lab for NSE trading. Test strategies on past prices, then paper-trade them live with pretend money.
          </p>
        </div>
      </div>

      <div className="stk-cols-split">
        <section aria-label="Instrument search">
          <SearchBox />
        </section>

        <nav aria-label="Where to go">
          <ul className="stk-dests">
            {NAV.map((item) => (
              <li key={item.href}>
                <Link href={item.href}>
                  <strong>{item.label}</strong>
                  <span>{item.note}</span>
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      </div>
    </div>
  );
}
