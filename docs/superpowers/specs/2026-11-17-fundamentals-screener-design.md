# Fundamentals screener: today's listed stocks scored against their sector

Date: 2026-11-17. Approach chosen by the owner in conversation (their variant of option B).

## Intent

The owner wants a research tool that reads every listed company's quarterly results, compares each stock with its
sector on valuation and quality, and lists the stocks that look underpriced and healthy, as candidates worth
researching before buying. It answers "what is worth a closer look to buy today?", the complement of the holdings
desk ("when do I get out of what I hold?").

Owner's words that set the scope (2026-11-17):
- "Pick the stocks based on different sectors that are listed today, make a list differentiating different stocks
  using their sectors."
- "Hunt down till 2022 after covid because the period before that ... doesn't give us meaningful fundamentals."
- "The data itself inside [the quarterly reports] is the valuable key here that we need to figure out the best way to
  parse to get the right information."
- "There's no point in listing or hunting down delisted stocks because they won't help me make decisions today."
- It is "a section that is separate and on top in the research category of the platform".

## Goals

1. Universe: today's listed stocks grouped by NSE's own sector and industry classification. Start with today's
   Nifty 500 (all its sectors); the universe is a config list so it can be widened later.
2. Data: every quarterly and half-yearly result each company filed with NSE from April 2021 onward (so 2022's
   quarters have a year-earlier quarter for growth), parsed from NSE's machine-readable (XBRL) filings, each figure
   stored with its period, whether it is standalone or consolidated, and the date it was published.
3. Per stock and day: P/E (trailing four quarters), P/B, ROE, debt-to-equity, sales and profit growth (year on year,
   quarter vs the same quarter last year), operating margin; each compared with the stock's sector median.
4. A score per stock from those comparisons (cheap vs sector AND healthy), with the reasons in plain sentences.
5. A test record: the rule "hold the top-scoring N stocks, rebalanced monthly" run from 2022 to the research
   reserve (31 Jul 2026), compared with holding the Nifty 500, with walk-forward so the score's weights are not fitted
   to the whole period.
6. A new section at the top of the Research page: the ranked list by sector, each stock's page with its figures over
   time against its sector, and the test record.

## Non-goals

