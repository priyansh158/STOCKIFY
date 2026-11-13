/**
 * The destinations of the app, in the order of the top bar. The top bar (AppNav) and the home page's list both read this one
 * list, so a page added to one is in the other. `note` is the home page's short description.
 */
export const DESTINATIONS = [
  { href: "/portfolio", label: "Portfolio", note: "Your Zerodha holdings, read-only." },
  { href: "/alerts", label: "Geo alerts", note: "World events that could disrupt Indian stocks, found by the background news monitor." },
  { href: "/tests", label: "Strategy tests", note: "Backtests on recorded and downloaded NSE data, with real costs." },
  { href: "/trade", label: "Paper trading", note: "Strategies trade live prices with pretend money." },
  { href: "/research", label: "Research", note: "Parameter sweeps and walk-forward runs, and how much a result can be trusted." },
  { href: "/data", label: "Data coverage", note: "What has been recorded and downloaded, day by day, and what is missing." },
] as const;
