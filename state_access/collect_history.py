"""Resumable post-Merge historical sweep of state_access at W=30.

Reuses the verified query builders in state_access.queries, one anchor at a time,
checkpointing the full result parquet after each anchor.

Run with:  uv run python -m state_access.collect_history
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from lib.clickhouse import run_query
from state_access import queries
from state_access.history_config import HISTORY_PARQUET, W, anchors, block_to_date


def remaining_anchors(all_anchors: list[int], existing: pd.DataFrame | None) -> list[int]:
    """Anchors not already present in a checkpoint DataFrame."""
    done: set[int] = set()
    if existing is not None and not existing.empty:
        done = {int(b) for b in existing["anchor_block"]}
    return [a for a in all_anchors if a not in done]


def build_row(
    anchor: int,
    state: Mapping[str, object],
    acct_pct: float,
    stor_pct: float,
    updt_pct: float,
    totals: Mapping[str, int],
) -> dict[str, object]:
    """Assemble one time-series row from raw per-anchor query outputs."""
    unique_accounts = int(state["unique_accounts"])
    unique_slots = int(state["unique_storage_slots"])
    total_accounts = int(totals["accounts"])
    total_storages = int(totals["storages"])
    pct_state_warm = 100 * unique_slots / total_storages
    return {
        "anchor_block": anchor,
        "date": block_to_date(anchor),
        "unique_accounts": unique_accounts,
        "unique_storage_slots": unique_slots,
        "total_accounts": total_accounts,
        "total_storages": total_storages,
        "pct_accounts_cold": round(100 - 100 * unique_accounts / total_accounts, 4),
        "pct_storage_cold": round(100 - 100 * unique_slots / total_storages, 4),
        "acct_writes_cold_pct": round(100 - acct_pct, 4),
        "storage_writes_cold_pct": round(100 - stor_pct, 4),
        "pct_update_gas_warm": round(updt_pct, 4),
        "pct_state_warm": round(pct_state_warm, 4),
        "concentration_x": round(updt_pct / pct_state_warm, 2),
    }
