"""Configuration for the post-Merge historical sweep of state_access.

Anchors step weekly (7 * 7,200 blocks) over the post-Merge range, generated descending
from END_BLOCK so the final anchor coincides with the static run. Each window W has its
own anchor floor so the whole lookback stays post-Merge (where 7,200 blocks == 1 day),
and its own output parquet, so sweeps at different W never overwrite each other.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from state_access.config import DATA_DIR

END_BLOCK = 24_870_000    # matches the static analysis anchor
STEP = 50_400             # 7 days * 7,200 blocks/day (weekly)
DEFAULT_W = 30            # default active-window, in days

MERGE_BLOCK = 15_537_394  # The Merge (first PoS block); 7,200 blocks/day holds from here on
MERGE_TS = 1_663_224_179  # 2022-09-15 06:42:59 UTC, block 15,537,394
SECONDS_PER_BLOCK = 12

# Fork boundary blocks, for chart annotations.
FORKS = {
    "Shanghai": 17_034_870,
    "Dencun": 19_426_587,
    "Pectra": 22_431_084,
    "Fusaka": 23_935_694,
}


def start_block(w: int) -> int:
    """Lowest anchor whose W-day lookback window stays entirely post-Merge.

    An anchor's window reaches back (W + 1) days (the 1-day "today" window plus the
    W-day warm set), so the earliest valid anchor is that far above the Merge block.
    """
    return MERGE_BLOCK + (w + 1) * 7_200


def anchors(w: int = DEFAULT_W) -> list[int]:
    """Weekly anchor blocks for window `w`, ascending; last == END_BLOCK."""
    floor = start_block(w)
    out: list[int] = []
    block = END_BLOCK
    while block >= floor:
        out.append(block)
        block -= STEP
    return sorted(out)


def parquet_for(w: int = DEFAULT_W) -> Path:
    """Output parquet path for a window's sweep (e.g. data/history_w90.parquet)."""
    return DATA_DIR / "v1" / f"history_w{w}.parquet"


def block_to_date(block: int) -> datetime:
    """Deterministic post-Merge block → UTC datetime (12s cadence; missed slots add drift)."""
    ts = MERGE_TS + (block - MERGE_BLOCK) * SECONDS_PER_BLOCK
    return datetime.fromtimestamp(ts, tz=timezone.utc)
