import Link from "next/link";
import { Logo } from "@/components/Logo";
import { SearchBox } from "@/components/SearchBox";
import { DESTINATIONS } from "@/lib/nav";


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
            {DESTINATIONS.map((item) => (
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
