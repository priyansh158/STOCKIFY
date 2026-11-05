# STOCKIFY simulation-lab plan — start here

This folder is the execution plan for turning **STOCKIFY** into an NSE simulation lab for testing equity, futures and options strategies with Kite Connect data, a C++ engine core, and paper trading.

It is written for two readers:

- **Claude Code**, which executes it milestone by milestone. Every task names files, interfaces, tests and a check that proves it is done.
- **The owner**, who decides at the 🛑 gates and who should understand *why* each piece exists. The "why" lives in `02_concepts.md`.

Plan version: 1.0 · written 2026-10-29 · facts verified 2026-10-29 (see `01_facts.md`).

---

## File index

| File | What it is | When to read it |
|---|---|---|
| `README.md` | This page: how to execute the plan | Always, first |
| `00_overview.md` | Goals, invariants, architecture, decision log | Always, second |
| `01_facts.md` | External facts the code depends on (Kite, NSE, charges, Zerodha policy), dated and sourced | Before any task that touches market rules |
| `02_concepts.md` | The finance and engineering ideas behind the design, explained in depth | When a task says "see concept C-x", or when something seems arbitrary |
| `M0_cleanup_toolchain.md` | Remove the LLM pipeline, set up the C++/pybind toolchain | Milestone 0 |
| `M1_data_layer.md` | Reference data, NSE files, Kite bars, the tick recorder, storage, event packs | Milestone 1 |
| `M2_python_oracle.md` | A small, readable Python backtester used for research now and as the correctness oracle later | Milestone 2 |
| `M3_engine_core.md` | The C++ engine: clock, orders, fills, costs, ledger, metrics, strategy API, runners, test-run UI | Milestone 3 |
| `M4_fno.md` | Futures and options: contract specs, Black-76, settlement, margin models | Milestone 4 |
| `M5_paper_daemon.md` | Unattended paper sessions, journals, parity, daily login, deployment | Milestone 5 |
| `M6_reporting_research.md` | Comparing runs, data coverage, research hygiene (walk-forward, trial counts) | Milestone 6 |
| `A1_schemas.md` | Every SQL table, config file, binary format and artifact layout | Whenever you create or read data |
| `A2_interfaces.md` | C++ headers, the Python binding surface, the strategy API, REST endpoints | Whenever you write engine, binding or API code |
| `A3_testing.md` | Test strategy, golden scenarios, parity gates, CI | Whenever you write tests |
| `A4_engine_semantics.md` | **The contract**: exactly how events, orders, fills, costs, ledger and settlement behave. Both the Python oracle and the C++ engine implement it | Before any M2, M3 or M4 task |

Created during execution (not part of this plan drop): `RECON.md` (what the code actually looks like, appended per milestone) and `HOWTO_strategy.md` (M3.15).

## Milestone order and dependencies

```
M0 cleanup + toolchain
 └─► M1 data layer + recorder  (start recording as early as possible — history only accrues from day 1)
      ├─► M2 Python oracle backtester  (research starts here)
      │    └─► M3 C++ engine core (equity) ──► M4 F&O ──► M5 paper daemon ──► M6 reporting + research
      └──────────────────────────────────────────────────► (M5 reuses the M1 recorder and ticker)
```

M1.5 (the recorder) should go live as soon as M1.0–M1.2 pass, even if the rest of M1 is unfinished. Every market day without it is a day of option-chain history that can never be recovered (Kite keeps no data for expired options).

## Execution protocol for Claude Code

1. **Read narrowly.** For a milestone, read `README.md`, `00_overview.md`, the milestone file, and only the appendix sections it links. Read `01_facts.md` rows when a task references them. Do not load every file at once.
2. **Recon first.** Each milestone starts with a recon task. Confirm that the files, functions and line numbers the plan mentions still exist. Write what you found in `docs/plan/RECON.md` (append a dated section). Line numbers in this plan are hints from an earlier snapshot of the repo; trust the code.
3. **One branch per milestone**: `stockify/m1-data`, `stockify/m3-engine`, and so on. Commit titles follow the football-position roster (`00_overview.md` §5a, D-31): `<POS> <milestone>.<n>: summary`, where `n` counts that position's commits in the milestone, e.g. `LWB 1.1: ingest UDiFF F&O bhavcopy`, then `LWB 1.2: …` for the next change to the same stage. Team-wide work uses `XI`.
4. **Task loop**: implement → write the tests the task lists → run the task's verification commands → tick the box in the milestone file (`- [ ]` → `- [x]`) in the same commit.
5. **🛑 STOP gates**: stop, summarise what you found and what you propose, and wait for the owner. Never skip a gate because a workaround exists.
6. **⚠ VERIFY facts**: anything marked ⚠ in `01_facts.md` must be checked against its source before code depends on it. Record the evidence (URL, date, what you saw) next to the fact.
7. **Deviations**: if the plan is wrong or impossible, add a decision entry (`D-xx`) to `00_overview.md` explaining what changed and why, in the same commit as the change.
8. **Invariants are not negotiable.** Never weaken an invariant from `00_overview.md` §2 to make a test pass. If an invariant blocks you, that is a 🛑.
9. **Definition of done**: a milestone is done when every box is ticked, every verification command passes, and its demo script runs. Then update `00_overview.md` §4 (status column).

## Prompts the owner can paste into Claude Code

Start a milestone:

```
Read docs/plan/README.md, docs/plan/00_overview.md and docs/plan/M1_data_layer.md,
plus the A1/A2/A3 sections that M1 links. Use plan mode first: run the M1 recon task and
show me what differs from the plan. After I approve, execute M1 tasks in order, ticking
boxes as you go. Stop at every 🛑 gate.
```

Resume after a break:

```
Continue STOCKIFY milestone M3 from docs/plan/M3_engine_core.md. Find the first unticked
task, re-run the verification commands of the last ticked task to confirm the tree is green,
then continue.
```

Check a fact before relying on it:

```
Before implementing M4.7, verify every ⚠ row in docs/plan/01_facts.md that margin work
depends on (X-9 to X-13 in §2, Z-3 to Z-5 in §3) against its source and update the rows with
evidence. Stop if anything changed.
```

## Conventions used in every file

- **🛑** = stop and ask the owner. **⚠** = verify before relying on it.
- Paths are relative to the repo root. Runtime data lives under `stockify_paths.state_dir() / "lab"`, written here as `$LAB/`.
- "IST" is India Standard Time (UTC+05:30, no daylight saving). All stored timestamps are UTC epoch nanoseconds. See invariant I-4.
- Money in the engine is integer paise (₹1 = 100 paise). See invariant I-3.
- Sizes: **S** (an evening), **M** (a few sessions), **L** (a week or more of part-time work). They are rough and only meant for ordering work.
