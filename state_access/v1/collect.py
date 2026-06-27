"""Run the state_access queries across all windows and write the result tables.

Outputs (under ``state_access/data/``):
  - hot_cold_state.parquet  : per-window unique accounts / storage slots
  - tradeoff.parquet        : per-window cold-state % and writes-to-cold %
  - gas_concentration.parquet: per-window warm-state %, warm update-gas %, concentration
  - totals.json             : live-state denominators and their snapshot block

Run with:  uv run python -m state_access.v1.collect
"""

from __future__ import annotations

import json

import pandas as pd

from lib.clickhouse import run_query
from state_access.v1 import queries
from state_access.config import (
    ACCOUNT_WRITE_WINDOWS,
    ANCHOR_BLOCK,
    DATA_DIR_V1,
    STATE_WINDOWS,
    STORAGE_WRITE_WINDOWS,
    UPDATE_WINDOWS,
)


def fetch_totals() -> dict[str, int]:
    """Read live-state totals from the ethpandaops cluster."""
    df = run_query(queries.totals(ANCHOR_BLOCK), profile="ethpandaops")
    if df.empty:
        raise RuntimeError(
            f"execution_state_size has no mainnet snapshot at or before block {ANCHOR_BLOCK}."
        )
    row = df.iloc[0]
    return {
        "accounts": int(row["accounts"]),
        "storages": int(row["storages"]),
        "snapshot_block": int(row["snapshot_block"]),
    }


def fetch_state_touched() -> pd.DataFrame:
    """Unique accounts and storage slots modified per window."""
    rows = []
    for w in STATE_WINDOWS:
        r = run_query(queries.state_touched(ANCHOR_BLOCK, w)).iloc[0]
        rows.append(
            {
                "window_days": w,
                "unique_accounts": int(r["unique_accounts"]),
                "unique_storage_slots": int(r["unique_storage_slots"]),
            }
        )
        print(f"  state W={w:>3}d: {rows[-1]['unique_accounts']:>12,} acct  "
              f"{rows[-1]['unique_storage_slots']:>13,} slots")
    return pd.DataFrame(rows)


def _warm_pct(query_builder, windows: list[int], label: str) -> dict[int, float]:
    """Run a writes-to-warm query per window, returning {window: pct_warm}."""
    out = {}
    for w in windows:
        r = run_query(query_builder(ANCHOR_BLOCK, w)).iloc[0]
        out[w] = float(r["pct_warm"])
        print(f"  {label} W={w:>3}d: {int(r['warm']):>10,} / {int(r['today']):>10,} "
              f"= {out[w]:6.2f}% warm")
    return out


def build_tradeoff(state: pd.DataFrame, totals: dict[str, int]) -> pd.DataFrame:
    """Cold-state % alongside the share of today's writes hitting cold."""
    acct = _warm_pct(queries.account_writes_warm, ACCOUNT_WRITE_WINDOWS, "acct ")
    stor = _warm_pct(queries.storage_writes_warm, STORAGE_WRITE_WINDOWS, "stor ")

    windows = sorted(set(ACCOUNT_WRITE_WINDOWS) | set(STORAGE_WRITE_WINDOWS))
    slot_hot = dict(zip(state["window_days"], state["unique_storage_slots"]))
    acct_hot = dict(zip(state["window_days"], state["unique_accounts"]))

    rows = []
    for w in windows:
        # State-cold uses storage slots as the headline denominator, matching the original.
        state_cold = 100 - 100 * slot_hot[w] / totals["storages"]
        rows.append(
            {
                "window_days": w,
                "state_cold_pct": round(state_cold, 4),
                "acct_writes_cold_pct": round(100 - acct[w], 4) if w in acct else None,
                "storage_writes_cold_pct": round(100 - stor[w], 4) if w in stor else None,
                "acct_state_cold_pct": round(100 - 100 * acct_hot[w] / totals["accounts"], 4),
            }
        )
    return pd.DataFrame(rows)


def build_gas_concentration(state: pd.DataFrame, totals: dict[str, int]) -> pd.DataFrame:
    """Warm-state %, warm update-gas %, and their ratio (concentration)."""
    updates = _warm_pct(queries.update_writes_warm, UPDATE_WINDOWS, "updt ")
    slot_hot = dict(zip(state["window_days"], state["unique_storage_slots"]))

    rows = []
    for w in UPDATE_WINDOWS:
        pct_state_warm = 100 * slot_hot[w] / totals["storages"]
        pct_gas_warm = updates[w]
        rows.append(
            {
                "window_days": w,
                "pct_state_warm": round(pct_state_warm, 4),
                "pct_update_gas_warm": round(pct_gas_warm, 4),
                "concentration_x": round(pct_gas_warm / pct_state_warm, 2),
            }
        )
    return pd.DataFrame(rows)


def _check(state: pd.DataFrame) -> None:
    """Sanity-check the state-touched table before persisting."""
    assert state["unique_accounts"].is_monotonic_increasing, "hot accounts must grow with window"
    assert state["unique_storage_slots"].is_monotonic_increasing, "hot slots must grow with window"


def main() -> None:
    DATA_DIR_V1.mkdir(parents=True, exist_ok=True)

    print(f"Anchor block: {ANCHOR_BLOCK:,}")
    print("Fetching live-state totals (ethpandaops)...")
    totals = fetch_totals()
    print(f"  totals @ block {totals['snapshot_block']:,}: "
          f"{totals['accounts']:,} accounts, {totals['storages']:,} slots")

    print("Querying state touched per window...")
    state = fetch_state_touched()
    _check(state)

    print("Querying writes-to-warm (tradeoff)...")
    tradeoff = build_tradeoff(state, totals)

    print("Querying update writes (gas concentration)...")
    gas = build_gas_concentration(state, totals)

    assert gas["concentration_x"].is_monotonic_decreasing, "concentration should fall as W grows"

    state.to_parquet(DATA_DIR_V1 / "hot_cold_state.parquet", index=False)
    tradeoff.to_parquet(DATA_DIR_V1 / "tradeoff.parquet", index=False)
    gas.to_parquet(DATA_DIR_V1 / "gas_concentration.parquet", index=False)
    (DATA_DIR_V1 / "totals.json").write_text(json.dumps(totals, indent=2))

    print(f"\nWrote 3 parquets + totals.json to {DATA_DIR_V1}")


if __name__ == "__main__":
    main()
