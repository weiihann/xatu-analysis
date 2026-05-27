"""Derived EIP-7928 size metrics from per-block BAL component counts.

Kept separate from analysis.py (which runs as a script on import) so the pure size math
is importable and testable. ``items`` is the EIP's native size unit; the byte columns use
RLP type widths and are summed into ``bytes_total`` with framing overhead applied. RLP
strips leading zeros, so the byte view over-estimates encoded balances/nonces — it is a
rough secondary to the item counts.
"""

from __future__ import annotations

import pandas as pd

from bal_size.config import (
    ADDRESS_BYTES,
    BALANCE_CHANGE_BYTES,
    CODE_INDEX_BYTES,
    NONCE_CHANGE_BYTES,
    RLP_OVERHEAD_FRAC,
    STORAGE_CHANGE_BYTES,
    STORAGE_KEY_BYTES,
)

# Byte-contribution columns, in breakdown order (suffix after "b_").
BYTE_COMPONENTS = [
    "addresses", "storage_writes", "storage_reads", "balance", "nonce", "code",
]


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Add EIP-7928 size columns (items + per-component bytes) to a per-block frame."""
    df = df.copy()
    df["storage_keys"] = df["n_write_slots"] + df["n_read_only_slots"]
    df["items"] = df["storage_keys"] + df["n_addresses"]

    df["b_addresses"] = df["n_addresses"] * ADDRESS_BYTES
    df["b_storage_writes"] = (
        df["n_write_slots"] * STORAGE_KEY_BYTES + df["n_write_entries"] * STORAGE_CHANGE_BYTES
    )
    df["b_storage_reads"] = df["n_read_only_slots"] * STORAGE_KEY_BYTES
    df["b_balance"] = df["n_balance_changes"] * BALANCE_CHANGE_BYTES
    df["b_nonce"] = df["n_nonce_changes"] * NONCE_CHANGE_BYTES
    df["b_code"] = df["n_contracts"] * CODE_INDEX_BYTES + df["code_bytes"]

    df["bytes_raw"] = df[[f"b_{c}" for c in BYTE_COMPONENTS]].sum(axis=1)
    df["bytes_total"] = df["bytes_raw"] * (1 + RLP_OVERHEAD_FRAC)
    return df
