"""Resumable post-Merge historical sweep of state_access.

Reuses the verified query builders in state_access.queries, one anchor at a time,
checkpointing the full result parquet after each anchor. Each window W writes its own
parquet (data/history_w{W}.parquet), so sweeps at different W never overwrite each other.

Run with:  uv run python -m state_access.collect_history [W ...]
e.g.        uv run python -m state_access.collect_history 90 180 365
(no args defaults to W=30)
"""

from __future__ import annotations

import sys
from collections.abc import Mapping

import pandas as pd

from lib.clickhouse import run_query
from state_access import queries
from state_access.history_config import DEFAULT_W, anchors, block_to_date, parquet_for


def remaining_anchors(all_anchors: list[int], existing: pd.DataFrame | None) -> list[int]:
    """Anchors not already present in a checkpoint DataFrame."""
    done: set[int] = set()
    if existing is not None and not existing.empty:
        done = {int(b) for b in existing["anchor_block"]}
    return [a for a in all_anchors if a not in done]


def build_row(
    anchor: int,
    w: int,
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
        "window_days": w,
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


def _fetch_anchor(anchor: int, w: int) -> dict[str, object]:
    """Run all queries for one anchor at window `w` and build its row."""
    state = run_query(queries.state_touched(anchor, w)).iloc[0]
    acct_pct = float(run_query(queries.account_writes_warm(anchor, w)).iloc[0]["pct_warm"])
    stor_pct = float(run_query(queries.storage_writes_warm(anchor, w)).iloc[0]["pct_warm"])
    updt_pct = float(run_query(queries.update_writes_warm(anchor, w)).iloc[0]["pct_warm"])

    tdf = run_query(queries.totals(anchor), profile="ethpandaops")
    if tdf.empty:
        raise RuntimeError(f"No execution_state_size snapshot at or before block {anchor}.")
    totals = {"accounts": int(tdf.iloc[0]["accounts"]), "storages": int(tdf.iloc[0]["storages"])}

    return build_row(anchor, w, state, acct_pct, stor_pct, updt_pct, totals)


def collect(w: int) -> None:
    """Run (or resume) the sweep for a single window W."""
    parquet = parquet_for(w)
    existing = pd.read_parquet(parquet) if parquet.exists() else None
    rows = existing.to_dict("records") if existing is not None else []

    todo = remaining_anchors(anchors(w), existing)
    print(f"W={w}d sweep: {len(todo)} anchors to collect "
          f"({len(rows)} already done), writing {parquet}", flush=True)

    for i, anchor in enumerate(todo, 1):
        row = _fetch_anchor(anchor, w)
        rows.append(row)
        pd.DataFrame(rows).sort_values("anchor_block").to_parquet(parquet, index=False)
        print(f"  [{i}/{len(todo)}] block {anchor:,} {row['date']:%Y-%m-%d}: "
              f"storage cold {row['pct_storage_cold']:.1f}%, "
              f"update-gas warm {row['pct_update_gas_warm']:.1f}%, "
              f"conc {row['concentration_x']:.0f}x", flush=True)

    print(f"W={w}d done: {len(rows)} anchors in {parquet}\n", flush=True)


def main(windows: list[int]) -> None:
    for w in windows:
        collect(w)


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]] or [DEFAULT_W])
