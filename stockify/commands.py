"""The one spelling of a terminal command shown to the owner (review I2).

Commands run in a terminal in the STOCKIFY folder with the project's own Python: plain `python` on this machine
lacks the time zone data, so a bare `python -m stockify` fails. Every user-facing string in server/ and stockify/ that
names a command builds it here. web/src/lib/commands.ts holds the same interpreter path for the web pages;
tests/stockify/test_ui_commands.py fails when the two differ or when any string spells the bare form.
"""
from __future__ import annotations

VENV_PYTHON = r".\.venv\Scripts\python.exe"


def stockify_command(args: str) -> str:
    r"""`.\.venv\Scripts\python.exe -m stockify <args>`"""
    return f"{VENV_PYTHON} -m stockify {args}"
