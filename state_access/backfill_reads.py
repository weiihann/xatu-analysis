"""Backfill missing blocks for the three `canonical_execution_*_reads` tables.

Compares unique block_numbers in `[15_537_394, 25_189_620]` between ethpandaops (source) and
the local primary cluster (sink) within the affected 100k buckets, then pulls and inserts the
missing rows. Inserts target the Distributed wrapper; ClickHouse routes to the right shard.

Idempotent: the underlying tables are `ReplicatedReplacingMergeTree`, so re-running this just
reinserts the same rows and gets deduped on merge.

    uv run python -m state_access.backfill_reads
"""

from __future__ import annotations

from lib.clickhouse import get_client, run_query

RANGE_LO, RANGE_HI = 15_537_394, 25_189_620

# Buckets with known mismatches, from the earlier audit. We re-derive the actual missing
# block sets at runtime so re-runs after a partial backfill stay correct.
AFFECTED_BUCKETS: dict[str, list[int]] = {
    "canonical_execution_storage_reads": [228, 240, 251],
    "canonical_execution_balance_reads": [157, 226, 227, 228, 240, 241, 251],
    "canonical_execution_nonce_reads": [226, 227, 240, 241, 251],
}
BUCKET = 100_000

# Local schema lacks meta_network_id (which epo has). Everything else matches; UInt32 widens
# to UInt64 implicitly for storage_reads' block_number/transaction_index.
COMMON_COLS = {
    "canonical_execution_storage_reads": [
        "updated_date_time", "block_number", "transaction_index", "transaction_hash",
        "internal_index", "contract_address", "slot", "value", "meta_network_name",
    ],
    "canonical_execution_balance_reads": [
        "updated_date_time", "block_number", "transaction_index", "transaction_hash",
        "internal_index", "address", "balance", "meta_network_name",
    ],
    "canonical_execution_nonce_reads": [
        "updated_date_time", "block_number", "transaction_index", "transaction_hash",
        "internal_index", "address", "nonce", "meta_network_name",
    ],
}

CHUNK_BLOCKS = 50  # blocks per pull+insert round-trip


def missing_blocks_in_bucket(table: str, bucket: int) -> list[int]:
    """Block numbers that exist on epo but not locally within the 100k window starting at bucket."""
    lo = max(RANGE_LO, bucket * BUCKET)
    hi = min(RANGE_HI, (bucket + 1) * BUCKET - 1)
    epo_q = (f"SELECT DISTINCT block_number FROM {table} "
             f"WHERE meta_network_name='mainnet' AND block_number BETWEEN {lo} AND {hi}")
    epo = set(int(x) for x in run_query(epo_q, profile="ethpandaops")["block_number"])
    loc = set(int(x) for x in run_query(epo_q, profile="primary")["block_number"])
    return sorted(epo - loc)


def backfill_table(table: str) -> tuple[int, int]:
    """Pull missing rows from epo and insert into local. Returns (blocks_filled, rows_inserted)."""
    cols = COMMON_COLS[table]
    col_list = ", ".join(cols)

    all_missing: list[int] = []
    for b in AFFECTED_BUCKETS[table]:
        all_missing.extend(missing_blocks_in_bucket(table, b))
    if not all_missing:
        print(f"  {table}: no missing blocks, skipping")
        return 0, 0

    print(f"  {table}: {len(all_missing)} blocks to backfill")
    epo = get_client("ethpandaops")
    loc = get_client("primary")
    try:
        total_rows = 0
        for i in range(0, len(all_missing), CHUNK_BLOCKS):
            chunk = all_missing[i:i + CHUNK_BLOCKS]
            in_list = ",".join(str(b) for b in chunk)
            sql = (f"SELECT {col_list} FROM {table} "
                   f"WHERE meta_network_name='mainnet' AND block_number IN ({in_list})")
            qr = epo.query(sql)
            rows = qr.result_rows
            if not rows:
                continue
            loc.insert(table=table, data=rows, column_names=cols)
            total_rows += len(rows)
            print(f"    [{i + len(chunk):>5}/{len(all_missing)}] +{len(rows):>7,} rows "
                  f"(blocks {chunk[0]}..{chunk[-1]})")
        return len(all_missing), total_rows
    finally:
        epo.close()
        loc.close()


def main() -> None:
    print(f"Backfilling missing _reads rows over [{RANGE_LO:,}, {RANGE_HI:,}]\n")
    totals = []
    for table in COMMON_COLS:
        print(f"\n>>> {table}")
        blocks, rows = backfill_table(table)
        totals.append((table, blocks, rows))

    print("\n=== Summary ===")
    for table, blocks, rows in totals:
        print(f"  {table}: {blocks} blocks, {rows:,} rows inserted")


if __name__ == "__main__":
    main()
