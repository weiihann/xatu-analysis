"""Backfill execution_state_size into the primary node from ethpandaops.

The state_delta analysis reads per-block live-state size (counts + bytes) from
execution_state_size. The primary node's copy is empty, and querying it locally is far
faster than hitting the ethPandaOps cluster for the heavy per-block pass, so this copies
the post-Merge rows for the two clients the analysis stitches together (see
state_delta/config.py): `manual-backfill` for the early range and a `tysm` live client for
the recent range.

Safe to re-run: the table is ReplacingMergeTree (dedups on merge), and chunks already
present on the target are skipped. Interruptions lose at most the in-flight chunk.

Run with:  uv run python -m scripts.backfill_state_size
"""

from __future__ import annotations

import time

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import OperationalError

from lib.clickhouse import get_client
from state_delta.config import CLIENTS, NETWORK, START_BLOCK

TABLE = "execution_state_size"
CHUNK_BLOCKS = 100_000
COMPLETE_RATIO = 0.99      # skip a chunk whose target row count is already this close to source
MAX_ATTEMPTS = 5           # transient Tailscale/HTTP stalls are retried with reconnect

_CLIENT_LIST = "(" + ", ".join(f"'{c}'" for c in CLIENTS) + ")"
_WHERE = (f"meta_network_name='{NETWORK}' AND meta_client_name IN {_CLIENT_LIST} "
          "AND block_number BETWEEN {lo} AND {hi}")


def row_count(client: Client, lo: int, hi: int) -> int:
    """Rows for the analysis clients in [lo, hi] on `client`."""
    sql = f"SELECT count() FROM {TABLE} WHERE " + _WHERE.format(lo=lo, hi=hi)
    return client.query(sql).result_rows[0][0]


def source_max_block(src: Client) -> int:
    """Highest block the analysis clients cover on the source."""
    sql = (f"SELECT max(block_number) FROM {TABLE} "
           f"WHERE meta_network_name='{NETWORK}' AND meta_client_name IN {_CLIENT_LIST}")
    return src.query(sql).result_rows[0][0]


def backfill_chunk(src: Client, tgt: Client, lo: int, hi: int) -> int:
    """Copy one block range from src to tgt; returns rows inserted."""
    sql = f"SELECT * FROM {TABLE} WHERE " + _WHERE.format(lo=lo, hi=hi)
    table = src.query_arrow(sql)
    if table.num_rows:
        tgt.insert_arrow(TABLE, table)
    return table.num_rows


def _copy_with_retry(src: Client, tgt: Client, lo: int, hi: int) -> tuple[Client, Client]:
    """Copy one chunk, reconnecting on transient errors. Returns the (src, tgt) clients."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            src_rows = row_count(src, lo, hi)
            tgt_rows = row_count(tgt, lo, hi)
            if src_rows == 0:
                print(f"  {lo:,}-{hi:,}: no source rows, skip", flush=True)
            elif tgt_rows >= COMPLETE_RATIO * src_rows:
                print(f"  {lo:,}-{hi:,}: already {tgt_rows:,}/{src_rows:,}, skip", flush=True)
            else:
                inserted = backfill_chunk(src, tgt, lo, hi)
                print(f"  {lo:,}-{hi:,}: inserted {inserted:,} rows (was {tgt_rows:,})", flush=True)
            return src, tgt
        except OperationalError as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            delay = 10 * attempt
            print(f"  {lo:,}-{hi:,}: attempt {attempt} failed ({type(exc).__name__}); "
                  f"reconnecting, retry in {delay}s", flush=True)
            src.close()
            tgt.close()
            time.sleep(delay)
            src = get_client("ethpandaops")
            tgt = get_client("primary")
    raise AssertionError("unreachable")  # loop either returns or raises


def main() -> None:
    src = get_client("ethpandaops")
    tgt = get_client("primary")
    try:
        end = source_max_block(src)
        chunks = (end - START_BLOCK) // CHUNK_BLOCKS + 1
        print(f"Backfilling {TABLE} blocks {START_BLOCK:,}-{end:,} ({chunks} chunks) "
              f"for clients {CLIENTS}", flush=True)
        block = START_BLOCK
        while block <= end:
            chunk_hi = min(block + CHUNK_BLOCKS - 1, end)
            src, tgt = _copy_with_retry(src, tgt, block, chunk_hi)
            block = chunk_hi + 1
    finally:
        src.close()
        tgt.close()
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
