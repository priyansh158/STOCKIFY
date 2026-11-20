"""FS 6: the test record. The rule "hold the best-placed stocks, rebalanced monthly" run out of sample with the
existing walk-forward (spec "The test record", "Calls made" 1 and 9).

`run(end=...)`:
1. the ranking history (history.rankings) from START (2022-01-01) to `end`, at most the day before the holdout
   (research.json holdout_from; a later end is refused before anything is fetched);
2. Kite's adjusted daily candles for every ranked symbol and the benchmark, through the nightly candles' top-up
   (`stockify.data.history.top_up_daily`, CW 1b) over the fewest whole years from `end` that reach START, with
   today's Kite token, so a stock the nightly job already holds (further back) asks for nothing but the adjustment
   check and the end's day; a Kite token is needed; a symbol refdata does not know or Kite cannot give is left out (`missing`; the strategy skips it);
3. the base run config: the Fundamentals basket on a bars pack of those symbols, the daily-bar fill model and costs
   of examples/runs/sma_reliance_daily.json (bar fills, 10 % participation, 5 bp slippage; Zerodha costs);
4. the walk-forward over balance x basket size (fundamentals.json balances, basket_sizes): train 365 days, test 90,
   step 90, objective Sharpe;
5. nothing else to keep: the walk-forward's own lab.sqlite research_jobs row (kind walkforward, name NAME, its
   family, status and span) is the record; `store.latest_test_record()` reads the newest one for the page. No
   fundamentals.sqlite table, so no migration.

Benchmark (call 9): NSE Indices publishes the Nifty 500 total-return history only through its report form
(niftyindices.com/reports/historical-data, checked 2026-11-17), not as a file, so the benchmark is Kite's
"NIFTY 500" index candles, labelled "price index, without dividends".
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from stockify.data import history as bars_history
from stockify.data import instruments
from stockify.fundamentals import history, store
from stockify.research import holdout, sweep, walkforward

log = logging.getLogger("stockify.fundamentals.test_record")

START = date(2022, 1, 1)
TRAIN, TEST, STEP = 365, 90, 90
OBJECTIVE = "sharpe"
NAME = store.TEST_RECORD_NAME                       # the job the page finds (store.latest_test_record)
REF = "stockify.strategies.fundamentals_basket:FundamentalsBasket"
CAPITAL_PAISE = 1_000_000_000                       # ₹1 crore: a share of even a ₹1 lakh stock in a 30-stock basket
BENCHMARK = {"symbol": store.TEST_RECORD_BENCHMARK[0], "label": store.TEST_RECORD_BENCHMARK[1], "source": "kite"}
FILL_MODEL = {"kind": "bar", "limit_touch_policy": "through", "participation": "0.10",
              "slippage": {"kind": "bps", "x": "0.0005"}}
COSTS = {"broker": "zerodha", "allow_unverified": True, "auto_square_off_fee": True, "dp_charges": True}


def default_end() -> date:
    """The day before the holdout."""
    hold = holdout.holdout_from()
    if hold is None:
        raise ValueError("research.json has no holdout_from: the test record needs one to end before")
    return hold - timedelta(days=1)


def base_config(ranking: dict, symbols: list[str], end: date) -> dict:
    labels = sorted(ranking)
    return {
        "schema": "stockify.run/1", "name": NAME,
        "strategy": {"kind": "python", "ref": REF,
                     "params": {"ranking": ranking, "balance": "0.5" if "0.5" in ranking else labels[0],
                                "basket_size": 20}},
        "data": {"packs": [{"spec": {"dataset": "bars", "interval": "day",
                                     "instruments": [{"symbols": [f"NSE:{s}" for s in symbols]}],
                                     "from": START.isoformat(), "to": end.isoformat()}}]},
        "price_basis": "adjusted", "capital_paise": CAPITAL_PAISE, "fill_model": dict(FILL_MODEL), "latency_ms": 0,
        "costs": dict(COSTS), "margin": {"model": "flat"},
        "policies": {"preopen": "queue", "cas": "none", "freeze": "slice", "max_slices": 10, "rms_square_off": False},
        "mark": "ltp", "seed": 42, "unlock_holdout": None}


def years_needed(end: date) -> int:
    """The fewest whole years before `end` that reach START."""
    years = 1
    while bars_history.years_before(end, years) > START:
        years += 1
    return years


def _fetch_all(symbols: list[str], end: date, top_up, client) -> tuple[list[str], list[str]]:  # noqa: ANN001
    """(symbols with bars, symbols without), each sorted."""
    from stockify import timeutil
    years, today = years_needed(end), timeutil.ist_today()
    have, missing = [], []
    for s in symbols:
        iid = instruments.find("NSE", s)
        if iid is None:
            missing.append(s)
            continue
        try:
            res = top_up(iid, end, years=years, client=client, today=today)
        except Exception as e:  # noqa: BLE001 — one stock Kite cannot give is left out, the rest go on
            log.warning("no bars for %s: %s", s, type(e).__name__)
            missing.append(s)
            continue
        (have if res.outcome == "ok" else missing).append(s)
    return sorted(have), sorted(missing)


def run(*, end: date | None = None, client=None, rank=history.rankings, top_up=bars_history.top_up_daily,  # noqa: ANN001
        walk=walkforward.run_windows, now_ns: int | None = None) -> dict:
    """Run the test record (see the module docstring). Returns {"job_id", "family_id", "start", "end", "symbols",
    "missing", "benchmark"}. ValueError before anything is fetched for an end in the holdout."""
    last = default_end()
    end = last if end is None else end
    if end > last:
        raise ValueError(f"the test record must end before the holdout ({last + timedelta(days=1)}): "
                         f"{end.isoformat()} is in it")
    if end < START + timedelta(days=TRAIN + TEST - 1):
        raise ValueError(f"{end.isoformat()} leaves no room for one window of {TRAIN} + {TEST} days")
    if client is None:
        from stockify.data.history import _client
        client = _client()                         # one Kite client for every symbol
    ranking = rank(START, end)
    ranked = sorted({s for entries in ranking.values() for e in entries for s in e["symbols"]})
    symbols, missing = _fetch_all(ranked, end, top_up, client)
    if not symbols:
        raise ValueError("no ranked stock has daily bars: nothing to test")
    bench = instruments.find("NSE", BENCHMARK["symbol"].split(":", 1)[1])
    if bench is None:
        log.warning("the benchmark %s is not in refdata", BENCHMARK["symbol"])
    else:
        from stockify import timeutil
        top_up(bench, end, years=years_needed(end), client=client, today=timeutil.ist_today())
    for s in missing:
        log.warning("%s is ranked but has no daily bars: the basket skips it", s)
    spec = sweep.build_spec(NAME, base_config(ranking, symbols, end),
                            {"strategy.params.balance": history.balances(),
                             "strategy.params.basket_size": history.basket_sizes()}, objective=OBJECTIVE)
    result = walk(spec, train=TRAIN, test=TEST, step=STEP, objective=OBJECTIVE, start=START, end=end,
                  now_ns=now_ns)
    log.info("test record: walk-forward %s over %d stocks (%d ranked without bars)", result.job_id, len(symbols),
             len(missing))
    return {"job_id": result.job_id, "family_id": result.family_id, "start": START.isoformat(),
            "end": end.isoformat(), "symbols": symbols, "missing": missing, "benchmark": dict(BENCHMARK)}
