import Link from "next/link";
import { Logo } from "@/components/Logo";
import { SearchBox } from "@/components/SearchBox";
import { DESTINATIONS } from "@/lib/nav";


export default function Home() {
  return (
    <div className="stk-page">
      <div className="stk-home-head">
        <div>
          <Logo />
          <p className="stk-lede">
            A practice lab for NSE trading. Test strategies on past prices, then paper-trade them live with pretend money.
          </p>
        </div>

        <section aria-label="Instrument search">
          <SearchBox />
        </section>
      </div>

      <nav aria-label="Where to go">
        <ul className="stk-dests" data-grid="">
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
  );
}
