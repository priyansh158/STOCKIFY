#!/bin/bash
# Launchd-friendly entrypoint for the STOCKIFY geo monitor.
set -euo pipefail

ROOT="/Users/mac/Developer/STOCKIFY"
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

exec /usr/bin/python3 -m geo_monitor "$@"
