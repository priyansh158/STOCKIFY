# STOCKIFY: draft of the public README section

Draft only. Not in `README.md`, not published, not pushed. The owner reads and approves every sentence below after
the live days exist (gates in `RECON.md`, M6.12). The table is pasted from `python -m stockify tools measure` run on
the sandboxed fixture lab, so every row reads "not measured yet".

## What STOCKIFY does

STOCKIFY is a simulation lab for Indian equities and derivatives. It records market data, replays it through a
deterministic C++ engine, runs strategies written in Python (or C++), and runs a paper-trading daemon on live data.
It is **paper trading only, no real orders**: nothing in it places an order with a broker.

- A backtest of the same configuration on the same recorded data gives the same result byte for byte.
- A paper session is written to a journal as it happens, and the journal can be replayed to check the stored rows.
- Costs, margin, and exchange rules come from dated configuration, not from numbers hard-coded in the code.
- Research reports say how many variants were tried, and hold back a final stretch of dates until a run asks for it.

It runs on Windows only. Whether results are identical on another operating system has not been measured.

## Architecture

```
                market data service                            live feed
        (instruments, history, quotes, margins,                  |
              charges, session)                                  |
                 |                                               |
+----------------+-----------------+   +----------------+   +----+-------------------------------+
| API server                       |   | exchange public|   | market daemon                      |
| search, portfolio, tests, trades,|   | end-of-day     |   |  ticker supervisor (raw frames)    |
| data, alerts                     |   | files          |   |   +- recorder -> tick landing       |
+------+---------------------------+   +-------+--------+   |   +- paper sessions                 |
       | starts a subprocess                   |            |   |   journal (.evt) -> engine      |
       v                                       v            |   +- scheduler: sync, compaction,   |
 python -m stockify run  <--  reference data, bars, exchange files, ticks (Python data layer)
       |                                                    |      bhavcopy, parity, reports      |
       |  run bundle (JSON) + event packs (.evt)            +------------------------------------+
       v
 deterministic C++20 engine -- artifacts --> run folder --> imported --> lab database --> UI
```

## Measured numbers

Every number is read from the lab's stores and run folders, with its sample size and source. Where the sample is
missing or too short (fewer than five live days for the parity rows), the table says "not measured yet" instead of
a number.

| Measure | Result | Sample | Source |
|---|---|---|---|
| Re-runs reproducing determinism.json byte for byte | not measured yet (no re-run with both determinism.json files: 0 so far) | 0 | runs (role rerun) and determinism.json |
| Oracle against engine on the goldens | not measured yet (no stored oracle-against-engine result on the goldens: 0 so far) | 0 | golden scenarios (oracle against engine) |
| Determinism across platforms | not measured yet (Windows only, cross-platform not measured (D-26)) | 0 | none |
| Journal parity (days passed of days) | not measured yet (5 live days: 0 so far) | 0 | parity_results (kind journal) |
| Recorder against live, median absolute NAV difference (paise) | not measured yet (5 live days: 0 so far) | 0 | parity_results (kind recorder) |
| Recorder against live, fills matched | not measured yet (5 live days: 0 so far) | 0 | parity_results (kind recorder) |
| Margin staircase error against the broker's answers (median, paise) | not measured yet (the scorecard could not be read (OperationalError)) | 0 | margin scorecard (staircase against the broker's answers) |
| Engine throughput on a full recorded day | not measured yet (no full recorded day benchmarked: 0 so far (D-48)) | 0 | engine benchmark (M3.13) |

Determinism was checked on Windows only. Cross-platform determinism is not measured.
