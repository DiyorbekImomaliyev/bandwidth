"""Where things live. Overridable with BW_DATA_DIR so tests never touch real data."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = Path(os.environ.get("BW_DATA_DIR") or ROOT / "data")
AGENT_DB = DATA_DIR / "agent.db"
