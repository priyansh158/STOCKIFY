# STOCKIFY test suite

Offline unit tests for the Python side of STOCKIFY. **These tests never modify
application code** — everything lives under `tests/`, and the suite treats the
rest of the repo as read-only.

```powershell
.\.venv\Scripts\python.exe tests\run_tests.py
.\.venv\Scripts\python.exe tests\run_tests.py -v
.\.venv\Scripts\python.exe tests\run_tests.py tick_buffer
.\.venv\Scripts\python.exe tests\run_tests.py --python-only
.\.venv\Scripts\python.exe tests\watch.py
```

The engine tests need the C++ module (`.\run.ps1 -BuildEngine`). If it is missing, or was built for another Python,
those tests skip and the run **exits 3** with an "ENGINE NOT LOADABLE" banner, so a skipped engine never looks green.
Pass `--python-only` (or set `STOCKIFY_TESTS_PYTHON_ONLY=1`) when a Python-only run is what you want.

The suite is credential-free and redirects local application state into a
temporary Windows sandbox.

---

## Design constraints

**Nothing touches real user state.** Every on-disk store resolves its path
through `stockify_paths.state_dir()`, which reads `%LOCALAPPDATA%`. Importing
`tests.support` sandboxes that two ways:

1. `LOCALAPPDATA` is repointed at a fresh temp dir before any project module
   loads — for anything reading the env var directly.
2. **`stockify_paths.state_dir` and `logs_dir` are replaced outright** with
   functions returning sandbox paths.

Step 2 is the authoritative one, and it exists because step 1 alone is not
enough: a test using `patch.dict(os.environ, {...}, clear=True)` wipes
`LOCALAPPDATA`. The path authority now fails safely instead of falling back to
another platform's home-directory convention. This is covered by
(`SandboxIntegrityTests.test_clearing_the_environment_cannot_escape_the_sandbox`)
that clears the whole environment and asserts all four stores still resolve
inside the sandbox.

Use **`tests.support.isolated_env({...})`** instead of
`patch.dict(os.environ, ..., clear=True)` in any test whose code path can
reach the stores. `assert_sandboxed()` runs in both the setUp *and tearDown*
of every DB-backed test — tearDown matters because a test can clear the
environment mid-run.

**Nothing hits the network.** `yfinance` and
`kiteconnect` are deliberately *left absent* — the production code already
guards them with `AVAILABLE` flags, so their absence is what keeps the suite
offline by construction.

**Stdlib runner.** `unittest`, no pytest. Run it from the production Python
3.11/3.12 venv so import coverage has no dependency-driven skips.

---

## Layout

| File | Covers |
|---|---|
| `support/` | sandbox, `SandboxedDBTest` base class |
| `test_module_health.py` | every module imports; public symbols exist; sandbox intact |
| `test_india_utils.py` | ₹ formatting, lakh/crore, Indian digit grouping, ticker suffixes |
| `test_kite_client.py` | token lifecycle, **06:00 IST expiry boundary**, symbol resolution |
| `test_tick_buffer.py` | tick upsert idempotency, retention, subscriptions |
| `test_geo_alerts.py` | seen-article ledger, alert dedupe, severity filtering |
| `test_portfolio_sync.py` | holding math, snapshot aggregates, fetching and caching |
| `test_geo_monitor.py` | query building, Gemini classifier, SMTP alerts, one worker cycle |
| `test_known_defects.py` | **confirmed bugs** — see below |
| `stockify/` | STOCKIFY paths, exact numerics + canonical output, config loader rules, `lab.sqlite` schema, CLI, engine gateway |
| `gnews/` | pre-existing Google News client tests (not written here) |

One thing gets disproportionate coverage because it is load-bearing and
silent when broken:

- **The 06:00 IST token boundary.** Pinned from both sides, including the
  before-06:00 case where *yesterday's* boundary applies, and a UTC-vs-IST
  check so the comparison can't silently drift to server time.

---

## Tracked defects

`test_known_defects.py` holds bugs found while writing the suite. Each is
written as the behaviour that *should* hold and marked
`@unittest.expectedFailure`, so:

- while the bug exists → recorded as an expected failure, **suite stays green,
  your pushes are never blocked**;
- when someone fixes it → unittest reports an *unexpected success* and
  `run_tests.py` exits **2**, telling you to delete the decorator and promote
  the test.

It's a to-do list that tells you when it's done.

| # | Defect | Severity |
|---|---|---|
| 1 | `india_utils.format_inr_full` loses the carry when the fraction rounds up: `format_inr_full(0.999, decimals=2)` → `'0.100'`, expected `'1.00'` | low |

The analysis-pipeline defects tracked here earlier (and `tick_buffer.candles_since`)
were removed together with their code in milestone M0.

---

## Documented-behaviour tests

Some tests pin current behaviour that is *arguably* wrong but is a design
call, not a bug. They're commented as such so a future change is deliberate
rather than accidental:

- `to_kite_symbol()` treats the `exchange` argument as authoritative and
  discards a contradicting suffix on the symbol.
- `resolve_symbol()` interpolates the query into
  a SQL `LIKE` pattern without escaping, so `%` and `_` act as wildcards.
  (Values are still parameterised — this is wildcard leakage, not injection;
  hostile-input tests confirm the tables survive.)
- `format_price()` uses Western thousands grouping while `format_inr_full()`
  uses lakh/crore pairing.
- `write_ticks()` drops a tick whose `last_price` is exactly `0.0`, and stores
  a real `0` volume as `NULL`.

---

## Environment notes

The supported test environment is the same Python 3.11/3.12 venv used by the
application. A skipped project module is treated as an incomplete local setup,
not as a successful migration verification.
