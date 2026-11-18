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
