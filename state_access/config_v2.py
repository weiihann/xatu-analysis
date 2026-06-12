"""Configuration for the v2 hot/cold state analysis.

The anchor is capped by the source table with the shortest local coverage. On the primary
cluster diffs end at block ~24.87M while reads + `address_appearances` reach ~25.19M, so
the common envelope's natural round anchor is 24,870,000 — same as the original
`analysis.py`. A longer-reach anchor would need a diffs backfill first.
"""

from __future__ import annotations


from state_access.config import DATA_DIR

ANCHOR_BLOCK_V2 = 24_870_000

# First post-merge block. The full-history sweep sources read tables from ethpandaops
# below this (local `_reads` coverage starts at the merge) and from the local node above.
MERGE_BLOCK = 15_537_394

# Trailing-window lengths (days).
WINDOWS_V2 = [1, 7, 14, 30, 60, 90, 180, 365]

# Historical sweep grid: weekly anchors descending from the snapshot anchor, floored so
# each window's whole lookback stays post-merge (local read coverage starts at the
# merge; 7,200 blocks/day holds from there).
SWEEP_WINDOWS = [30, 90, 180, 365]
SWEEP_STEP = 50_400  # 7 days * 7,200 blocks/day


def anchors_v2(window_days: int) -> list[int]:
    """Weekly anchor blocks for `window_days`, ascending; last == ANCHOR_BLOCK_V2."""
    floor = MERGE_BLOCK + window_days * 7_200
    out: list[int] = []
    block = ANCHOR_BLOCK_V2
    while block >= floor:
        out.append(block)
        block -= SWEEP_STEP
    return sorted(out)

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
