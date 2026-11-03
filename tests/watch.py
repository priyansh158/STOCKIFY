r"""Re-run the test suite whenever the repo changes.

    .\.venv\Scripts\python.exe tests\watch.py
    .\.venv\Scripts\python.exe tests\watch.py tick_buffer

Built for a repo taking constant pushes: it polls the mtimes of every tracked
.py file plus the current git HEAD, so it fires on your own edits *and* on a
`git pull` / `git checkout` that moves the branch under you.

Output is one line per run — green when the suite is clean, followed by the
failure detail when it is not. Ctrl-C to stop.

Read-only: it never writes to the repo and never touches git state.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".next", "web",
             ".venv", "venv", ".stockify_cache"}

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
)


def snapshot() -> tuple:
    """A cheap fingerprint of the working tree: (py file mtimes, git HEAD)."""
    stamps = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        try:
            stamps.append((str(path), path.stat().st_mtime_ns))
        except OSError:
            continue
    return (tuple(sorted(stamps)), git_head())


def git_head() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ""


def short_head() -> str:
    return git_head()[:7] or "no-git"


def run_suite(pattern: str) -> tuple:
    cmd = [sys.executable, "-W", "ignore", str(REPO_ROOT / "tests" / "run_tests.py")]
    if pattern:
        cmd.append(pattern)
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def summarise(stdout: str) -> str:
    """Pull the count lines out of the runner's report."""
    interesting = ("passed", "skipped", "tracked defects", "FAILURES", "ERRORS", "FIXED")
    bits = []
    for line in stdout.splitlines():
        stripped = line.strip()
        for key in interesting:
            if stripped.startswith(key):
                value = stripped[len(key):].strip().split()[0]
                bits.append(f"{key.lower()} {value}")
                break
    return ", ".join(bits)


def emit(text: str = "") -> None:
    # flush so the watcher stays readable when piped to a log or a split pane.
    print(text, flush=True)


def report(code: int, stdout: str, stderr: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    summary = summarise(stdout) or "no tests ran"
    if code == 0:
        emit(f"{GREEN}✓{RESET} {stamp} {DIM}{short_head()}{RESET}  {summary}")
    elif code == 2:
        emit(f"{YELLOW}★{RESET} {stamp} {DIM}{short_head()}{RESET}  {summary}")
        emit(stdout.rstrip())
    else:
        emit(f"{RED}✗{RESET} {stamp} {DIM}{short_head()}{RESET}  {summary}")
        detail = stderr.strip() or stdout.strip()
        for line in detail.splitlines():
            if line.startswith(("FAIL:", "ERROR:")) or "AssertionError" in line \
                    or line.startswith("  File "):
                emit(f"    {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-run the suite on every change.")
    parser.add_argument("pattern", nargs="?", default="",
                        help="only run tests whose id contains this substring")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="poll interval in seconds (default 1.0)")
    args = parser.parse_args()

    scope = f" [{args.pattern}]" if args.pattern else ""
    emit(f"{DIM}watching {REPO_ROOT}{scope} — Ctrl-C to stop{RESET}")

    last = None
    try:
        while True:
            current = snapshot()
            if current != last:
                last = current
                report(*run_suite(args.pattern))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        emit(f"\n{DIM}stopped{RESET}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
