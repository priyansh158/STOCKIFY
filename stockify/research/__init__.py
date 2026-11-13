"""Research tools (M6): config, seeding and file helpers shared by the honest-backtest tools."""
from __future__ import annotations

from stockify.config import loader


def settings() -> dict:
    """research.json, loaded and validated (ConfigError on a bad file)."""
    return loader.load_json("research.json")
