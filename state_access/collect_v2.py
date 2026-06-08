"""Drive the v2 histograms across all `(W, object_type)` cells.

One ClickHouse query per cell. Each result is a long-format histogram with rows
`(slice, n_w, n_r, n_keys)`. All cells from all windows are concatenated and persisted to
`data/v2/{slot,account}_histogram.parquet`. The parquet stores `window_days` per row so
re-runs can skip cells already present.

Resumable: if a parquet already contains a row for `(window_days, slice, n_w, n_r)`, the
corresponding cell is skipped on the next run. Delete the parquet to force a full re-pull.

    uv run python -m state_access.collect_v2
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

import pandas as pd

from lib.clickhouse import run_query
from state_access import queries_v2
from state_access.config_v2 import ANCHOR_BLOCK_V2, DATA_DIR_V2, WINDOWS_V2

OBJECT_TYPES: dict[str, Callable[[int, int], str]] = {
    "slot": queries_v2.slot_histogram,
    "account": queries_v2.account_histogram,
    # Typed slot histogram with per-key counts split by value transition
    # (create / update / delete for writes, zero / nonzero for reads).
    "slot_typed": queries_v2.slot_typed_histogram,
}


def _parquet_path(object_type: str) -> Path:
    return DATA_DIR_V2 / f"{object_type}_histogram.parquet"


def _load_existing(object_type: str) -> tuple[pd.DataFrame, set[int]]:
    """Return (existing histogram rows, windows already present)."""
    p = _parquet_path(object_type)
    if not p.exists():
        return pd.DataFrame(), set()
    df = pd.read_parquet(p)
    return df, set(int(w) for w in df["window_days"].unique())


def _run_cell(object_type: str, window_days: int) -> pd.DataFrame:
    """Run one (object_type, window_days) query, returning the histogram with W tagged.

    Column shape varies by `object_type`: the slot/account histograms emit
    `(slice, n_w, n_r, n_keys)`; the slot_typed histogram emits
    `(n_w_create, n_w_update, n_w_delete, n_r_zero, n_r_nonzero, n_keys)`. We just
    propagate whatever the query returned plus `window_days`.
    """
    builder = OBJECT_TYPES[object_type]
    sql = builder(ANCHOR_BLOCK_V2, window_days)
    # 90 min ceiling — slot W=365 is the worst case; raise if it ever bites.
    df = run_query(sql, profile="primary", settings={"max_execution_time": 5400})
    df = df.copy()
    df["window_days"] = window_days
    return df


def collect(object_type: str) -> pd.DataFrame:
    """Collect all windows for one object type, resuming over already-persisted windows."""
    existing, done = _load_existing(object_type)
    todo = [w for w in WINDOWS_V2 if w not in done]
    print(f"\n>>> {object_type}: {len(done)} windows already done, {len(todo)} to run")

    out = existing
    for w in todo:
        print(f"  {object_type} W={w:>3}d: querying...", flush=True)
        t0 = time.time()
        df = _run_cell(object_type, w)
        elapsed = time.time() - t0
        n_keys_total = int(df["n_keys"].sum())
        n_slices = df["slice"].nunique() if "slice" in df.columns else 0
        print(f"  {object_type} W={w:>3}d: {len(df):>6} rows, "
              f"{n_keys_total:>14,} total keys, {n_slices} slices, {elapsed:>6.1f}s")
        # Persist after each cell so a long run can be killed and resumed.
        out = pd.concat([out, df], ignore_index=True)
        out.to_parquet(_parquet_path(object_type), index=False)
    return out


def main() -> None:
    DATA_DIR_V2.mkdir(parents=True, exist_ok=True)
    print(f"Anchor block: {ANCHOR_BLOCK_V2:,}")
    print(f"Windows: {WINDOWS_V2}")
    for object_type in OBJECT_TYPES:
        collect(object_type)
    print("\nDone. Histograms at:")
    for object_type in OBJECT_TYPES:
        print(f"  {_parquet_path(object_type)}")


if __name__ == "__main__":
    main()
