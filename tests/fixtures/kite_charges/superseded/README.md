Captures kept as evidence but not used by G08:

- 2026-11-06_one_request_all_cases.json: all cases in one request. Kite netted the INFY CNC buy and sell as
  an intraday round trip (brokerage 0.03 %, STT 0.025 % on the sell, no delivery STT), so the CNC cases
  were priced as MIS. The tool now sends one order per request unless cases share a request group.
