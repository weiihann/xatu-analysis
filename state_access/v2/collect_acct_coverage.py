"""Additive sweep collector for account warm-update coverage.

Adds the account analog of the slot warm-update columns to the sweep parquets. For each
(anchor, window), the share of account update events (balance/nonce `x→y`) that are warm
(the account had an earlier in-window write). Writes
`acct_upd_{total_updates, warm_updates, cold_updates, pct_warm}` and touches no existing
column, so it neither reads nor rewrites the slot/account warmth artifacts.

Resumable via a sidecar checkpoint, OOM-resilient retry, atomic parquet writes.

    ACCT_UPD_LIMIT=1 uv run python -m state_access.v2.collect_acct_coverage 30   # smoke
    uv run python -m state_access.v2.collect_acct_coverage                       # all cells
"""

from __future__ import annotations

import json
import os
import sys
import time

import pandas as pd
from clickhouse_connect.driver.exceptions import DatabaseError

from lib.clickhouse import run_query
from state_access.v2 import queries as q
from state_access.v2.config import DATA_DIR_V2, SWEEP_WINDOWS

MAX_ATTEMPTS = 6
RETRY_BASE_DELAY = 20
HEAVY = {
    "max_execution_time": 7200,
    "max_bytes_before_external_group_by": 20_000_000_000,
    "max_bytes_before_external_sort": 20_000_000_000,
}
CHECKPOINT = DATA_DIR_V2 / "acct_coverage_done.json"
COLS = ("total_updates", "warm_updates", "cold_updates", "pct_warm")


def parquet_for(window_days: int):
    return DATA_DIR_V2 / f"sweep_w{window_days}.parquet"


def _coverage(anchor: int, window_days: int) -> dict:
    row = run_query(q.account_update_coverage(anchor, window_days),
                    settings=HEAVY, send_receive_timeout=7200).iloc[0]
    return {f"acct_upd_{c}": (float(row[c]) if c == "pct_warm" else int(row[c]))
            for c in COLS}


def _fetch_with_retry(anchor: int, window_days: int) -> dict:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _coverage(anchor, window_days)
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


def collect(window_days: int, done: set[tuple[int, int]], limit: int | None) -> None:
    parquet = parquet_for(window_days)
    df = pd.read_parquet(parquet)
    todo = [int(a) for a in sorted(df["anchor_block"])
            if (int(a), window_days) not in done]
    if limit is not None:
        todo = todo[:limit]
    print(f"T={window_days}d: {len(todo)} cells to collect (of {len(df)})", flush=True)

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
              f"acct %warm={cols['acct_upd_pct_warm']:.1f}", flush=True)


def main(windows: list[int]) -> None:
    limit = int(os.environ["ACCT_UPD_LIMIT"]) if "ACCT_UPD_LIMIT" in os.environ else None
    done = _load_done()
    for w in windows:
        collect(w, done, limit)
    print(f"\nDone. checkpoint: {CHECKPOINT}")


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]] or list(SWEEP_WINDOWS))
