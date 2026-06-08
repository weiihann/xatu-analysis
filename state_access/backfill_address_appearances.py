"""Backfill `canonical_execution_address_appearances` over `[20,501,000, 25,189,620]`.

Targets the mid-range partial holes and tail; deliberately skips the pre-Dencun front
(`[15,537,394, 19,399,999]`) which is a coverage gap, not a backfill failure, and is large
enough (~12B rows) to warrant a separate decision.

Discovers missing blocks per bucket at runtime, so partial re-runs are safe (the underlying
table is `ReplicatedReplacingMergeTree` — duplicates get merged out).

    uv run python -m state_access.backfill_address_appearances
"""

from __future__ import annotations

import time

from lib.clickhouse import get_client, run_query

TABLE = "canonical_execution_address_appearances"
RANGE_LO, RANGE_HI = 20_501_000, 25_189_620
BUCKET = 100_000

# Buckets known to have gaps in this range (from the audit).
# Bucket 233 is entirely empty within range; others are partial.
AFFECTED_BUCKETS = [205, 230, 231, 232, 233, 234, 240, 241, 251]

# Schema is identical on epo and local — all 8 columns transfer.
COLS = [
    "updated_date_time", "block_number", "transaction_hash", "internal_index",
    "address", "relationship", "meta_network_id", "meta_network_name",
]

# Per-block density is ~3,200 rows; 30 blocks/chunk -> ~100k rows/insert, matches the
# _reads backfill pace where insert throughput stayed stable.
CHUNK_BLOCKS = 15


def missing_blocks_in_bucket(bucket: int) -> list[int]:
    """Block numbers in `bucket` that exist on epo but not locally, clipped to [LO, HI]."""
    lo = max(RANGE_LO, bucket * BUCKET)
    hi = min(RANGE_HI, (bucket + 1) * BUCKET - 1)
    sql = (f"SELECT DISTINCT block_number FROM {TABLE} "
           f"WHERE meta_network_name='mainnet' AND block_number BETWEEN {lo} AND {hi}")
    epo_df = run_query(sql, profile="ethpandaops")
    loc_df = run_query(sql, profile="primary")
    # An empty DataFrame from clickhouse_connect drops the column too, so guard before access.
    epo = set(int(x) for x in epo_df["block_number"]) if "block_number" in epo_df else set()
    loc = set(int(x) for x in loc_df["block_number"]) if "block_number" in loc_df else set()
    return sorted(epo - loc)


def main() -> None:
    print(f"Backfilling {TABLE} over [{RANGE_LO:,}, {RANGE_HI:,}]\n")
    col_list = ", ".join(COLS)

    # Discover all missing blocks up front so the progress bar is meaningful.
    all_missing: list[int] = []
    for b in AFFECTED_BUCKETS:
        m = missing_blocks_in_bucket(b)
        print(f"  bucket {b}: {len(m):>7,} blocks missing")
        all_missing.extend(m)

    if not all_missing:
        print("\nNo missing blocks. Done.")
        return

    print(f"\nTotal: {len(all_missing):,} blocks to backfill")

    epo = get_client("ethpandaops")
    loc = get_client("primary")
    try:
        total_rows = 0
        for i in range(0, len(all_missing), CHUNK_BLOCKS):
            chunk = all_missing[i:i + CHUNK_BLOCKS]
            in_list = ",".join(str(b) for b in chunk)
            sql = (f"SELECT {col_list} FROM {TABLE} "
                   f"WHERE meta_network_name='mainnet' AND block_number IN ({in_list})")
            rows = None
            for attempt in range(3):
                try:
                    rows = epo.query(sql).result_rows
                    break
                except Exception as e:
                    backoff = 5 * (2 ** attempt)
                    print(f"    !! epo query failed (attempt {attempt + 1}/3): {str(e)[:120]}; "
                          f"sleeping {backoff}s then retrying")
                    time.sleep(backoff)
                    # Recycle the epo client in case the connection itself is bad.
                    epo.close()
                    epo = get_client("ethpandaops")
            if rows is None:
                raise RuntimeError(f"epo query failed 3x for chunk starting at block {chunk[0]}")
            if not rows:
                continue
            inserted = False
            for attempt in range(3):
                try:
                    loc.insert(table=TABLE, data=rows, column_names=COLS)
                    inserted = True
                    break
                except Exception as e:
                    backoff = 5 * (2 ** attempt)
                    print(f"    !! local insert failed (attempt {attempt + 1}/3): {str(e)[:120]}; "
                          f"sleeping {backoff}s then retrying")
                    time.sleep(backoff)
                    loc.close()
                    loc = get_client("primary")
            if not inserted:
                raise RuntimeError(f"local insert failed 3x for chunk starting at block {chunk[0]}")
            total_rows += len(rows)
            print(f"    [{i + len(chunk):>7,}/{len(all_missing):,}] +{len(rows):>7,} rows "
                  f"(blocks {chunk[0]:,}..{chunk[-1]:,}); total {total_rows:,}")
    finally:
        epo.close()
        loc.close()

    print(f"\nDone. {len(all_missing):,} blocks, {total_rows:,} rows inserted.")


if __name__ == "__main__":
    main()
