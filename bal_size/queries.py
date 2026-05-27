"""SQL builders for the bal_size analysis.

Each builder returns a ClickHouse SQL string that aggregates one canonical_execution_*
table (or a join of two) to per-block BAL component counts over a half-open-free block
range ``[lo, hi]``. Unique counts use ``uniq`` (HyperLogLog, ~1% error), matching the
other analyses; the read-only anti-join already deduplicates, so it uses ``count``.

Source split (see config.py): the ``local_*`` builders run on the personal ``primary``
node; the ``epo_*`` builders run on ``ethpandaops`` (the only place the *_reads tables
exist). The read-only and address-union builders therefore live on ethpandaops, where
both reads and diffs are available for an exact per-block anti-join / union.
"""

from __future__ import annotations

from bal_size.config import NETWORK

_NET = f"meta_network_name='{NETWORK}'"


def _range(lo: int, hi: int) -> str:
    return f"block_number BETWEEN {lo} AND {hi}"


def local_storage_perblock(lo: int, hi: int) -> str:
    """Per-block storage write slots (SlotChanges) and write entries (StorageChange)."""
    return f"""
SELECT
    block_number,
    uniq((address, slot)) AS n_write_slots,
    uniq((address, slot, transaction_index)) AS n_write_entries
FROM canonical_execution_storage_diffs
WHERE {_NET} AND {_range(lo, hi)}
GROUP BY block_number
"""


def local_balance_perblock(lo: int, hi: int) -> str:
    """Per-block balance-change entries: one BalanceChange per (address, tx)."""
    return f"""
SELECT block_number, uniq((address, transaction_index)) AS n_balance_changes
FROM canonical_execution_balance_diffs
WHERE {_NET} AND {_range(lo, hi)}
GROUP BY block_number
"""


def local_nonce_perblock(lo: int, hi: int) -> str:
    """Per-block nonce-change entries: one NonceChange per (address, tx)."""
    return f"""
SELECT block_number, uniq((address, transaction_index)) AS n_nonce_changes
FROM canonical_execution_nonce_diffs
WHERE {_NET} AND {_range(lo, hi)}
GROUP BY block_number
"""


def local_contracts_perblock(lo: int, hi: int) -> str:
    """Per-block new-contract count (CodeChange) and total deployed bytecode bytes."""
    return f"""
SELECT block_number, count() AS n_contracts, sum(n_code_bytes) AS code_bytes
FROM canonical_execution_contracts
WHERE {_NET} AND {_range(lo, hi)}
GROUP BY block_number
"""


def epo_addresses_perblock(lo: int, hi: int) -> str:
    """Per-block unique touched addresses across all six access tables.

    Every account whose balance/nonce/storage/code is changed or read appears once as an
    ``AccountChanges`` entry; touched-only accounts (balance/nonce reads) contribute only
    their address, so they enter this union but produce no separate count column.
    """
    return f"""
SELECT block_number, uniq(addr) AS n_addresses
FROM (
    SELECT block_number, address AS addr FROM canonical_execution_storage_diffs
      WHERE {_NET} AND {_range(lo, hi)}
    UNION ALL
    SELECT block_number, contract_address AS addr FROM canonical_execution_storage_reads
      WHERE {_NET} AND {_range(lo, hi)}
    UNION ALL
    SELECT block_number, address AS addr FROM canonical_execution_balance_diffs
      WHERE {_NET} AND {_range(lo, hi)}
    UNION ALL
    SELECT block_number, address AS addr FROM canonical_execution_balance_reads
      WHERE {_NET} AND {_range(lo, hi)}
    UNION ALL
    SELECT block_number, address AS addr FROM canonical_execution_nonce_diffs
      WHERE {_NET} AND {_range(lo, hi)}
    UNION ALL
    SELECT block_number, address AS addr FROM canonical_execution_nonce_reads
      WHERE {_NET} AND {_range(lo, hi)}
)
GROUP BY block_number
"""


def epo_readonly_perblock(lo: int, hi: int) -> str:
    """Per-block read-only storage slots: slots read (SLOAD) but not written that block.

    EIP-7928 places a slot in exactly one of ``storage_changes`` or ``storage_reads``; a
    slot both read and written belongs to the write list. xatu logs it in both tables, so
    we anti-join the distinct read (address, slot) set against the distinct write set per
    block. The reads are pre-deduplicated, so a plain ``count`` is exact.
    """
    return f"""
SELECT r.block_number AS block_number, count() AS n_read_only_slots
FROM (
    SELECT DISTINCT block_number, contract_address AS address, slot
    FROM canonical_execution_storage_reads
    WHERE {_NET} AND {_range(lo, hi)}
) AS r
LEFT ANTI JOIN (
    SELECT DISTINCT block_number, address, slot
    FROM canonical_execution_storage_diffs
    WHERE {_NET} AND {_range(lo, hi)}
) AS w
ON r.block_number = w.block_number AND r.address = w.address AND r.slot = w.slot
GROUP BY r.block_number
"""


def write_slots_for_blocks(blocks: list[int]) -> str:
    """Per-block storage write slots for an explicit block list (cross-check helper).

    Used to compute the same ``n_write_slots`` on ethpandaops for a sample of blocks and
    compare it against the local node's value.
    """
    block_list = ", ".join(str(b) for b in blocks)
    return f"""
SELECT block_number, uniq((address, slot)) AS n_write_slots
FROM canonical_execution_storage_diffs
WHERE {_NET} AND block_number IN ({block_list})
GROUP BY block_number
"""
