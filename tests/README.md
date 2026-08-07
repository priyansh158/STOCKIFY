# STOCKIFY test suite

Offline unit tests for the Python side of STOCKIFY. **These tests never modify
application code** — everything lives under `tests/`, and the suite treats the
rest of the repo as read-only.

```bash
python3 tests/run_tests.py            # run everything
python3 tests/run_tests.py -v         # per-test names
python3 tests/run_tests.py tick_buffer   # only matching tests
python3 tests/watch.py                # re-run on every edit / git HEAD change
```

Current state: **847 tests, ~1.1s, no network, no API keys required.**

---

## Design constraints

**Nothing touches real user state.** Every on-disk store resolves its path
through `kite_client._cache_dir()`, which reads `$LOCALAPPDATA`. Importing
`tests.support` sandboxes that two ways:

1. `LOCALAPPDATA` is repointed at a fresh temp dir before any project module
   loads — for anything reading the env var directly.
2. **`kite_client._cache_dir` is replaced outright** with a function returning
   the sandbox path.

Step 2 is the authoritative one, and it exists because step 1 alone is not
enough: a test using `patch.dict(os.environ, {...}, clear=True)` wipes
`LOCALAPPDATA`, and the next DB call lands in the developer's real
`~/.cache/STOCKIFY/kite`. That happened once during development — it is now
covered by a regression test
(`SandboxIntegrityTests.test_clearing_the_environment_cannot_escape_the_sandbox`)
that clears the whole environment and asserts all four stores still resolve
inside the sandbox.

Use **`tests.support.isolated_env({...})`** instead of
`patch.dict(os.environ, ..., clear=True)` in any test whose code path can
reach the stores. `assert_sandboxed()` runs in both the setUp *and tearDown*
of every DB-backed test — tearDown matters because a test can clear the
environment mid-run.

**Nothing hits the network.** `anthropic` is stubbed only when it isn't
installed, and the stub raises on any attempted call. `yfinance` and
`kiteconnect` are deliberately *left absent* — the production code already
guards them with `AVAILABLE` flags, so their absence is what keeps the suite
offline by construction.

**Stdlib only.** `unittest`, no pytest, no new dependencies — matching the
existing `tests/gnews/` suite. Runs on the system Python 3.9 as-is.

---

## Layout

| File | Covers |
|---|---|
| `support/` | sandbox, dependency stubs, `SandboxedDBTest` base class |
| `test_module_health.py` | every module imports; public symbols exist; sandbox intact |
| `test_india_utils.py` | ₹ formatting, lakh/crore, Indian digit grouping, ticker suffixes |
| `test_kite_client.py` | token lifecycle, **06:00 IST expiry boundary**, symbol resolution |
| `test_tick_buffer.py` | tick upsert idempotency, retention, subscriptions, macro snapshot |
| `test_analysis_history.py` | save/list/get/delete, JSON round-trip fidelity, filters |
| `test_geo_alerts.py` | seen-article ledger, alert dedupe, severity filtering |
| `test_portfolio_sync.py` | holding math, **the anonymisation rule**, snapshot caching |
| `test_technical_analyst.py` | candle geometry, statistics, LLM response parsing |
| `test_prompts.py` | prompt structure, macro block, schema keys, ₹-only hygiene |
| `test_stream_claude.py` | JSON repair, friendly errors, the SSE event contract |
| `test_analysis_pipeline.py` | verdict thresholds, summary shape, state isolation |
| `test_data_helpers.py` | HTML cleaning, DataFrame normalisation, news fallback ladder |
| `test_geo_monitor.py` | query building, Gemini classifier, SMTP alerts, one worker cycle |
| `test_known_defects.py` | **confirmed bugs** — see below |
| `gnews/` | pre-existing Google News client tests (not written here) |

Two things get disproportionate coverage because they are load-bearing and
silent when broken:

- **The anonymisation rule.** `portfolio_sync`'s docstring says position data
  reaching the LLM must never be owner-attributed, or the model confirms the
  user's existing trade. That's invisible to type checkers and one copy-edit
  away from breaking, so `AnonymisationRuleTests` regex-scans the generated
  blurb for owner pronouns across P&L signs and position sizes — and asserts
  that `ui_caption_for` (which *is* owner-attributed, UI-only) stays distinct,
  catching an accidental swap of the two.

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
| 1 | `tick_buffer.candles_since()` **always** raises `OperationalError: misuse of aggregate function MIN()`; the bare `except` returns `[]`, so the live intraday chart is silently always empty | **high** |
| 2 | `technical_analyst._parse_response` raises `ValueError`/`TypeError` on `"₹2,850"`-style or `null` levels and non-numeric confidence, instead of degrading to AVOID like its other error paths | medium |
| 3 | `_compute_basic_stats` divides by zero on an all-zero-volume window or a zero opening close | low-med |
| 4 | `heuristic_suspect` matches holding tickers as bare substrings (no `\b` anchors, 2-char minimum), so `"IT"` fires inside `"digital"` — every false positive is a paid Gemini call | medium |
| 5 | `india_utils.format_inr_full` loses the carry when the fraction rounds up: `format_inr_full(0.999, decimals=2)` → `'0.100'`, expected `'1.00'` | low |
| 6 | `build_bottom_line_prompt` renders the literal `"None"` for summaries not produced by `build_summary_data()` | low |

Defect 1 in detail — the query correlates scalar subqueries against the outer
`GROUP BY` via `ts = MIN(t.ts)`, which SQLite rejects. Reproduce:

```python
tick_buffer.write_ticks([...three ticks inside one 60s bucket...])
tick_buffer.candles_since(1, 0, 60)   # []  — expected one OHLC candle
```

Fix sketch: use window functions (`FIRST_VALUE(ltp) OVER (PARTITION BY bucket
ORDER BY ts)`) for open/close, and let the exception surface or log rather than
returning `[]`.

---

## Documented-behaviour tests

Some tests pin current behaviour that is *arguably* wrong but is a design
call, not a bug. They're commented as such so a future change is deliberate
rather than accidental:

- `to_kite_symbol()` treats the `exchange` argument as authoritative and
  discards a contradicting suffix on the symbol.
- `resolve_symbol()` and the history verdict filter interpolate the query into
  a SQL `LIKE` pattern without escaping, so `%` and `_` act as wildcards.
  (Values are still parameterised — this is wildcard leakage, not injection;
  hostile-input tests confirm the tables survive.)
- `format_price()` uses Western thousands grouping while `format_inr_full()`
  uses lakh/crore pairing.
- `write_ticks()` drops a tick whose `last_price` is exactly `0.0`, and stores
  a real `0` volume as `NULL`.
- `compute_verdict()` skips a screen entirely when its stage dict is empty, so
  a technical-only run can still return BUY.

---

## Environment notes

The system Python 3.9 has `requests` but not `fastapi`/`pydantic`/`anthropic`/
`yfinance`/`kiteconnect`, so 11 modules are reported as skipped rather than
tested — `run_tests.py` prints exactly which, and why. Install the venv from
`requirements.txt` and those light up automatically with no test changes.

`stock_agent.py` and `geopolitical_risk_agent.py` additionally need **Python
3.10+**: they use PEP 604 (`X | None`) in runtime annotation positions without
`from __future__ import annotations`. Adding that import to those two files
would make them 3.9-compatible.
