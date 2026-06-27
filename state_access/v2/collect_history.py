"""Collect full-history (genesis → anchor) event totals for writes and reads.

One `countIf` aggregate per (kind, chunk). Chunks are 1M-block tiles of
`[0, ANCHOR_BLOCK_V2]`, split at the merge boundary so every chunk maps to exactly one
ClickHouse profile:

- write kinds always run on `primary` (the `_diffs` / `contracts` tables are
  full-history locally),
- read kinds run on `ethpandaops` pre-merge (local `_reads` coverage starts at the
  merge) and `primary` post-merge.

Counts are additive across disjoint ranges, so a chunk that times out is split in half
and retried losslessly (floor 125k blocks). Results accumulate in long format
`(kind, metric, bn_lo, bn_hi, profile, n)` in `data/v2/history_event_totals.parquet`,
persisted after every chunk; re-runs skip `(kind, bn_lo)` pairs already present.

    uv run python -m state_access.v2.collect_history
"""

from __future__ import annotations

import time
from typing import Callable

import pandas as pd

from lib.clickhouse import run_query
from state_access.v2 import queries
from state_access.v2.config import ANCHOR_BLOCK_V2, DATA_DIR_V2, MERGE_BLOCK

PARQUET = DATA_DIR_V2 / "history_event_totals.parquet"
CHUNK_BLOCKS = 1_000_000
MIN_CHUNK = 125_000
SETTINGS = {"max_execution_time": 900}

# kind -> (SQL builder, is_read). Read kinds need ethpandaops below the merge block.
KINDS: dict[str, tuple[Callable[[int, int], str], bool]] = {
    "slot_write": (queries.slot_write_event_totals, False),
    "account_balance_write": (queries.account_balance_write_totals, False),
    "account_nonce_write": (queries.account_nonce_write_totals, False),
    "account_contract_create": (queries.account_contract_create_totals, False),
    "slot_read": (queries.slot_read_event_totals, True),
    "account_balance_read": (queries.account_balance_read_totals, True),
    "account_nonce_read": (queries.account_nonce_read_totals, True),
    "account_appearance_read": (queries.account_appearance_read_totals, True),
}


def build_chunks() -> list[tuple[int, int]]:
    """Inclusive 1M-block tiles of [0, anchor], split exactly at the merge block."""
    chunks = []
    lo = 0
    while lo <= ANCHOR_BLOCK_V2:
        hi = min(lo + CHUNK_BLOCKS - 1, ANCHOR_BLOCK_V2)
        if lo < MERGE_BLOCK <= hi:
            chunks.append((lo, MERGE_BLOCK - 1))
            chunks.append((MERGE_BLOCK, hi))
        else:
            chunks.append((lo, hi))
        lo = hi + 1
    return chunks


def _profile(kind: str, bn_hi: int) -> str:
    is_read = KINDS[kind][1]
    return "ethpandaops" if is_read and bn_hi < MERGE_BLOCK else "primary"


def _melt(df: pd.DataFrame) -> list[tuple[str, int]]:
    """Normalize a query result to (metric, n) pairs.

    The appearance builder is already long (`metric`, `n`); the others are one wide row
    of `n_<metric>` columns.
    """
    if "metric" in df.columns:
        return [(str(r["metric"]), int(r["n"])) for _, r in df.iterrows()]
    assert len(df) == 1, f"expected one aggregate row, got {len(df)}"
    row = df.iloc[0]
    return [(c.removeprefix("n_"), int(row[c])) for c in df.columns]


def _run_range(kind: str, bn_lo: int, bn_hi: int, chunk_lo: int) -> list[dict]:
    """Run one (kind, range) query, splitting in half on failure (counts are additive).

    `chunk_lo` is the originating chunk's low bound — the resume key — so split
    sub-ranges still register the whole chunk as done once all parts complete.
    """
    builder = KINDS[kind][0]
    profile = _profile(kind, bn_hi)
    try:
        df = run_query(builder(bn_lo, bn_hi), profile=profile, settings=SETTINGS)
    except Exception as e:
        span = bn_hi - bn_lo + 1
        if span < 2 * MIN_CHUNK:
            raise RuntimeError(
                f"{kind} [{bn_lo:,}, {bn_hi:,}] failed at minimum chunk size"
            ) from e
        mid = bn_lo + span // 2
        print(f"    {kind} [{bn_lo:,}, {bn_hi:,}] failed ({type(e).__name__}); "
              f"splitting at {mid:,}", flush=True)
        return (_run_range(kind, bn_lo, mid - 1, chunk_lo)
                + _run_range(kind, mid, bn_hi, chunk_lo))
    return [
        {"kind": kind, "metric": metric, "bn_lo": bn_lo, "bn_hi": bn_hi,
         "chunk_lo": chunk_lo, "profile": profile, "n": n}
        for metric, n in _melt(df)
    ]


def main() -> None:
    DATA_DIR_V2.mkdir(parents=True, exist_ok=True)
    chunks = build_chunks()
    done_df = pd.read_parquet(PARQUET) if PARQUET.exists() else pd.DataFrame()
    done = (set(zip(done_df["kind"], done_df["chunk_lo"])) if len(done_df) else set())
    rows = done_df.to_dict("records") if len(done_df) else []

    todo = [(k, lo, hi) for k in KINDS for (lo, hi) in chunks if (k, lo) not in done]
    print(f"{len(chunks)} chunks × {len(KINDS)} kinds; "
          f"{len(done)} already done, {len(todo)} to run", flush=True)

    for i, (kind, lo, hi) in enumerate(todo, 1):
        t0 = time.time()
        new = _run_range(kind, lo, hi, chunk_lo=lo)
        rows.extend(new)
        pd.DataFrame(rows).to_parquet(PARQUET, index=False)
        total = sum(r["n"] for r in new if r["metric"] != "total")
        print(f"  [{i:>3}/{len(todo)}] {kind:24s} [{lo:>10,}, {hi:>10,}] "
              f"{_profile(kind, hi):11s} {time.time()-t0:>5.0f}s  {total:>15,} events",
              flush=True)

    print(f"\nDone. {PARQUET}")


if __name__ == "__main__":
    main()
