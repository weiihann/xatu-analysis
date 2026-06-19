"""Repatch the account columns of the historical sweep after the lower(address) fix.

`address_appearances` and `balance_reads` stored checksummed (mixed-case) addresses for
blocks 15,537,394 to ~20,975,000 (Merge through 2024-10-08). The original sweep hashed
them raw, so the per-account W/R join missed and account R was inflated for any
(anchor, window) cell whose window reaches below the flip block. queries_v2 now wraps
account address hashes in lower(); this reruns ONLY the four account queries for the
affected cells and overwrites ONLY the 18 account-derived columns. Slot columns,
denominators, and already-correct (recent) cells are left untouched.

Resumable via a sidecar checkpoint, OOM-resilient retry, atomic parquet writes.

    uv run python -m state_access.repatch_v2_account_sweep            # all affected cells
    REPATCH_LIMIT=1 uv run python -m state_access.repatch_v2_account_sweep 30  # smoke
"""

from __future__ import annotations

import json
import os
import sys
import time

import pandas as pd
from clickhouse_connect.driver.exceptions import DatabaseError

from lib.clickhouse import run_query
from state_access import queries_v2 as q
from state_access.config import BLOCKS_PER_DAY
from state_access.config_v2 import DATA_DIR_V2, SWEEP_WINDOWS
from state_access.sweep_concentration import concentration_shares

# Safe margin above the ~20,975,000 checksum flip. A cell whose window low end is at or
# above this block is already lowercase, so lower() is a no-op there and it is skipped.
FLIP_BLOCK = 21_000_000
MAX_ATTEMPTS = 6
RETRY_BASE_DELAY = 20
HEAVY = {
    "max_execution_time": 7200,
    "max_bytes_before_external_group_by": 20_000_000_000,
    "max_bytes_before_external_sort": 20_000_000_000,
}
CHECKPOINT = DATA_DIR_V2 / "account_repatch_done.json"
ACCT_KEYS = ("W", "R", "RW_union")
AFO_KEYS = ("total_accounts", "first_is_write", "first_is_nonzero_read",
            "first_is_zero_read", "first_is_appearance_read")
RES_KEYS = ("total_r", "empty_accounts", "nonempty_accounts", "unknown_accounts")


def parquet_for(window_days: int):
    return DATA_DIR_V2 / f"sweep_w{window_days}.parquet"


def _q(sql: str) -> pd.DataFrame:
    return run_query(sql, settings=HEAVY, send_receive_timeout=7200)


def _is_affected(anchor: int, window_days: int) -> bool:
    return anchor - window_days * BLOCKS_PER_DAY < FLIP_BLOCK


def account_columns(anchor: int, window_days: int) -> dict:
    """Recompute the 18 account-derived sweep columns with the lower()-fixed queries."""
    acct = _q(q.account_sweep_summary(anchor, window_days)).iloc[0]
    conc = concentration_shares(_q(q.account_histogram(anchor, window_days)))
    afo = _q(q.account_first_op(anchor, window_days)).iloc[0]
    res = _q(q.account_r_empty_split(anchor, window_days)).iloc[0]

    out: dict = {f"acct_{k}": int(acct[k]) for k in ACCT_KEYS}
    for k, v in conc.items():
        out[f"conc_acct_{k}"] = float(v)
    for k in AFO_KEYS:
        out[f"afo_{k}"] = int(afo[k])
    for k in RES_KEYS:
        out[f"res_{k}"] = int(res[k])
    return out


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


def repatch(window_days: int, done: set[tuple[int, int]], limit: int | None) -> None:
    parquet = parquet_for(window_days)
    df = pd.read_parquet(parquet)
    todo = [int(a) for a in sorted(df["anchor_block"])
            if _is_affected(int(a), window_days) and (int(a), window_days) not in done]
    if limit is not None:
        todo = todo[:limit]
    print(f"T={window_days}d: {len(todo)} affected cells to repatch "
          f"(of {len(df)} total)", flush=True)

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
    limit = int(os.environ["REPATCH_LIMIT"]) if "REPATCH_LIMIT" in os.environ else None
    done = _load_done()
    for w in windows:
        repatch(w, done, limit)
    print(f"\nDone. checkpoint: {CHECKPOINT}")


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]] or list(SWEEP_WINDOWS))
