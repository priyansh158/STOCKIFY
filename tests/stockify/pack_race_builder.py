"""A child process for test_pack_race: build one pack spec, as an API-launched run does, and print its sha.

With `--miss-first`, the process's first look at a pack target misses it, reproducing two builders that start
together (both see no pack, both build, the second must accept the first's file)."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    spec = json.loads(sys.argv[1])
    from stockify import paths
    from stockify.data import packs
    if "--miss-first" in sys.argv:
        real = Path.exists
        state = {"seen": False}

        def first_check_misses(p: Path) -> bool:
            if p.suffix == ".evt" and p.parent == paths.packs_dir() and not state["seen"]:
                state["seen"] = True
                return False
            return real(p)
        Path.exists = first_check_misses
    print(packs.build(spec))


if __name__ == "__main__":
    main()
