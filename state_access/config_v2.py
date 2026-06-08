"""Configuration for the v2 hot/cold state analysis.

The anchor is capped by the source table with the shortest local coverage. On the primary
cluster diffs end at block ~24.87M while reads + `address_appearances` reach ~25.19M, so
the common envelope's natural round anchor is 24,870,000 — same as the original
`analysis.py`. A longer-reach anchor would need a diffs backfill first.
"""

from __future__ import annotations

from pathlib import Path

from state_access.config import DATA_DIR

ANCHOR_BLOCK_V2 = 24_870_000

# Trailing-window lengths (days).
WINDOWS_V2 = [1, 7, 14, 30, 60, 90, 180, 365]

# Fixed bins for Q2 (access-frequency tail). Each entry is (lo, hi, label) where lo/hi are
# inclusive bounds on the per-key access count. `None` for `hi` means open-ended.
BINS = [
    (1, 1, "1"),
    (2, 5, "2-5"),
    (6, 50, "6-50"),
    (51, 500, "51-500"),
    (501, None, "500+"),
]

DATA_DIR_V2 = DATA_DIR / "v2"
