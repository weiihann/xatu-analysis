"""Collect slot + account warm-update coverage at the small windows (T=1, T=7) across the
weekly post-Merge sweep.

The main sweep covers T in {30, 90, 180, 365}. This adds the two short windows for the §6.1
coverage table, so all its rows are on the same sweep-mean basis. Writes-only queries
(storage_diffs for slots, balance/nonce_diffs for accounts), so they are cheap and do not
risk the OOM the wide-window account scans hit. Output is a dedicated parquet
`coverage_small_windows.parquet`, one row per (anchor, window), resumable per cell.

    uv run python -m state_access.v2.collect_coverage_small         # T=1 and T=7
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

WINDOWS = (1, 7)
MAX_ATTEMPTS = 6
RETRY_BASE_DELAY = 15
HEAVY = {
    "max_execution_time": 3600,
    "max_bytes_before_external_group_by": 20_000_000_000,
}
PARQUET = DATA_DIR_V2 / "coverage_small_windows.parquet"


def _fetch(anchor: int, window_days: int) -> dict:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            slot = run_query(q.slot_update_coverage(anchor, window_days),
                             settings=HEAVY, send_receive_timeout=3600).iloc[0]
            acct = run_query(q.account_update_coverage(anchor, window_days),
                             settings=HEAVY, send_receive_timeout=3600).iloc[0]
            return {
                "anchor_block": anchor,
                "window_days": window_days,
                "date": block_to_date(anchor),
                "slot_total_updates": int(slot.total_updates),
                "slot_pct_warm": float(slot.pct_warm),
                "acct_total_updates": int(acct.total_updates),
                "acct_pct_warm": float(acct.pct_warm),
            }
        except DatabaseError as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            delay = RETRY_BASE_DELAY * attempt
            print(f"  block {anchor:,} T={window_days}d: attempt {attempt} failed "
                  f"({type(exc).__name__}); retry in {delay}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def collect(windows: tuple[int, ...]) -> None:
    existing = pd.read_parquet(PARQUET) if PARQUET.exists() else None
    done = (set(zip(existing["anchor_block"], existing["window_days"]))
            if existing is not None else set())
    rows = existing.to_dict("records") if existing is not None else []

    for w in windows:
        todo = [a for a in sorted(anchors_v2(w)) if (a, w) not in done]
        print(f"T={w}d: {len(todo)} anchors to collect", flush=True)
        for i, anchor in enumerate(todo, 1):
            t0 = time.time()
            row = _fetch(anchor, w)
            rows.append(row)
            tmp = PARQUET.with_suffix(".parquet.tmp")
            pd.DataFrame(rows).sort_values(["window_days", "anchor_block"]).to_parquet(tmp, index=False)
            tmp.replace(PARQUET)
            if i % 25 == 0 or i == len(todo):
                print(f"  [{i}/{len(todo)}] block {anchor:,} {time.time()-t0:4.0f}s: "
                      f"slot {row['slot_pct_warm']:.1f}%  acct {row['acct_pct_warm']:.1f}%",
                      flush=True)


def main(windows: tuple[int, ...]) -> None:
    collect(windows)
    print(f"\nDone. {PARQUET}")


if __name__ == "__main__":
    main(tuple(int(a) for a in sys.argv[1:]) or WINDOWS)