- No delisted or suspended companies (owner's decision). The page says plainly that the test covers today's listed
  companies only, which flatters the test a little (survivorship).
- No data before April 2021.
- No scraping of third-party sites (Screener, Tickertape, Moneycontrol): official NSE filings and NSE files only.
- No orders, no "buy" wording: the output is "candidates worth researching".
- No forecasts or price targets.

## Anti-goals

- A figure used before it was published (look-ahead): every number is used only from its filing's publication date.
- Mixing standalone and consolidated figures, or a quarter and a half-year, in one ratio.
- A silent bad parse: a filing whose figures fail the checks is held back and listed, never scored on.
- Jargon on the page: plain names ("price to earnings", "debt to equity") with one-line meanings.

## Constraints (project)

- Windows only; desktop UI with Hallmark and web/design.md; ₹ via Intl en-IN; money int64 paise, time UTC ns.
- No new packages without the owner's word: XBRL is XML, parsed with the standard library (xml.etree).
- All on-disk paths through stockify.paths; stores follow the project pattern; downloads keep their raw files and
  hashes like the existing NSE fetchers (stockify/data/nse_*); polite, rate-limited NSE requests with the existing
  headers/session handling.
- The research reserve (holdout from 2026-08-01) stays locked: tests end 31 Jul 2026.
- Research rules already in the platform apply (trial counting / deflated Sharpe, walk-forward machinery in
  stockify/research).

## Approaches considered

- A. Honest history: a point-in-time universe including delisted companies, NSE daily files backfilled with
  corporate-action adjustment. Most trustworthy test, most work. Not chosen: the owner only wants today's listed
  companies, since delisted ones cannot inform a decision today.
- **B (owner's variant, chosen). Today's listed stocks by sector, filings from April 2021, Kite prices.** Simpler
  data path; the test is labelled as covering today's listed companies only.
- C. Ranking only, no tests. Not chosen: the platform shows evidence for every suggestion.

## The data

- **Filings list:** NSE's corporate financial-results listing per company (quarterly, half-yearly, annual), each
  entry with its period end, standalone/consolidated, filing date and time, and the XBRL file link.
- **XBRL parsing:** the filings follow SEBI/NSE taxonomies (Ind AS formats; banks and NBFCs have their own formats).
  The parser maps taxonomy elements to a small fixed set of fields: revenue from operations, other income, total
  income, total expenses, finance costs, depreciation, profit before tax, tax, net profit, profit attributable to
  owners (consolidated), EPS (basic, diluted), paid-up equity and face value, and from half-yearly / annual
  statements total equity, total borrowings (current + non-current), total assets, cash and equivalents, operating
  cash flow. Each value keeps its unit/scale as filed (lakhs/crores) and is converted to paise exactly.
- **Checks before use:** units resolved; quarter values sum to the annual figure within rounding; EPS × shares ≈ net
  profit; a restated figure (a later filing for the same period) is stored as a new version with its own date, and
  the version known on a date is the one used on that date.
- **Banks and financials** have no "sales" or "debt to equity" in the usual sense: they are scored on their own
  measures (P/B, ROE, net interest income growth) within the financials sector only.
- **Sector classification:** NSE's macro-economic sector / sector / industry for each stock (from the Nifty 500
  constituent file's industry column and NSE's classification), stored with the date it was read.
- **Prices:** Kite daily candles via the existing bar cache; shares outstanding from the filings (paid-up equity /
  face value) for market cap.

## Scoring (my calls, shown on the page)

- Valuation vs sector: P/E and P/B as a percentile within the sector (cheaper = better); loss-making companies
  (negative trailing earnings) get no P/E score and are flagged.
- Quality vs sector: ROE, debt to equity (lower better), operating margin, and profit growth, as sector percentiles.
- Score = valuation half + quality half, each the mean of its percentiles; a stock needs at least four of the six
  figures to be scored. A sector needs at least five scored stocks for comparisons; smaller sectors are compared
  within their macro-economic sector instead.
- Value traps: the reasons call out a cheap P/E with falling profits or rising debt.

## The test record

- Monthly, on the first trading day: rank the universe by the score using only figures published by the day before;
  hold the top 20 equally weighted; costs and fills as the engine's daily-bar runs.
- Walk-forward over the 2022–Jul 2026 period with the existing machinery, varying only the basket size and the
  valuation/quality balance, so the reported result is out of sample.
- Compared with holding the Nifty 500 (Kite's index candles) over the same days.

## Where it shows

(Superseded by "Owner's changes" above: its own page, ordered list without scores, upcoming results on top.)

- Research page: a link at the top to the Fundamentals page; the page lists the candidates best first (symbol, a few
  words of reason, Details), with the sector shown beside each, and a link to the test record.
- A page per stock: its figures quarter by quarter against its sector median, the filings each came from (with dates
  and links), and why it scores as it does.
- A data page or panel: filings fetched, held back (with the reason), last update.

## Refresh

- A daily job in the daemon after the close (held during the session, like the desk's) fetches new filings for the
  universe and rescores. Results season brings many filings at once; the job is rate-limited and resumable.

## Owner's changes (2026-11-17, after reading the first draft)

These override anything above that disagrees:

- **No small, illiquid stocks** ("we don't want to target them anyway because they lack liquidity"). The universe is
  today's Nifty 500 plus a liquidity floor: a stock is listed only if its median daily traded value over the last
  three months is at least ₹8 crore (owner, 2026-11-17; a config value).
- **No score shown.** The page is an ordered list, best first; the order itself is the judgement. Each row reads:
  the symbol (e.g. RELIANCE), a few words on why the screen picked it ("Cheaper than its sector on earnings, with
  rising profits and little debt"), and a **Details** button. The ranking method (the sector comparisons below) decides
  the order internally; no number is printed beside a stock and the page carries no explanation of the method
  (owner: "that's implementation details, don't put it there"). Each stock's details page still shows the figures and
  the plain reasons for that stock.
- **Its own page.** "Fundamentals" is a page of its own, reached from the top of the Research page (a link card
  above the existing sections), not a section inside it.
- **Details page per stock:** graphs of every figure used (quarter by quarter, the stock against its sector median),
  the figures themselves in a table, the filings each came from with their dates, and the plain reasons for its place
  in the list.
- **Upcoming results, at the top.** Every week the screener reads NSE's announced board meetings for financial
  results and shows, above the list, which listed companies will publish results in the coming weeks (company, date,
  and the quarter). A company's filing is picked up after its results date by the daily refresh.

## Calls made after the builder check (mine, for the owner to check)

1. **The test runs in the engine** as an ordinary strategy, "Fundamentals basket": the screener computes the monthly
   ranking schedule (date → the top N symbols) from point-in-time figures and passes it to the strategy as a
   parameter; the strategy holds those symbols equally weighted in a multi-instrument daily-bar pack. Walk-forward and
   trial counting work as they do for any strategy: each grid cell (basket size × valuation/quality balance) is one
   try. If the engine cannot hold a multi-instrument basket from a parameter, the plan's first task finds out and the
   plan adds the smallest engine-side change, not a separate simulator.
2. **Two price series.** Returns in the test use Kite's adjusted daily candles (the bar cache stores adjusted prices).
   Valuation (market cap for P/E and P/B) uses the raw close from NSE's daily price files times the shares outstanding
   on that date, so splits and bonuses never distort a ratio. The NSE daily files are backfilled from April 2021 with
   the existing fetcher. A company listed after 2022 enters the ranking once it has four quarters filed and a price.
3. **No look-ahead rule.** A figure is usable from the trading day after NSE broadcast its results (the broadcast
   time in NSE's listing, compared in IST), whichever of the PDF or XBRL came first. A later filing for the same
   period with different figures is a restatement: a new version usable from its own broadcast day; identical figures
   are the same version. The monthly ranking uses figures usable on or before the rebalance day.
4. **Banks and NBFCs** are parsed with their own taxonomy map. Shippable when at least 90 % of non-financial and
   80 % of financial companies in the universe parse cleanly for their latest four quarters; failures are listed with
   the reason and left out of the ranking, never guessed.
5. **Sectors:** NSE's four-level classification (macro-economic sector, sector, industry, basic industry) read per
   stock from NSE, today's version applied to the whole period; the page states that reclassifications since 2022
   are not reflected.
6. **Shares outstanding:** paid-up equity ÷ face value from the filings, carried forward between filings and checked
   against EPS × shares ≈ profit; NSE's issued-share figure is the fallback. Trailing P/E needs the four latest
   quarters of the same kind (no half-year substitutes); otherwise no P/E that day.
7. **Consolidated when the company files it, for the whole period; standalone otherwise.** Fixed per company, never
   mixed.
8. **The one-off backfill** (about 500 companies × ~22 periods, both kinds) runs politely (about one request a
   second), resumes from the raw files already saved, and keeps every raw file with its hash.
9. **Benchmark:** the Nifty 500 total-return index from NSE Indices' published history when it can be downloaded;
   otherwise the price index from Kite, labelled "price index, without dividends".

## Left to the builder

- Exact NSE endpoints and session handling (follow the existing NSE fetchers), store tables, the XBRL element map per
  taxonomy version (with tests on real sample filings saved as fixtures), page layout within web/design.md.
