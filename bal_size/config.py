"""Configuration for the bal_size analysis.

Reconstructs per-block EIP-7928 Block-Level Access List (BAL) component counts over the
trailing ~6 months and reports their trend, distribution, and breakdown.

BAL size is measured in EIP-7928's own unit, ``items = storage_keys + addresses``, capped
per block at ``block_gas_limit / ITEM_COST``. Encoded byte size is a linear function of the
per-component counts (see analysis.py); it is reported as a clearly-labelled derived view.

Write-side counts come from the local ``primary`` node; the read-side (read-only storage
slots, touched-address union) comes from ``ethpandaops``, the only cluster holding the
canonical_execution_*_reads tables. The two sources are reconciled by a sample cross-check.
"""

from __future__ import annotations

from pathlib import Path

# Post-Merge cadence + block->date mapping are shared with the other analyses.
from state_access.history_config import (  # re-exported for convenience
    FORKS,
    MERGE_BLOCK,
    MERGE_TS,
    SECONDS_PER_BLOCK,
    block_to_date,
)

__all__ = [
    "FORKS", "MERGE_BLOCK", "MERGE_TS", "SECONDS_PER_BLOCK", "block_to_date",
    "NETWORK", "BLOCKS_PER_DAY", "END_BLOCK", "WINDOW_DAYS", "START_BLOCK",
    "CHUNK_BLOCKS", "CROSSCHECK_SAMPLE", "ITEM_COST", "GAS_LIMITS",
    "ADDRESS_BYTES", "STORAGE_KEY_BYTES", "STORAGE_CHANGE_BYTES", "BALANCE_CHANGE_BYTES",
    "NONCE_CHANGE_BYTES", "CODE_INDEX_BYTES", "RLP_OVERHEAD_FRAC",
    "COMPONENT_COLS", "DATA_DIR", "LOCAL_PARQUET", "EPO_PARQUET", "PERBLOCK_PARQUET",
]

NETWORK = "mainnet"
BLOCKS_PER_DAY = 7_200

# Window: trailing ~6 months ending at the latest jointly-available block. The write-side
# (local) node tops out at ~24,873,999; END_BLOCK rounds that down to a clean block that
# also coincides with the other analyses' anchor.
END_BLOCK = 24_870_000
WINDOW_DAYS = 180
START_BLOCK = END_BLOCK - WINDOW_DAYS * BLOCKS_PER_DAY  # 23,574,000

# Resumable collection: blocks per chunked query.
CHUNK_BLOCKS = 25_000
# Blocks sampled for the local-vs-ethpandaops write-slot cross-check.
CROSSCHECK_SAMPLE = 2_000

# --- EIP-7928 sizing -------------------------------------------------------------------
# A BAL's size cap is expressed in "items": items = storage_keys + addresses.
ITEM_COST = 2_000
GAS_LIMITS = {"30M": 30_000_000, "36M": 36_000_000, "60M": 60_000_000}

# RLP encoding type widths (bytes). Nominal upper bounds — RLP strips leading zeros, so
# actual encoded integers are smaller; the derived byte view is an over-estimate and is
# treated as rough. EIP-7928 §"BAL Size Considerations" cites ~6.1% RLP framing overhead.
ADDRESS_BYTES = 20            # Address per touched account (AccountChanges)
STORAGE_KEY_BYTES = 32        # StorageKey, per written slot and per read-only slot
STORAGE_CHANGE_BYTES = 4 + 32   # BlockAccessIndex(uint32) + StorageValue(uint256)
BALANCE_CHANGE_BYTES = 4 + 32   # BlockAccessIndex + Balance(uint256)
NONCE_CHANGE_BYTES = 4 + 8      # BlockAccessIndex + Nonce(uint64)
CODE_INDEX_BYTES = 4            # BlockAccessIndex; bytecode size comes from code_bytes
RLP_OVERHEAD_FRAC = 0.061

# Per-block component count columns produced by collection.
COMPONENT_COLS = [
    "n_addresses", "n_write_slots", "n_write_entries", "n_read_only_slots",
    "n_balance_changes", "n_nonce_changes", "n_contracts", "code_bytes",
]

DATA_DIR = Path(__file__).resolve().parent / "data"
LOCAL_PARQUET = DATA_DIR / "_local_perblock.parquet"
EPO_PARQUET = DATA_DIR / "_epo_perblock.parquet"
PERBLOCK_PARQUET = DATA_DIR / "bal_perblock.parquet"
