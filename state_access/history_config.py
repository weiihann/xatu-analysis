"""Configuration for the post-Merge historical sweep of state_access at W=30.

Anchors step weekly (7 * 7,200 blocks) over the post-Merge range, generated
descending from END_BLOCK so the final anchor coincides with the static run.
"""

from __future__ import annotations

from datetime import datetime, timezone

from state_access.config import DATA_DIR

START_BLOCK = 15_537_394  # The Merge (first PoS block)
END_BLOCK = 24_870_000    # matches the static analysis anchor
STEP = 50_400             # 7 days * 7,200 blocks/day (weekly)
W = 30                    # fixed active-window, in days

MERGE_BLOCK = 15_537_394
MERGE_TS = 1_663_224_179  # 2022-09-15 06:42:59 UTC, block 15,537,394
SECONDS_PER_BLOCK = 12

# Fork boundary blocks, for chart annotations.
FORKS = {
    "Shanghai": 17_034_870,
    "Dencun": 19_426_587,
    "Pectra": 22_431_084,
}

HISTORY_PARQUET = DATA_DIR / "history_w30.parquet"


def anchors() -> list[int]:
    """Anchor blocks, weekly across the post-Merge range, ascending; last == END_BLOCK."""
    out: list[int] = []
    block = END_BLOCK
    while block >= START_BLOCK:
        out.append(block)
        block -= STEP
    return sorted(out)


def block_to_date(block: int) -> datetime:
    """Deterministic post-Merge block → UTC datetime (12s cadence; missed slots add drift)."""
    ts = MERGE_TS + (block - MERGE_BLOCK) * SECONDS_PER_BLOCK
    return datetime.fromtimestamp(ts, tz=timezone.utc)
