# 01 — External facts the code depends on

Everything here can change. Each row has an ID that code comments and config rows should cite (for example `# per 01_facts K-9`), a date it was checked, and a source. **✔** = checked against a primary or staff source on 2026-10-29. **⚠** = from a secondary source, memory, or community report: verify before code depends on it, then record the evidence in the row.

When a fact changes: add a new dated row to the relevant config (I-1), never edit history in place, and update this file.

---

## 1. Kite Connect (K-)

| ID | Fact | Status | Source |
|---|---|---|---|
| K-1 | WebSocket endpoint `wss://ws.kite.trade?api_key=…&access_token=…`. Up to **3,000 instruments per connection**, up to **3 connections per API key**. Exceeding the subscription count historically failed silently (no error, no data). | ✔ | [WebSocket docs](https://kite.trade/docs/connect/v3/websocket/), [forum](https://kite.trade/forum/discussion/comment/7357/) |
| K-2 | Modes: `ltp` (8-byte packet), `quote` (44 bytes, no depth), `full` (184 bytes, with 5-level depth). Prices are `int32` paise (divide by 100). A 1-byte heartbeat arrives every couple of seconds when idle. Text (JSON) messages on the same socket carry order postbacks, errors and broker messages. | ✔ | WebSocket docs |
| K-3 | Packet fields. **Quote**: token, LTP, last traded qty, average traded price (ATP), day volume, total buy qty, total sell qty, day open, high, low, previous close. **Full** adds: last-trade timestamp, OI, OI day high, OI day low, **exchange timestamp**, depth (5 bids + 5 asks: qty, price, orders). **Index packets**: quote = LTP, high, low, open, close, change (28 bytes); full adds the exchange timestamp (32 bytes). ⇒ *Quote mode has no exchange timestamp and no bid/ask.* Timestamps are epoch **seconds**. | ✔ | WebSocket docs; forum (timestamps only in full mode) |
| K-4 | The feed is **snapshots**, not trades: Zerodha staff describe it as level-2 snapshot data as broadcast by the exchange, about one or two updates per second per instrument, while hundreds of trades may occur. The exchange also publishes a snapshot when depth changes. | ✔ (staff) | [forum, sujith](https://kite.trade/forum/profile/comments/sujith?page=p4) |
| K-5 | Historical candles: intervals `minute, 3minute, 5minute, 10minute, 15minute, 30minute, 60minute, day`; params `from`, `to`, `continuous`, `oi`. Timestamps carry `+0530` and mark the **start** of the bar. `continuous=1` works only for NFO/MCX **futures** and returns **day** candles of expired contracts. No minute data for expired contracts; **no data at all for expired options**. Per-request date-range caps exist per interval but are not on the docs page. | ✔ / caps ⚠ | [Historical docs](https://kite.trade/docs/connect/v3/historical/), [forum](https://www.kite.trade/forum/discussion/comment/42068/) |
| K-6 | Historical data **is adjusted for corporate actions** (bonuses, splits, rights, spin-offs, extraordinary dividends above ~2%), applied in the beginning-of-day process on the ex-date. Past candles can change when fetched later. Volumes are adjusted for bonus/split/rights/demerger (not dividends). Kite offers **no unadjusted** series and no corporate-actions API. | ✔ (staff, 2025–26) | [forum 16280](https://kite.trade/forum/discussion/16280/historical-5-minute-api-data-corporate-action-price-and-volume-adjustments), [forum 52411](https://www.kite.trade/forum/discussion/comment/52411/) |
| K-7 | Access tokens expire at **06:00 IST the next day**. The official login is a browser flow (login page → redirect with `request_token` → exchange for `access_token`); the official SDKs have no headless login. A 403 `TokenException` means re-login. SEBI's implementation standards for retail algos also require daily logout of API sessions. | ✔ | [Exceptions docs](https://kite.trade/docs/connect/v3/exceptions/), [brokerkit notes](https://pypi.org/project/brokerkit-zerodha), [implementation standards summary](https://www.finseclaw.com/article/nse-releases-implementation-standards-for-retail-participation-in-algorithmic-trading) |
| K-8 | Rate limits: **Quote 1 req/s, Historical 3 req/s, Order placement 10 req/s, all other endpoints 10 req/s**; 400 orders/min, 5,000 orders/day, 25 modifications per order; HTTP 429 when exceeded. The quote endpoint accepts up to ~500 instruments per call. | ✔ / 500 ⚠ | Exceptions docs; [forum](https://kite.trade/forum/discussion/13071/quotes-api-throttling-limits) |
| K-9 | **Instrument tokens are unique only for the current day**; exchanges reuse tokens after a contract expires. Zerodha recommends keying on exchange + tradingsymbol. The instruments dump has `instrument_type` ∈ {EQ, FUT, CE, PE}; indices appear with segment `INDICES`; there is no ISIN column. | ✔ (staff) / dump details ⚠ | [forum 9073](https://kite.trade/forum/discussion/9073/instrument-lookup-by-id-for-expired-instruments), [forum](https://kite.trade/forum/discussion/comment/20479) |
| K-10 | Margin and charges endpoints (read-only, no order placed): `POST /margins/orders` (per-order margins: span, exposure, option_premium, additional, var, total, charges), `POST /margins/basket?consider_positions=…&mode=compact` (`initial` vs `final` = with spread benefit), `POST /charges/orders` ("virtual contract note": per-order brokerage, STT, exchange, SEBI, stamp, GST; `order_id` may be any string; `average_price` must be non-zero). | ✔ | [Margins docs](https://kite.trade/docs/connect/v3/margins/) |
| K-11 | Pricing: **Kite Connect ₹500/month per API key** includes live WebSocket data and historical candles. The free "Personal" plan has orders but **no market data**. From October 2026, ≥ ₹2,000 brokerage in a month earns 500 developer credits that cover the next renewal. | ✔ | [Kite API FAQ](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/kite-connect-api-faqs), [payment plan](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/historical-data-and-live-market-data-payment-plan) |
| K-12 | **Market protection** (since 2026-04-01 for API orders): MARKET and SL-M orders must carry `market_protection` (`-1` = automatic, or a percentage > 0 up to 100); `0` is rejected. The order is converted to a LIMIT at the protection price; if the market moves beyond it, the unfilled remainder can stay open. Execution is still subject to the exchange's limit-price-protection range. | ✔ (staff) | [forum 52940](https://kite.trade/forum/discussion/comment/52940/), [forum 52991](https://kite.trade/forum/discussion/comment/52991/), [forum 52145](https://kite.trade/forum/discussion/comment/52145) |
| K-13 | Static IP is enforced **only on order endpoints**; WebSocket and other data endpoints work from any IP. Order rate above 10/s returns 429 unless the strategy is exchange-registered. | ✔ (staff) | [forum 52147](https://kite.trade/forum/discussion/comment/52147/), [forum 52152](https://kite.trade/forum/discussion/comment/52152/) |

## 2. NSE market structure (X-)

| ID | Fact | Effective | Status | Source |
|---|---|---|---|---|
| X-1 | Weekly expiries on only **one benchmark index per exchange**: NSE keeps NIFTY weekly; BANKNIFTY, FINNIFTY, MIDCPNIFTY became monthly-only. | 2024-11-20 | ✔ | [Angel One](https://www.angelone.in/knowledge-center/futures-and-options/f-and-o-expiry-date), [share.market](https://www.share.market/buzz/insights/weekly-expiry-days-in-indian-fo-markets/) |
| X-2 | **Expiry day moved Thursday → Tuesday** for all NSE F&O contracts expiring after 2025-08-31 (first Tuesday expiry 2025-09-02). Weekly = every Tuesday; monthly = last Tuesday; if a holiday, the previous trading day. (BSE uses Thursday.) | 2025-09-01 | ✔ | [Angel One](https://www.angelone.in/news/market-updates/nifty-weekly-expiry-today-see-what-changed-on-sept-1-2025), share.market |
| X-3 | **Lot sizes, January 2026 series** (circular FAOP70616, 2025-10-03): NIFTY 75→**65**, BANKNIFTY 35→**30**, FINNIFTY 65→**60**, MIDCPNIFTY 140→**120**; NIFTYNXT50 unchanged at 25. Weekly: last old-size expiry 2025-12-23, first new 2026-01-06. Monthly: last old 2025-12-30, first new 2026-01-27. **Existing quarterly/half-yearly contracts expiring 2026-03-31 were resized after the close of 2025-12-30** — lot size changed mid-life. | 2026-01 | ✔ | [Bigul](https://bigul.co/blog/nse-implements-new-lot-sizes-for-nifty-bank-nifty-and-other-index-derivatives), [Alice Blue](https://aliceblueonline.com/blog/nifty-bank-nifty-lot-size-2026), [StockMojo](https://stockmojo.in/fno-lot-size) |
| X-4 | Earlier revision: new BANKNIFTY contracts 30→35 and MIDCPNIFTY 120→140 from 2025-04-25 (monthly contracts from the July 2025 expiry; quarterly contracts resized). The Nov–Dec 2024 revision (SEBI's ₹15 lakh minimum contract value) raised NIFTY to 75 and BANKNIFTY to 30. | 2025-04 / 2024-11 | ✔ / 2024 values ⚠ | [Business Standard](https://www.business-standard.com/amp/markets/capital-market-news/nse-revises-market-lot-sizes-for-nifty-bank-nifty-mid-select-f-o-contracts-125033100440_1.html) |
| X-5 | **Index quantity-freeze limits change often.** 2026-03-02: BANKNIFTY 600, NIFTY 1,800, FINNIFTY 1,200, MIDCPNIFTY 2,800, NIFTYNXT50 600. 2026-04-01: BANKNIFTY 900, FINNIFTY 1,800 (others unchanged). **2026-10-05: NIFTY 3,510**, and the computation methodology no longer applies to indices. Orders above the limit are rejected by the exchange. `qtyfreeze.csv` on NSE archives reportedly lags circulars. | 2026 | ✔ / 10-05 other indices ⚠ | [FAOP73531](https://nsearchives.nseindia.com/content/circulars/FAOP73531.pdf), [FAOP76693](https://nsearchives.nseindia.com/content/circulars/FAOP76693.pdf), [Angel One](https://www.angelone.in/news/market-updates/nse-announces-revised-quantity-freeze-limits-for-index-derivatives-from-march-2-2026), [tradingqna](https://tradingqna.com/t/freeze-quantity/165071) |
| X-6 | **F&O pre-open auction** 09:00–09:15 for current-month index and stock **futures** (plus next-month futures in the last 5 trading days before expiry). Not for options, far months or spreads. Order entry 09:00–09:08 with a random close between 09:07 and 09:08, then matching, then a buffer 09:12–09:15. Limit and market orders only (no SL; no IOC on NSE). | 2025-12-08 | ✔ | [Business Today](https://www.businesstoday.in/markets/stocks/story/nse-rolls-out-pre-open-session-for-equity-derivatives-from-december-8-key-things-to-know-505498-2025-12-08), [5paisa](https://tradebetter.5paisa.com/t/introduction-of-pre-open-session-in-equity-derivatives-f-o-on-nse-bse/810) |
| X-7 | **Closing Auction Session (CAS) and longer F&O hours.** F&O trades **09:15–15:40** (was 15:30). The derivatives close-price VWAP window is **15:10–15:40** (was 15:00–15:30). F&O-eligible stocks in the cash market trade continuously until **15:15**, then a call auction **15:15–15:35** sets the official close; non-CAS stocks trade until 15:30; transition 15:35–15:50; post-close 15:50–16:00. If the auction does not match, the close falls back to a reference price (VWAP of 15:00–15:15). Circulars NSE/CMTR/74466 and NSE/FAOP/74467 (2026-05-29). | 2026-08-03 | ✔ / CAS sub-phases ⚠ (secondary sources disagree; take them from the circular) | [NSE CAS page](https://www.nseindia.com/static/products-services/closing-auction-session), [Anand Rathi](https://anandrathi.com/blog/nse-extends-trading-hours), [Zerodha support](https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/what-are-pre-market-and-post-market-sessions-and-orders) |
| X-8 | **Final settlement**: index futures and options settle in cash at the **closing value of the index** on expiry day; stock F&O settle **physically** (futures delivered at the settlement price, ITM options at the strike). Closing prices: non-CAS stocks = VWAP of the last 30 minutes; CAS stocks = auction price. The index close is computed from constituents' official closes. | ongoing | ✔ | [tradingqna (NSE wording)](https://tradingqna.com/t/itm-closing-price-on-expiry/73710), [Zerodha physical settlement](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/policy-on-physical-settlement), Zerodha support (CAS) |
| X-9 | **Expiry-day extra ELM**: +2% extreme-loss margin on short **index** options expiring that day, for positions open at the start of the day and those opened during it. The calendar-spread margin benefit on expiry day was removed (Feb 2025). A later SPAN parameter file (effective 2026-01-27) reinstated a calendar-spread benefit for single-stock derivatives on expiry day. | 2024-11-20 / 2025-02 / 2026-01-27 | ✔ / last item ⚠ | [Zerodha Z-Connect](https://zerodha.com/z-connect/?p=391917), [stocko](https://stocko.in/?p=1768), [circular summary](https://rhnvrm.github.io/stock-market-circulars/circulars/nse/2026/nse-2026-02-20-2d986aef0a207209-calendar-spread-margin-benefit-for-single-stock-derivatives-on-expiry-day) |
| X-10 | Option **premium is collected upfront** from buyers (no intraday leverage on long options). | 2025-02 | ⚠ | SEBI 2024-10-01 framework (secondary summaries) |
| X-11 | Margin rules keep moving: e.g. an **additional 15% exposure margin** on F&O contracts of securities where the top 10 clients hold > 20% of MWPL. | 2026-07-01 | ✔ | [Zerodha bulletin](https://zerodha.com/marketintel/bulletin/451673/additional-exposure-margin-on-securities-under-mwpl-7) |
| X-12 | ELM file `F_AEL_OPT_CONTRACTS_DDMMYYYY.CSV` was extended (June 2026) to list **all** futures and option contracts with their ELM %. It is published on the members' extranet (`/FAOFTP/FAOCOMMON/Limit Files`); whether it is on the public website is unknown. An older `ael_DDMMYYYY.csv` (symbol-level exposure %) is described as also published on the website. | 2026-06 | ✔ existence / public URL ⚠ | [CMPT74569](https://nsearchives.nseindia.com/content/circulars/CMPT74569.pdf), [CMPT36851](https://nsearchives.nseindia.com/content/circulars/CMPT36851.pdf) |
| X-13 | SPAN risk-parameter files are published daily (begin-of-day, several intraday, end-of-day), in SPAN 4.00 format, named like `nsccl.<YYYYMMDD>.s.spn.gz` (EOD). NSE also distributes the free PC-SPAN tool. | ongoing | name ⚠ / public URL ⚠ | [NSE PC-SPAN](https://www.nseindia.com/static/products-services/equity-derivatives-pc-span), [deep dive](https://javajack.github.io/broking-kyc/deep-dives/trading-day/rms-span-methodology/) |

## 3. Charges and broker policy (Z-)

### Z-1 Current Zerodha charges (✔ [zerodha.com/charges](https://zerodha.com/charges), read 2026-10-29; the page shows no effective date)

| | Equity delivery (CNC) | Equity intraday (MIS) | Futures | Options |
|---|---|---|---|---|
| Brokerage | 0 | min(₹20, 0.03%) per executed order | min(₹20, 0.03%) per executed order | ₹20 flat per executed order |
| STT | 0.1% buy and sell | 0.025% sell | 0.05% sell | 0.15% sell (on premium); 0.15% of intrinsic value on options bought and exercised |
| NSE transaction | 0.00307% | 0.00307% | 0.00183% | 0.03553% (on premium) |
| SEBI fee | ₹10 / crore | ₹10 / crore | ₹10 / crore | ₹10 / crore |
| Stamp (buy side) | 0.015% | 0.003% | 0.002% | 0.003% |
| GST | 18% of (brokerage + SEBI + transaction) | same | same | same |

Also on the page: NSE IPFT (₹0.01/crore) is included in transaction charges; DP charge **₹15.34 per scrip** on days you sell holdings; **auto square-off ("call & trade") ₹50 + GST per order**; brokerage is also charged on expired, exercised and assigned option contracts; physical delivery brokerage **0.25%** of contract value (**0.1%** for netted positions); Kite Connect ₹500/month.

### Z-2 STT history (needed for dated `charges.json`)

| Item | Before 2023-04-01 | 2023-04-01 → 2024-09-30 | 2024-10-01 → 2026-03-31 | From 2026-04-01 | Source |
|---|---|---|---|---|---|
| Futures, sell | 0.01% ⚠ | 0.0125% | 0.02% | **0.05%** | ✔ [Outlook Money](https://www.outlookmoney.com/invest/stt-hike-from-april-1-2026-budget-what-it-means-for-futures-and-options-traders) (also reports the 2023-04-01 change), [1 Finance](https://1finance.co.in/blog/stt-futures-options-increased-budget-2026-for-fno-investors/) |
| Options premium, sell | 0.05% ⚠ | 0.0625% | 0.1% | **0.15%** | ✔ same |
| Options exercised (on intrinsic) | 0.125% ⚠ | 0.125% | 0.125% | **0.15%** | ✔ same; Zerodha page |

### Z-3 NSE transaction-charge history ⚠

Current values are in Z-1 (effective date unknown — find the Zerodha bulletin). A Zerodha pricing page captured during the 2024-10 → 2026-03 STT era showed futures **0.00173%** and options **0.03503%** on premium ([snapshot](https://zerodha.com/pricing?c=AUOGBR)); an older capture showed equity **0.00322%**. The equity rate between those (believed 0.00297%) and the switch date to the current values must be verified before backtests that span them are trusted. Until verified, `charges.json` marks those rows `"verified": false` and runs that use them are labelled (I-14).

### Z-4 Physical settlement of stock F&O at Zerodha (✔ [policy page](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/policy-on-physical-settlement), read 2026-10-29)

Delivery margin schedule (beginning of day, Tuesday expiry):

| Day | Long ITM options | Futures and short options |
|---|---|---|
| E-4 (Wed) | 10% of VaR + ELM + adhoc | normal NRML |
| E-3 (Thu) | 25% of VaR + ELM + adhoc | normal NRML |
| E-2 (Fri) | 45% of VaR + ELM + adhoc | normal NRML |
| E-1 (Mon) | 25% of contract value | normal NRML |
| Expiry (Tue) | 50% of contract value (long ITM); 25% for long OTM strikes within 1% of spot (stocks ≥ ₹100) or ₹1 (stocks < ₹100) | min(50% of contract value, 1.5 × NRML margin) |

A holiday in expiry week moves the schedule one trading day earlier. Fresh long positions in current-month OTM stock options are blocked on Monday and Tuesday of expiry week. Delivery: futures at the settlement price, ITM options at the strike (option P&L exit price 0). STT on physically settled contracts: **0.1% of contract value for buyer and seller**, plus **0.15% of intrinsic** for options bought and exercised. Equal give/take positions in the same stock are netted (brokerage 0.1% instead of 0.25%). Square-offs by Zerodha cost ₹50 + GST.

### Z-5 Intraday (MIS) rules

| Fact | Status | Source |
|---|---|---|
| Auto square-off: **equity 15:25, F&O 15:26** IST (changed 2025-12-26 from 15:20 / 15:25). No fresh MIS orders after the cut-off. | ✔ | [Z-Connect](https://zerodha.com/z-connect/updates/changes-to-the-auto-square-off-timings-for-equity-and-fo), [tradingqna](https://tradingqna.com/t/mis-auto-square-off-timings-extended/189855) |
| After CAS began (2026-08-03), several brokers moved the cut-off for CAS stocks earlier (≈15:05–15:12). Zerodha's current CAS-era timing was not confirmed. | ⚠ | [comparison page](https://onetradejournal.com/brokers/auto-square-off-times-and-charges) |
| MIS leverage: equity up to 5× (margin ≈ 20%, based on exchange VaR + ELM + adhoc); index and stock F&O 1× (full NRML). | ✔ (2024 bulletin) ⚠ current | [Zerodha bulletin](https://zerodha.com/marketintel/bulletin/249809/latest-intraday-leverages-mis-bo-co) |

## 4. Regulation (R-)

| ID | Fact | Status | Source |
|---|---|---|---|
| R-1 | SEBI's retail algo framework (circular 2025-02-04) became fully mandatory **2026-04-01**: static-IP whitelisting for order APIs, an order-rate threshold (10/s) above which strategies must be exchange-registered, mandatory market protection on market orders, daily logout. Irrelevant while STOCKIFY places no orders (D-21). | ✔ | [Taxscan](https://www.taxscan.in/top-stories/sebi-extends-timeline-for-retail-algo-trading-framework-full-implementation-from-apr-1-2026-1433982), [Kite forum](https://kite.trade/forum/discussion/comment/52152/) |

## 5. Time zones (T-)

**T-1.** IST = UTC+05:30 all year. Seattle = PDT (UTC−07:00) until **2026-11-01 02:00 local**, then PST (UTC−08:00) until the second Sunday of March 2027. An IST trading day *D* happens on the evening of *D−1* in Seattle. The table below is T-1; M5.9 extends it with every scheduled job.

| Event (IST) | Seattle while PDT | Seattle while PST |
|---|---|---|
| 06:00 token expiry (day D) | 17:30 on D−1 | 16:30 on D−1 |
| 08:30 instruments refresh | 20:00 on D−1 | 19:00 on D−1 |
| 09:00 pre-open | 20:30 on D−1 | 19:30 on D−1 |
| 09:15 open | 20:45 on D−1 | 19:45 on D−1 |
| 15:15 CAS starts (cash) | 02:45 on D | 01:45 on D |
| 15:40 F&O close | 03:10 on D | 02:10 on D |
| ~19:00 bhavcopy available ⚠ | 06:30 on D | 05:30 on D |

## 6. NSE file locations (S-)

All on `https://nsearchives.nseindia.com`. NSE may block clients without browser-like headers; fetch politely (≤ 1 request/second, retries with backoff, cache forever).

| ID | Dataset | URL pattern | Valid dates | Status |
|---|---|---|---|---|
| S-1 | F&O bhavcopy, UDiFF | `/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip` | 2024-07-08 → | ✔ pattern used in [open-source code](https://rdrr.io/github/nandp1/nser/src/R/fobhav.R); not downloaded during planning |
| S-2 | F&O bhavcopy, legacy | `/content/historical/DERIVATIVES/{YYYY}/{MON}/fo{DD}{MON}{YYYY}bhav.csv.zip` | → 2024-07-05 | ⚠ (old host `archives.nseindia.com` in old code) |
| S-3 | CM bhavcopy, UDiFF | `/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` | 2024-07-08 → | ✔ [tradingqna](https://tradingqna.com/t/has-nse-changed-bhavcopy-location/169551) |
| S-4 | CM bhavcopy, legacy | `/content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip` | → 2024-07-05 | ⚠ |
| S-5 | All index closes | `/content/indices/ind_close_all_{DDMMYYYY}.csv` | | ⚠ (two independent catalogues list it) |
| S-6 | Quantity freeze | `/content/fo/qtyfreeze.csv` (current only; lags circulars) | | ⚠ |
| S-7 | SPAN risk parameters, ELM files, CM VaR files | unknown public URLs | | 🛑 verify in a browser on `nseindia.com/all-reports-derivatives` before coding M4.8 |

UDiFF column names are expected to include `TradDt, BizDt, FinInstrmTp, TckrSymb, XpryDt, StrkPric, OptnTp, OpnPric, HghPric, LwPric, ClsPric, LastPric, PrvsClsgPric, UndrlygPric, SttlmPric, OpnIntrst, ChngInOpnIntrst, TtlTradgVol, TtlTrfVal, NewBrdLotQty` ⚠. The parser must read the header of a real file, map columns by name, and fail loudly on anything unexpected.
