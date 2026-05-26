"""Configuration for the state_delta analysis.

How Ethereum live-state *size* grows over time, from the per-block snapshots in
execution_state_size (counts + byte sizes for accounts, storage, contract code).

No single client reports execution_state_size across the whole post-Merge range, so the
daily/per-block series is stitched from two: ``EARLY_CLIENT`` (a full backfill of the early
post-Merge era) below ``SEAM_BLOCK`` and ``RECENT_CLIENT`` (a live node) at/above it. The
two agree closely in their overlap (accounts exact, storages ~2e-6, bytes match) apart from
a ~1.7% step in contract_codes, which is documented as a known seam artifact.
"""

from __future__ import annotations

from pathlib import Path

# Post-Merge cadence + block->date mapping are shared with the state_access sweep.
from state_access.history_config import (  # re-exported for convenience
    FORKS,
    MERGE_BLOCK,
    MERGE_TS,
    SECONDS_PER_BLOCK,
    block_to_date,
)

__all__ = [
    "FORKS", "MERGE_BLOCK", "MERGE_TS", "SECONDS_PER_BLOCK", "block_to_date",
    "NETWORK", "BLOCKS_PER_DAY", "START_BLOCK", "SEAM_BLOCK",
    "EARLY_CLIENT", "RECENT_CLIENT", "CLIENTS",
    "ANCHOR_BLOCK", "ANCHOR_ACCOUNTS", "ANCHOR_STORAGES",
    "COUNT_COLS", "BYTE_COLS", "METRIC_COLS", "DATA_DIR",
]

NETWORK = "mainnet"
BLOCKS_PER_DAY = 7_200
START_BLOCK = MERGE_BLOCK          # 15,537,394 — first PoS block; 7,200 blocks/day holds here on

# Client stitch seam: EARLY_CLIENT below, RECENT_CLIENT at/above.
SEAM_BLOCK = 23_000_000
EARLY_CLIENT = "ethpandaops/mainnet/manual-backfill"
RECENT_CLIENT = "ethpandaops/mainnet/utility-mainnet-prysm-geth-tysm-002"
CLIENTS = (EARLY_CLIENT, RECENT_CLIENT)

# Cross-check against the state_access static snapshot (data/totals.json).
ANCHOR_BLOCK = 24_870_000
ANCHOR_ACCOUNTS = 379_632_901
ANCHOR_STORAGES = 1_552_604_459

# Live-state size metrics tracked per block: entry counts and byte sizes.
COUNT_COLS = ["accounts", "storages", "contract_codes"]
BYTE_COLS = ["account_bytes", "storage_bytes", "contract_code_bytes"]
METRIC_COLS = COUNT_COLS + BYTE_COLS

DATA_DIR = Path(__file__).resolve().parent / "data"
