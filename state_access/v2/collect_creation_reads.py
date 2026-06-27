"""Resumable sweep of created-slot read-back counts (additive to the main sweep).

One row per (anchor, window): how many create-only (C) and created+deleted (C+D) slots
are read back with a populated value in a transaction that did NOT write them — the
genuine "created once, then read elsewhere" signal (the write's own coupled read is
excluded; see `queries_v2.slot_creation_reads`).

This is a NEW, additive product: it neither reads nor writes any existing sweep artifact.
Anchors run newest-first so the snapshot anchor (24,870,000) lands first for validation
against `sweep_summary` before the full sweep continues. Checkpoint after every anchor;
re-runs skip anchors already present.

    LIMIT=1 uv run python -m state_access.v2.collect_creation_reads 30 90 180 365  # gate: anchor only
    uv run python -m state_access.v2.collect_creation_reads                        # full sweep
"""

from __future__ import annotations

import os
import sys
import time

import pandas as pd
from clickhouse_connect.driver.exceptions import DatabaseError

from lib.clickhouse import run_query
from state_access.v2 import queries as q
from state_access.v2.config import DATA_DIR_V2, SWEEP_WINDOWS, anchors_v2
from state_access.history_config import block_to_date

MAX_ATTEMPTS = 6
RETRY_BASE_DELAY = 20
HEAVY = {
    "max_execution_time": 7200,
    "max_bytes_before_external_group_by": 20_000_000_000,
    "max_bytes_before_external_sort": 20_000_000_000,
}
PARQUET = DATA_DIR_V2 / "creation_reads.parquet"
COLS = ("c_only", "c_only_read", "cd", "cd_read")


def _fetch_with_retry(anchor: int, window_days: int) -> dict:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            row = run_query(q.slot_creation_reads(anchor, window_days),
                            settings=HEAVY, send_receive_timeout=7200).iloc[0]
            return {
                "anchor_block": anchor,
                "window_days": window_days,
                "date": block_to_date(anchor),
                **{c: int(row[c]) for c in COLS},
            }
        except DatabaseError as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            delay = RETRY_BASE_DELAY * attempt
            print(f"  block {anchor:,} T={window_days}d: attempt {attempt} failed "
                  f"({type(exc).__name__}); retry in {delay}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def collect(windows: list[int], limit: int | None = None) -> None:
    existing = pd.read_parquet(PARQUET) if PARQUET.exists() else None
    done = (set(zip(existing["anchor_block"], existing["window_days"]))
            if existing is not None else set())
    rows = existing.to_dict("records") if existing is not None else []

    for w in windows:
        todo = [a for a in sorted(anchors_v2(w), reverse=True) if (a, w) not in done]
        if limit is not None:
            todo = todo[:limit]
        print(f"T={w}d: {len(todo)} anchors to collect", flush=True)
        for i, anchor in enumerate(todo, 1):
            t0 = time.time()
            row = _fetch_with_retry(anchor, w)
            rows.append(row)
            tmp = PARQUET.with_suffix(".parquet.tmp")
            pd.DataFrame(rows).sort_values(["window_days", "anchor_block"]).to_parquet(tmp, index=False)
            tmp.replace(PARQUET)
            cr = 100 * row["c_only_read"] / row["c_only"] if row["c_only"] else 0
            dr = 100 * row["cd_read"] / row["cd"] if row["cd"] else 0
            print(f"  [{i}/{len(todo)}] block {anchor:,} {row['date']:%Y-%m-%d} "
                  f"{time.time()-t0:5.0f}s: C read-back {cr:.1f}%  C+D read-back {dr:.1f}%",
                  flush=True)


def main(windows: list[int]) -> None:
    limit = int(os.environ["LIMIT"]) if "LIMIT" in os.environ else None
    collect(windows, limit=limit)
    print(f"\nDone. {PARQUET}")


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]] or list(SWEEP_WINDOWS))
