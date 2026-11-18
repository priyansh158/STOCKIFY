"""The fundamentals screener (FS): today's liquid Nifty 500 stocks compared with their sector from NSE's filings.

store     fundamentals.sqlite (universe, sectors; later tasks add filings and figures)
nse_api   the JSON client for www.nseindia.com/api over nse_files.Http, raw answers kept with their sha256
universe  today's Nifty 500 above the liquidity floor, and NSE's sector classification per stock
"""
