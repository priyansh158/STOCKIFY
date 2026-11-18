# Fundamentals fixtures (real NSE answers)

Fetched once by hand on 2026-11-17 (about 19:48 PST, 2026-11-18 09:18 IST) through the project's own
`stockify.data.nse_files.Http` (browser User-Agent, Referer https://www.nseindia.com/, cookie warm-up, 1 s between
requests). Kept byte for byte.

| file | URL | answer | sha256 |
|------|-----|--------|--------|
| `ind_nifty500list.csv` | https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv | 200, 32,765 bytes, 501 rows (496 EQ, 3 RR, 2 BE) | 31b764a95cc03ad1d7ddb6c42340de9125ed2c78b3d436bf0dbc8440e4e38c5e |
| `quote-equity_RELIANCE_403.html` | https://www.nseindia.com/api/quote-equity?symbol=RELIANCE (after a warm-up GET of https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE, which answered 200) | 403, 399 bytes, Akamai "Access Denied" HTML | 66418e6b4c0e8c73b7354c76ecf419b71280ce557db1a2b6aef0ce710d5d4bfa |

The same 403 came back for HDFCBANK, and again with the quote page as Referer and a JSON Accept header. No real
`quote-equity` JSON body could be saved on that day, so there is no sector (`industryInfo`) fixture yet.

## Event calendar (FS 7)

Fetched on 2026-11-17 (IST) the same way, after a warm-up GET of
https://www.nseindia.com/companies-listing/corporate-filings-event-calendar (200).

| file | URL | answer | sha256 |
|------|-----|--------|--------|
| `event_calendar_2026-11-17.json` | https://www.nseindia.com/api/event-calendar?index=equities&from_date=17-11-2026&to_date=15-12-2026 | 200, JSON list of 21 items, keys `symbol`, `company`, `purpose`, `bm_desc`, `date` ("19-Nov-2026"); dates 17-Nov to 05-Dec | 5ff00b3a11a191ac36d5835e8f509322a4f98902c3c899c74cac7cc739d52a44 |

Without the range parameters the same endpoint answered 13 items (the next few days only), so the range is needed.
Purposes seen: "Financial Results", "Financial Results/Other business matters", "Other business matters",
"Fund Raising", "Buyback", "Bonus/Other business matters".
