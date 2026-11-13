/**
 * Every terminal command the UI shows the owner, from one place. They run in a terminal in the STOCKIFY folder, with the
 * project's own Python: plain `python` on this machine lacks the time zone data, so a bare `python -m stockify` fails.
 * tests/stockify/test_ui_commands.py fails when any file under web/src spells the interpreter another way.
 */
export const PYTHON = ".\\.venv\\Scripts\\python.exe";

/** `.\.venv\Scripts\python.exe -m stockify <args>` */
export function stockifyCommand(args: string): string {
  return `${PYTHON} -m stockify ${args}`;
}

/** The sweep file the repo ships; a run of one needs a file, and this one exists. */
export const EXAMPLE_SWEEP = "examples/sweeps/orb_grid.yaml";

/** Starts the market daemon alone (it holds the recorder). */
export const START_RECORDER = ".\\run.ps1 -DaemonOnly";

/** Download the NSE files of one IST day; `day` is a YYYY-MM-DD date or a placeholder. */
export function nseFetchCommand(day: string): string {
  return stockifyCommand(`nse fetch --date ${day}`);
}

/** Download every NSE trading day from `from` to `to` (inclusive, YYYY-MM-DD); days already fetched are skipped (resumable). */
export function nseBackfillCommand(from: string, to: string): string {
  return stockifyCommand(`nse backfill --from ${from} --to ${to}`);
}

export const SWEEP_COMMAND = stockifyCommand(`sweep --config ${EXAMPLE_SWEEP}`);
export const WALKFORWARD_COMMAND = stockifyCommand(`walkforward --sweep ${EXAMPLE_SWEEP}`);

export function sensitivityCommand(runId: string | number): string {
  return stockifyCommand(`sensitivity --run ${runId}`);
}
