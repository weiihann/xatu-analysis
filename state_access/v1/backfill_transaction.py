"""Backfill missing blocks in `canonical_execution_transaction` from ethpandaops to local.

Only fills the three known gap buckets from the audit: 230, 232, 251 — roughly 35k blocks
total. Pre-23M coverage on local is already more complete than ethpandaops, so we don't
touch it.

Schema differences epo→local:
  - `gas_price`        UInt128 → UInt64 (CAST; gas prices fit UInt64 in practice)
  - `transaction_type` UInt8   → UInt32 (CAST; always safe to widen)
  - `meta_network_id` is only on local; we set it to `1` for mainnet on insert.

Idempotent — the underlying table is `ReplicatedMergeTree`-family, and duplicate-key
inserts on (block, tx_hash) will not corrupt anything. Re-running after a partial run
re-discovers the still-missing blocks at runtime.

    uv run python -m state_access.v1.backfill_transaction
"""

from __future__ import annotations

import time

from lib.clickhouse import get_client, run_query

TABLE = "canonical_execution_transaction"
BUCKET = 100_000
AFFECTED_BUCKETS = [230, 232, 251]
RANGE_HI = 25_189_620  # caps bucket 251 — we don't reach beyond the analysis anchor + tail

# Local table's column order; we emit values in this order during insert.
LOCAL_COLS = [
    "updated_date_time", "block_number", "transaction_index", "transaction_hash",
    "nonce", "from_address", "to_address", "value", "input",
    "gas_limit", "gas_used", "gas_price", "transaction_type",
    "max_priority_fee_per_gas", "max_fee_per_gas",
    "success", "n_input_bytes", "n_input_zero_bytes", "n_input_nonzero_bytes",
    "meta_network_id", "meta_network_name",
]
# Columns to SELECT from epo, with casts where the type differs. `meta_network_id` is
# materialised as a literal `1` (mainnet) since epo doesn't have it.
EPO_SELECT = ", ".join([
    "updated_date_time", "block_number", "transaction_index", "transaction_hash",
    "nonce", "from_address", "to_address", "value", "input",
    "gas_limit", "gas_used",
    "toUInt64(gas_price) AS gas_price",
    "toUInt32(transaction_type) AS transaction_type",
    "max_priority_fee_per_gas", "max_fee_per_gas",
    "success", "n_input_bytes", "n_input_zero_bytes", "n_input_nonzero_bytes",
    "toInt32(1) AS meta_network_id",
    "meta_network_name",
])

CHUNK_BLOCKS = 30  # tx rows are heavy (input column can be large), so smaller than _reads


def missing_blocks_in_bucket(bucket: int) -> list[int]:
    """Block numbers in `bucket` that exist on epo but not locally, clipped by RANGE_HI."""
    lo, hi = bucket * BUCKET, min((bucket + 1) * BUCKET - 1, RANGE_HI)
    sql = (f"SELECT DISTINCT block_number FROM {TABLE} "
           f"WHERE meta_network_name='mainnet' AND block_number BETWEEN {lo} AND {hi}")
    epo_df = run_query(sql, profile="ethpandaops")
    loc_df = run_query(sql, profile="primary")
    epo = set(int(x) for x in epo_df["block_number"]) if "block_number" in epo_df else set()
    loc = set(int(x) for x in loc_df["block_number"]) if "block_number" in loc_df else set()
    return sorted(epo - loc)


def main() -> None:
    print(f"Backfilling {TABLE}: buckets {AFFECTED_BUCKETS}\n")

    all_missing: list[int] = []
    for b in AFFECTED_BUCKETS:
        m = missing_blocks_in_bucket(b)
        print(f"  bucket {b}: {len(m):>6,} blocks missing")
        all_missing.extend(m)
    if not all_missing:
        print("\nNo missing blocks. Done.")
        return
    print(f"\nTotal: {len(all_missing):,} blocks to backfill\n")

    epo = get_client("ethpandaops")
    loc = get_client("primary")
    try:
        total_rows = 0
        for i in range(0, len(all_missing), CHUNK_BLOCKS):
            chunk = all_missing[i:i + CHUNK_BLOCKS]
            in_list = ",".join(str(b) for b in chunk)
            sql = (f"SELECT {EPO_SELECT} FROM {TABLE} "
                   f"WHERE meta_network_name='mainnet' AND block_number IN ({in_list})")
            rows = None
            for attempt in range(3):
                try:
                    rows = epo.query(sql).result_rows
                    break
                except Exception as e:
                    backoff = 5 * (2 ** attempt)
                    print(f"    !! epo query failed (attempt {attempt + 1}/3): {str(e)[:120]}; "
                          f"sleeping {backoff}s")
                    time.sleep(backoff)
                    epo.close()
                    epo = get_client("ethpandaops")
            if rows is None:
                raise RuntimeError(f"epo query failed 3x for chunk starting at block {chunk[0]}")
            if not rows:
                continue
            inserted = False
            for attempt in range(3):
                try:
                    loc.insert(table=TABLE, data=rows, column_names=LOCAL_COLS)
                    inserted = True
                    break
                except Exception as e:
                    backoff = 5 * (2 ** attempt)
                    print(f"    !! local insert failed (attempt {attempt + 1}/3): "
                          f"{str(e)[:120]}; sleeping {backoff}s")
                    time.sleep(backoff)
                    loc.close()
                    loc = get_client("primary")
            if not inserted:
                raise RuntimeError(f"local insert failed 3x for chunk starting at block {chunk[0]}")
            total_rows += len(rows)
            print(f"    [{i + len(chunk):>6,}/{len(all_missing):,}] +{len(rows):>7,} rows "
                  f"(blocks {chunk[0]:,}..{chunk[-1]:,}); total {total_rows:,}")
    finally:
        epo.close()
        loc.close()

    print(f"\nDone. {len(all_missing):,} blocks, {total_rows:,} rows inserted.")


if __name__ == "__main__":
    main()
