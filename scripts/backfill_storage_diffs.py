"""Backfill canonical_execution_storage_diffs gaps on the primary node from ethpandaops.

The personal node has storage-diff ingestion gaps (see state_access/README.md). This copies
the missing block ranges from the ethPandaOps Xatu cluster (which has ~99.9% coverage) into
the primary node, chunk by chunk.

Safe to re-run: the table is ReplacingMergeTree (dedups on merge), and chunks already present
on the target are skipped. Interruptions lose at most the in-flight chunk.

Run with:  uv run python -m scripts.backfill_storage_diffs
"""

from __future__ import annotations

import time

from clickhouse_connect.driver.exceptions import OperationalError

from lib.clickhouse import get_client

NETWORK = "mainnet"
TABLE = "canonical_execution_storage_diffs"
CHUNK_BLOCKS = 2_000
COMPLETE_THRESHOLD = 0.95  # skip a chunk already at least this covered on the target
MAX_ATTEMPTS = 5           # transient Tailscale/HTTP stalls are retried with reconnect

# Inclusive block ranges to backfill — the identified ingestion gaps (see README data note).
GAP_RANGES: list[tuple[int, int]] = [
    (23_060_000, 23_115_000),  # secondary dip (~early Sep 2025)
    (23_260_000, 23_667_000),  # main ~54-day gap
]


def coverage(client, lo: int, hi: int) -> float:
    """Fraction of blocks in [lo, hi] that have at least one storage-diff row on `client`."""
    sql = (f"SELECT uniqExact(block_number) FROM {TABLE} "
           f"WHERE meta_network_name='{NETWORK}' AND block_number BETWEEN {lo} AND {hi}")
    present = client.query(sql).result_rows[0][0]
    return present / (hi - lo + 1)


def backfill_chunk(src, tgt, lo: int, hi: int) -> int:
    """Copy one block range from src to tgt; returns rows inserted."""
    sql = (f"SELECT * FROM {TABLE} "
           f"WHERE meta_network_name='{NETWORK}' AND block_number BETWEEN {lo} AND {hi}")
    table = src.query_arrow(sql)
    if table.num_rows:
        tgt.insert_arrow(TABLE, table)
    return table.num_rows


def main() -> None:
    src = get_client("ethpandaops")
    tgt = get_client("primary")
    try:
        for lo, hi in GAP_RANGES:
            print(f"Range {lo:,}–{hi:,} ({(hi - lo) // CHUNK_BLOCKS + 1} chunks)", flush=True)
            block = lo
            while block <= hi:
                chunk_hi = min(block + CHUNK_BLOCKS - 1, hi)
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        before = coverage(tgt, block, chunk_hi)
                        if before >= COMPLETE_THRESHOLD:
                            print(f"  {block:,}–{chunk_hi:,}: already {before:.0%}, skip", flush=True)
                        else:
                            rows = backfill_chunk(src, tgt, block, chunk_hi)
                            print(f"  {block:,}–{chunk_hi:,}: inserted {rows:,} rows "
                                  f"(was {before:.0%})", flush=True)
                        break
                    except OperationalError as exc:
                        if attempt == MAX_ATTEMPTS:
                            raise
                        delay = 10 * attempt
                        print(f"  {block:,}–{chunk_hi:,}: attempt {attempt} failed "
                              f"({type(exc).__name__}); reconnecting, retry in {delay}s", flush=True)
                        src.close()
                        tgt.close()
                        time.sleep(delay)
                        src = get_client("ethpandaops")
                        tgt = get_client("primary")
                block = chunk_hi + 1
    finally:
        src.close()
        tgt.close()
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
