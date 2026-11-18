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
## XBRL results filings (FS 3)

Fetched on 2026-11-17 (about 20:00-20:20 PST) the same way: the filings lists from
https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=<SYM>&period=Quarterly (after a warm-up
GET of https://www.nseindia.com/companies-listing/corporate-filings-financial-results) and, for the integrated filings,
https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=<SYM>&type=Integrated%20Filing-%20Financials
(after https://www.nseindia.com/companies-listing/corporate-integrated-filing); then each XBRL file once. All answered 200.

| file | URL | what it is | sha256 |
|------|-----|------------|--------|
| `xbrl_RELIANCE_2024-12-31_consolidated.xml` | https://nsearchives.nseindia.in/corporate/xbrl/INDAS_117297_1348248_16012025081520.xml | 58,404 bytes; Ind AS, quarter ended 31-Dec-2024, consolidated (no balance sheet) | 45e06a7ef567ffd564e2234cbe97293407fa7f6dcd4db568c7f25fd44ec492bc |
| `xbrl_RELIANCE_2024-12-31_standalone.xml` | https://nsearchives.nseindia.in/corporate/xbrl/INDAS_117298_1348254_16012025082021.xml | 57,588 bytes; Ind AS, same quarter, standalone | 7a4376e20bcff469a6674116f0e45a76233d6358517d082a0a714ba78e6c2e96 |
| `xbrl_RELIANCE_2024-09-30_consolidated.xml` | https://nsearchives.nseindia.in/corporate/xbrl/INDAS_112771_1269085_14102024073244.xml | 83,484 bytes; Ind AS, quarter ended 30-Sep-2024 with the half-year balance sheet and cash flow | d1daceb0f8cdd5708fe0b2f61d9d863f9dd3ec85440e14554791a870abc3426b |
| `xbrl_RELIANCE_2021-06-30_consolidated.xml` | https://nsearchives.nseindia.in/corporate/xbrl/INDAS_73303_495024_23072021080800_WEB.xml | 35,440 bytes; Ind AS, quarter ended 30-Jun-2021: refers to OneD / OneI without defining them | 0b3104296636d7e91091d6b2138ee68e98459572f9b0de83d400434c3e885868 |
| `xbrl_INFY_2024-03-31_consolidated.xml` | https://nsearchives.nseindia.in/corporate/xbrl/INDAS_104589_1099938_19042024112830.xml | 108,706 bytes; Ind AS, quarter and year ended 31-Mar-2024 (shared by the Q4 and Annual listings) | 76b50cfcb43c474f7db3203627fe300165dcc54e7890d71eae416d7b838bb977 |
| `xbrl_VSTIND_2024-12-31_standalone.xml` | https://nsearchives.nseindia.in/corporate/xbrl/INDAS_119082_1374638_07022025052815.xml | 27,725 bytes; Ind AS, quarter ended 31-Dec-2024, rounding declared in Lakhs (values still in rupees) | 103b4d36a2ac86903d2db409e8c056148e41b01f7acf4a07e3b7733416e3ac8a |
| `xbrl_HDFCBANK_2024-12-31_standalone.xml` | https://nsearchives.nseindia.in/corporate/xbrl/BANKING_117524_1359008_23012025122553.xml | 41,868 bytes; Banking format, quarter ended 31-Dec-2024 | ce65de3c70e635ee4233ff08c17bf8bd70381275e91de59463c9fe8c1e70209b |
| `xbrl_HDFCBANK_2021-06-30_standalone.xml` | https://nsearchives.nseindia.in/corporate/xbrl/BANKING_73090_488992_19072021114537_WEB.xml | 38,326 bytes; Banking format, quarter ended 30-Jun-2021: no start date, no rounding declared | be0bdb1ee914ac00648181d85d89d4567c6470dbfdc0cc62e743498e4ba0c754 |
| `xbrl_SBIN_2024-09-30_standalone.xml` | https://nsearchives.nseindia.in/corporate/xbrl/BANKING_114554_1301550_08112024044234.xml | 63,924 bytes; Banking format, quarter ended 30-Sep-2024 with balance sheet; `Assets` filed without a unit | 2b36ad15bef5047059b89a8b97e21128f23b1790f57a4f4499cb7263ff0d3b7e |
| `xbrl_BAJFINANCE_2024-12-31_consolidated.xml` | https://nsearchives.nseindia.in/corporate/xbrl/NBFC_INDAS_118072_1365160_29012025074915.xml | 33,480 bytes; NBFC Ind AS, quarter ended 31-Dec-2024; balance-sheet lines filed as zeros | fa22efb7fc20881d5fecb414bb4cf73208547fbc2f8bee5e92c30b43f0a74c2c |
| `xbrl_BAJFINANCE_2024-09-30_consolidated.xml` | https://nsearchives.nseindia.in/corporate/xbrl/NBFC_INDAS_113040_1285336_22102024074237.xml | 54,765 bytes; NBFC Ind AS, quarter ended 30-Sep-2024 with balance sheet | 0e8de80125965de5d5a8fbba131b6ab8b782947b21bbf194842bc776fcddc393 |
| `xbrl_RELIANCE_20260930_integrated_consolidated.xml` | https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_INDAS_1750537_30102026093254_WEB.xml | 103,242 bytes; integrated filing (IFIndAs V2.1), quarter ended 30-Sep-2026, consolidated; copied from the Task 2 fixtures | 5e596d6e3dc2941d060bcb95585708f9b1bc3e892db9ab27cc7c03fd9513f758 |
| `xbrl_HDFCBANK_20260930_integrated_consolidated.xml` | https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_BANKING_1740913_17102026054812_WEB.xml | 80,749 bytes; integrated filing (IFBanking V2.1), quarter ended 30-Sep-2026, consolidated | 41840bebf50eacb915fe9ddd2d7cd2bace3d6fffdaa0898a482bd56ddd94aa3b |
| `xbrl_BAJFINANCE_20260930_integrated_consolidated.xml` | https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_NBFC_INDAS_1742040_19102026075918_WEB.xml | 57,385 bytes; integrated filing (IFNBFC V2.1), quarter ended 30-Sep-2026, consolidated | 3de881019f229f5fde8d2aa113ea1c71304f5225ebe6a4d192b5f4a65f54ea34 |
| `xbrl_RELIANCE_2024-12-31_standalone_broken.xml` | (made here) | a copy of the standalone file with the quarter's ProfitBeforeTax raised from 115970000000.00 to 125970000000.00 (₹1,000 crore), so profit after tax no longer equals profit before tax minus tax | 0fe2abb77764a53cdd9c001b292609456f5ae7f2d51c48c942699202d5d1e2ac |
## Event calendar (FS 7)

Fetched on 2026-11-17 (IST) the same way, after a warm-up GET of
https://www.nseindia.com/companies-listing/corporate-filings-event-calendar (200).

| file | URL | answer | sha256 |
|------|-----|--------|--------|
| `event_calendar_2026-11-17.json` | https://www.nseindia.com/api/event-calendar?index=equities&from_date=17-11-2026&to_date=15-12-2026 | 200, JSON list of 21 items, keys `symbol`, `company`, `purpose`, `bm_desc`, `date` ("19-Nov-2026"); dates 17-Nov to 05-Dec | 5ff00b3a11a191ac36d5835e8f509322a4f98902c3c899c74cac7cc739d52a44 |

Without the range parameters the same endpoint answered 13 items (the next few days only), so the range is needed.
Purposes seen: "Financial Results", "Financial Results/Other business matters", "Other business matters",
"Fund Raising", "Buyback", "Bonus/Other business matters".
