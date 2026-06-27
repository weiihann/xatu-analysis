"""Configuration for the state_access replication.

All windows are expressed in days and converted to block ranges via the
post-Merge cadence of 7,200 blocks/day (12 s/block).
"""

from __future__ import annotations

from pathlib import Path

# Anchor: the "now" block every window counts back from.
ANCHOR_BLOCK = 24_870_000
BLOCKS_PER_DAY = 7_200
NETWORK = "mainnet"

# Window lengths (days) per analysis. Step 1 is run at the union of every window
# any chart needs, so pct_state_warm is measured directly rather than interpolated.
STATE_WINDOWS = [1, 2, 4, 7, 8, 14, 16, 30, 32, 60, 64, 90, 128, 180]
ACCOUNT_WRITE_WINDOWS = [1, 7, 14, 30, 60, 90]
STORAGE_WRITE_WINDOWS = [1, 7, 14, 30, 60]
UPDATE_WINDOWS = [1, 7, 14, 30, 60]

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR_V1 = DATA_DIR / "v1"
