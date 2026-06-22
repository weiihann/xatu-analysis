"""Backfill the local balance_diffs / nonce_diffs write-gap from ethpandaops.

The local node is missing balance_diffs and nonce_diffs rows over ~[23,300,000, 23,640,000]
(a sync gap, roughly Aug-Sep 2025); ethpandaops has the data in full. This copies the
missing blocks from ethpandaops into the local node. The read tables and the storage/
contract write tables are intact locally, so only these two account-write tables need it.

ethpandaops lacks the local-only `meta_network_id` column, so it is set to 1 (mainnet) on
insert. Both tables are ReplicatedReplacingMergeTree, so re-runs dedup and the missing-block
discovery makes the job resumable.

    LIMIT=1 uv run python -m state_access.backfill_write_gap   # smoke: one chunk per table
    uv run python -m state_access.backfill_write_gap           # full backfill
"""

from __future__ import annotations

import os
import time

from lib.clickhouse import get_client, run_query

TABLES = ("canonical_execution_balance_diffs", "canonical_execution_nonce_diffs")
RANGE_LO, RANGE_HI = 23_200_000, 23_700_000
BUCKET = 100_000
NETWORK = "mainnet"
MAINNET_ID = 1
# ethpandaops column order (no meta_network_id); meta_network_id is appended on insert.
EPO_COLS = ["updated_date_time", "block_number", "transaction_index", "transaction_hash",
            "internal_index", "address", "from_value", "to_value", "meta_network_name"]
INSERT_COLS = [*EPO_COLS, "meta_network_id"]
CHUNK_BLOCKS = 50


def _missing_blocks(table: str, lo: int, hi: int) -> list[int]:
    sql = (f"SELECT DISTINCT block_number FROM {table} "
           f"WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {lo} AND {hi}")
    epo = run_query(sql, profile="ethpandaops")
    loc = run_query(sql, profile="primary")
    e = {int(x) for x in epo["block_number"]} if "block_number" in epo else set()
    loc_set = {int(x) for x in loc["block_number"]} if "block_number" in loc else set()
    return sorted(e - loc_set)


def _retry(action, what: str, recycle):
    for attempt in range(3):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - surface and retry any driver/network error
            backoff = 5 * (2 ** attempt)
            print(f"    !! {what} failed ({attempt + 1}/3): {str(exc)[:120]}; "
                  f"retry in {backoff}s", flush=True)
            time.sleep(backoff)
            recycle()
    raise RuntimeError(f"{what} failed 3x")


def backfill(table: str, limit: int | None) -> int:
    col_list = ", ".join(EPO_COLS)
    missing: list[int] = []
    for bucket in range(RANGE_LO // BUCKET, RANGE_HI // BUCKET + 1):
        lo = max(RANGE_LO, bucket * BUCKET)
        hi = min(RANGE_HI, (bucket + 1) * BUCKET - 1)
        block_gap = _missing_blocks(table, lo, hi)
        if block_gap:
            print(f"  bucket {bucket}: {len(block_gap):,} blocks missing", flush=True)
        missing.extend(block_gap)
    if not missing:
        print(f"  {table}: nothing missing", flush=True)
        return 0

    chunks = [missing[i:i + CHUNK_BLOCKS] for i in range(0, len(missing), CHUNK_BLOCKS)]
    if limit is not None:
        chunks = chunks[:limit]
    print(f"  {table}: {len(missing):,} blocks, {len(chunks):,} chunks", flush=True)

    state = {"epo": get_client("ethpandaops"), "loc": get_client("primary")}
    total = 0
    try:
        for i, chunk in enumerate(chunks, 1):
            in_list = ",".join(str(b) for b in chunk)
            sql = (f"SELECT {col_list} FROM {table} "
                   f"WHERE meta_network_name = '{NETWORK}' AND block_number IN ({in_list})")

            def _recycle_epo():
                state["epo"].close()
                state["epo"] = get_client("ethpandaops")

            rows = _retry(lambda: state["epo"].query(sql).result_rows, "epo query",
                          _recycle_epo)
            if not rows:
                continue
            data = [(*r, MAINNET_ID) for r in rows]

            def _recycle_loc():
                state["loc"].close()
                state["loc"] = get_client("primary")

            _retry(lambda: state["loc"].insert(table=table, data=data,
                                               column_names=INSERT_COLS),
                   "local insert", _recycle_loc)
            total += len(rows)
            if i % 20 == 0 or i == len(chunks):
                print(f"    [{i}/{len(chunks)}] blocks {chunk[0]:,}..{chunk[-1]:,} "
                      f"+{len(rows):,}; total {total:,}", flush=True)
    finally:
        state["epo"].close()
        state["loc"].close()
    return total


def main() -> None:
    limit = int(os.environ["LIMIT"]) if "LIMIT" in os.environ else None
    for table in TABLES:
        print(f"=== {table} [{RANGE_LO:,}, {RANGE_HI:,}] ===", flush=True)
        rows = backfill(table, limit)
        print(f"  done: {rows:,} rows inserted\n", flush=True)


if __name__ == "__main__":
    main()
