"""Re-run the account sweep cells whose window overlaps the backfilled write-gap.

`backfill_write_gap` restores the local balance_diffs/nonce_diffs over ~[23.3M, 23.64M].
The sweep cells whose window touched that range still hold pre-backfill (gap-inflated)
account columns, where missing writes pushed written accounts into R⁺. This recomputes the
four account queries for those cells against the now-complete local data and overwrites the
18 account columns, leaving slot columns and unaffected cells untouched.

Resumable via a sidecar checkpoint; OOM-resilient retry. Run after the backfill finishes.

    RERUN_LIMIT=1 uv run python -m state_access.rerun_v2_gap_anchors 30   # smoke
    uv run python -m state_access.rerun_v2_gap_anchors                    # all gap cells
"""

from __future__ import annotations

import json
import os
import time

import pandas as pd
from clickhouse_connect.driver.exceptions import DatabaseError

from state_access.config import BLOCKS_PER_DAY
from state_access.config_v2 import DATA_DIR_V2, SWEEP_WINDOWS
from state_access.repatch_v2_account_sweep import (
    MAX_ATTEMPTS,
    RETRY_BASE_DELAY,
    account_columns,
    parquet_for,
)

# Windows overlapping this block range carry the gap; the bounds bracket the partial edge
# buckets so every touched cell is caught.
GAP_LO, GAP_HI = 23_250_000, 23_700_000
CHECKPOINT = DATA_DIR_V2 / "gap_rerun_done.json"


def _overlaps_gap(anchor: int, window_days: int) -> bool:
    return anchor >= GAP_LO and (anchor - window_days * BLOCKS_PER_DAY) <= GAP_HI


def _fetch_with_retry(anchor: int, window_days: int) -> dict:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return account_columns(anchor, window_days)
        except DatabaseError as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            delay = RETRY_BASE_DELAY * attempt
            print(f"  block {anchor:,} T={window_days}d: attempt {attempt} failed "
                  f"({type(exc).__name__}); retry in {delay}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _load_done() -> set[tuple[int, int]]:
    if CHECKPOINT.exists():
        return {(int(a), int(t)) for a, t in json.loads(CHECKPOINT.read_text())}
    return set()


def _save_done(done: set[tuple[int, int]]) -> None:
    tmp = CHECKPOINT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(done)))
    tmp.replace(CHECKPOINT)


def rerun(window_days: int, done: set[tuple[int, int]], limit: int | None) -> None:
    parquet = parquet_for(window_days)
    df = pd.read_parquet(parquet)
    todo = [int(a) for a in sorted(df["anchor_block"])
            if _overlaps_gap(int(a), window_days) and (int(a), window_days) not in done]
    if limit is not None:
        todo = todo[:limit]
    print(f"T={window_days}d: {len(todo)} gap cells to re-run (of {len(df)} total)",
          flush=True)

    for i, anchor in enumerate(todo, 1):
        t0 = time.time()
        cols = _fetch_with_retry(anchor, window_days)
        mask = df["anchor_block"] == anchor
        for col, val in cols.items():
            df.loc[mask, col] = val
        tmp = parquet.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(parquet)
        done.add((anchor, window_days))
        _save_done(done)
        print(f"  [{i}/{len(todo)}] block {anchor:,} {time.time()-t0:5.0f}s: "
              f"acct_R={int(cols['acct_R']):,}", flush=True)


def main(windows: list[int]) -> None:
    limit = int(os.environ["RERUN_LIMIT"]) if "RERUN_LIMIT" in os.environ else None
    done = _load_done()
    for w in windows:
        rerun(w, done, limit)
    print(f"\nDone. checkpoint: {CHECKPOINT}")


if __name__ == "__main__":
    import sys
    main([int(a) for a in sys.argv[1:]] or list(SWEEP_WINDOWS))
