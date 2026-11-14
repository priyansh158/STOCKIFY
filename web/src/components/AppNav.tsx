"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { DESTINATIONS } from "@/lib/nav";


/** The top bar: the wordmark (a link home; on the home page the page's own heading carries it) and the six destinations. */
export function AppNav() {
  const path = usePathname() ?? "/";
  return (
    <header className="stk-topbar">
      <div className="stk-topbar-in">
        {path !== "/" && (
          <Link href="/" className="stk-wordmark">
            STOCKIFY
          </Link>
        )}
        <nav className="stk-nav" aria-label="Main">
          {DESTINATIONS.map((item) => {
            const here = path === item.href || path.startsWith(`${item.href}/`);
            return (
              <Link key={item.href} href={item.href} aria-current={here ? "page" : undefined}>
                {item.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
