"""Sweep the slot read-through warm/cold classification at T=30 across post-Merge anchors.

For each weekly anchor, classify populated (nonzero) slot reads as warm or cold under two
caching rules (write-age = EIP-8188, read-age = RPC-style), via `queries.slot_read_through`.
This quantifies how much populated-read traffic a write-age vs a read-age primary store would
serve, over time.

Only T=30 (the policy window). The strict ordering-aware query uses a window function over all
events in the window, so it is heavier than the scalar sweeps and can OOM on the densest
anchors; the run is resumable per anchor and meant to be wrapped in a relaunch loop. Output is
a dedicated parquet, additive to everything else.

    uv run python -m state_access.v2.collect_read_through        # T=30 across all anchors
"""

from __future__ import annotations

import sys
import time

import pandas as pd
from clickhouse_connect.driver.exceptions import DatabaseError

from lib.clickhouse import run_query
from state_access.v2 import queries as q
from state_access.v2.config import DATA_DIR_V2, anchors_v2
from state_access.history_config import block_to_date

WINDOW = 30
MAX_ATTEMPTS = 6
RETRY_BASE_DELAY = 20
HEAVY = {
    "max_execution_time": 7200,
    "max_bytes_before_external_group_by": 24_000_000_000,
    "max_bytes_before_external_sort": 24_000_000_000,
}
PARQUET = DATA_DIR_V2 / "read_through_w30.parquet"


def _fetch(anchor: int, window_days: int) -> dict:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            row = run_query(q.slot_read_through(anchor, window_days),
                            settings=HEAVY, send_receive_timeout=7200).iloc[0]
            return {
                "anchor_block": anchor,
                "window_days": window_days,
                "date": block_to_date(anchor),
                "total_nz": int(row.total_nz),
                "writeage_warm": int(row.writeage_warm),
                "readage_warm": int(row.readage_warm),
            }
        except DatabaseError as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            delay = RETRY_BASE_DELAY * attempt
            print(f"  block {anchor:,}: attempt {attempt} failed "
                  f"({type(exc).__name__}); retry in {delay}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def collect(window_days: int) -> None:
    existing = pd.read_parquet(PARQUET) if PARQUET.exists() else None
    done = set(existing["anchor_block"]) if existing is not None else set()
    rows = existing.to_dict("records") if existing is not None else []

    todo = [a for a in sorted(anchors_v2(window_days), reverse=True) if a not in done]
    print(f"T={window_days}d: {len(todo)} anchors to collect ({len(done)} done)", flush=True)

    for i, anchor in enumerate(todo, 1):
        t0 = time.time()
        row = _fetch(anchor, window_days)
        rows.append(row)
        tmp = PARQUET.with_suffix(".parquet.tmp")
        pd.DataFrame(rows).sort_values("anchor_block").to_parquet(tmp, index=False)
        tmp.replace(PARQUET)
        tot = row["total_nz"]
        wa = 100 * row["writeage_warm"] / tot if tot else 0
        ra = 100 * row["readage_warm"] / tot if tot else 0
        print(f"  [{i}/{len(todo)}] block {anchor:,} {row['date']:%Y-%m-%d} "
              f"{time.time()-t0:5.0f}s: write-age warm {wa:.1f}%  read-age warm {ra:.1f}%",
              flush=True)


def main(window_days: int) -> None:
    collect(window_days)
    print(f"\nDone. {PARQUET}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else WINDOW)
