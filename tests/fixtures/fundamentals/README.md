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

## Filings lists and XBRL files (FS 2)

Fetched once by hand on 2026-11-17 (PST evening, 2026-11-18 IST morning) through `nse_files.Http` as above, after a
warm-up GET of the listing's NSE page (https://www.nseindia.com/companies-listing/corporate-filings-financial-results
for the results API, https://www.nseindia.com/companies-listing/corporate-integrated-filing for the integrated one;
both answered 200). Kept byte for byte.

| file | URL | answer | sha256 |
|------|-----|--------|--------|
| `filings_RELIANCE_quarterly.json` | https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=RELIANCE&period=Quarterly | 200, 101,555 bytes, 130 filings (2005 to 31-Dec-2024), all `period` "Quarterly" | 6bae731262ee43f76d0fed5c831cc9e8d1620705648446d3bd840de48313241d |
| `filings_RELIANCE_half-yearly.json` | same, `period=Half-Yearly` | 200, 4,511 bytes, 6 filings (2005 to 2010, no XBRL: link ends `/xbrl/-`), `period` "Half-Yearly" | bc44478aa870b9f88a9017f28239db1774989ae71a6d8cfa8a5112d1bdddacec |
| `filings_RELIANCE_annual.json` | same, `period=Annual` | 200, 29,542 bytes, 39 filings (to 31-Mar-2024), `period` "Annual" | ee917690a8d6a7f2fff4101cd451a1cc73805c1ced5c5c3180600f5ad0e80162 |
| `filings_integrated_RELIANCE.json` | https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=RELIANCE&type=Integrated%20Filing-%20Financials | 200, 10,302 bytes, `{"data": [...14], "totalCount": 14}`, quarters 31-Mar-2025 to 30-Sep-2026 | f58dc1dee6614d3e8f5a26e3a1644ac9936a8c3976f498c9ecccd711321979dc |
| `xbrl_RELIANCE_20241231_consolidated.xml` | https://nsearchives.nseindia.in/corporate/xbrl/INDAS_117297_1348248_16012025081520.xml | 200, 58,404 bytes, Ind AS (in-bse-fin 2020-03-31), quarter ended 31-Dec-2024, consolidated | 45e06a7ef567ffd564e2234cbe97293407fa7f6dcd4db568c7f25fd44ec492bc |
| `xbrl_RELIANCE_20241231_standalone.xml` | https://nsearchives.nseindia.in/corporate/xbrl/INDAS_117298_1348254_16012025082021.xml | 200, 57,588 bytes, same quarter, standalone | 7a4376e20bcff469a6674116f0e45a76233d6358517d082a0a714ba78e6c2e96 |
| `xbrl_RELIANCE_20260930_integrated_consolidated.xml` | https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_INDAS_1750537_30102026093254_WEB.xml | 200, 103,242 bytes, integrated filing ("IFIndAs V2.1 (26-06-2026)"), quarter ended 30-Sep-2026, consolidated | 5e596d6e3dc2941d060bcb95585708f9b1bc3e892db9ab27cc7c03fd9513f758 |

What the answers show: the results API ends with the quarter ended 31-Dec-2024 (broadcast 16-Jan-2025); every later
quarter is only in the integrated-filing API. `period` values are exactly "Quarterly", "Half-Yearly", "Annual"
(mapped to Q, H, A). The year's last quarter and the year are separate entries sharing one XBRL link, a few seconds
apart. `broadCastDate` is missing on the oldest rows (2005-2007), `exchdisstime` before 2013, `filingDate` is "-" on
the oldest; within 2021-04-01 onward all three are present. The integrated API's `type=` filter answered the same
with the space encoded as `+`; without it the answer also holds "Integrated Filing- Governance" rows (no figures).
The integrated standalone file for 30-Sep-2026 is 6.6 MB, so the consolidated one is kept here.
