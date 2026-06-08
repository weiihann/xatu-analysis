"""SQL builders for the v2 hot/cold state analysis.

One query per `(window_days, object_type)` returns rows of `(slice, n_w, n_r, n_keys)`,
where:

- `slice` is `'w_only'` / `'r_only'` / `'rw'` — the partition of all keys touched in the
  trailing-W window.
- `n_w`, `n_r` are the per-key write-access count and read-access count.
- `n_keys` is how many distinct keys have that exact `(n_w, n_r)` pair within the slice.

From these histograms the collect/analysis layer derives:

- Q1 — set sizes `|W|`, `|R|`, `|R∩W|`, `|R∪W|`, `|W-only|`, `|R-only|` (sum n_keys by slice).
- Q2 — access-frequency bin distribution (bin n_w / n_r in Python).
- Q3 — top-1% / top-10% concentration (walk the histogram from the high-count tail).

A single inner `GROUP BY key` carries all three questions, instead of a separate scan per
question. The grouping key is `cityHash64` (UInt64) rather than the raw string, so the
distributed shuffle stays compact even at W=365.
"""

from __future__ import annotations

from state_access.config import BLOCKS_PER_DAY, NETWORK

# address_appearances relationships that count as account-level reads.
# Excludes erc20_* / erc721_* (log-emission artifacts, not state access).
READ_RELATIONSHIPS = (
    "call_from", "call_to", "tx_from", "tx_to",
    "miner_fee", "factory", "create", "suicide_refund", "suicide",
)
_RELATIONSHIP_LIST = ", ".join(f"'{r}'" for r in READ_RELATIONSHIPS)


def _window(bn_now: int, days: int) -> tuple[int, int]:
    """Inclusive block range for a trailing W-day window ending at `bn_now`."""
    return bn_now - days * BLOCKS_PER_DAY, bn_now


def slot_histogram(bn_now: int, days: int) -> str:
    """(slice, n_w, n_r, n_keys) over storage-slot keys for the trailing W-day window.

    Slot writes come from `storage_diffs`. Slot reads come from `storage_reads`. A key is
    `cityHash64(address, slot)`. The inner UNION ALL tags each row as write or read with
    flags; the outer GROUP BY h sums them per key; the slice is the 3-way partition by
    `(n_w > 0, n_r > 0)`; the final GROUP BY collapses to the access-count histogram.
    """
    bn_lo, bn_hi = _window(bn_now, days)
    return f"""
WITH per_key AS (
    SELECT
        h,
        sum(is_w) AS n_w,
        sum(is_r) AS n_r
    FROM (
        SELECT cityHash64(address, slot) AS h, 1 AS is_w, 0 AS is_r
        FROM canonical_execution_storage_diffs
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(contract_address, slot) AS h, 0 AS is_w, 1 AS is_r
        FROM canonical_execution_storage_reads
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
    )
    GROUP BY h
)
SELECT
    multiIf(n_w > 0 AND n_r > 0, 'rw',
            n_w > 0,             'w_only',
                                 'r_only') AS slice,
    n_w,
    n_r,
    count() AS n_keys
FROM per_key
GROUP BY slice, n_w, n_r
ORDER BY slice, n_w, n_r
"""


_ZERO = "0x" + "0" * 64


def slot_typed_histogram(bn_now: int, days: int) -> str:
    """Per-key event counts split by value-transition type, for storage slots.

    Splits writes by `(from_value, to_value)` transition and reads by their returned `value`.

    Write types (from `storage_diffs`):
      - `create`: `from_value = 0`, `to_value != 0` (~20k gas, always Inactive-priced)
      - `update`: `from_value != 0`, `to_value != 0` (~5k gas, the EIP-8188-relevant case)
      - `delete`: `from_value != 0`, `to_value = 0` (refund)

    Read types (from `storage_reads`):
      - `r_zero`: `value = 0` (empty-slot probe — "does this exist?" checks)
      - `r_nonzero`: `value != 0` (populated state inspection)

    Returns rows of `(n_w_create, n_w_update, n_w_delete, n_r_zero, n_r_nonzero, n_keys)`
    per distinct count-tuple. The same per-key GROUP BY as `slot_histogram` but with five
    typed counters instead of two — same shuffle cost, larger output cardinality.
    """
    bn_lo, bn_hi = _window(bn_now, days)
    return f"""
WITH per_key AS (
    SELECT
        h,
        sum(is_w_create) AS n_w_create,
        sum(is_w_update) AS n_w_update,
        sum(is_w_delete) AS n_w_delete,
        sum(is_r_zero)   AS n_r_zero,
        sum(is_r_nonzero) AS n_r_nonzero
    FROM (
        SELECT
            cityHash64(address, slot) AS h,
            toUInt8(from_value =  '{_ZERO}' AND to_value != '{_ZERO}') AS is_w_create,
            toUInt8(from_value != '{_ZERO}' AND to_value != '{_ZERO}') AS is_w_update,
            toUInt8(from_value != '{_ZERO}' AND to_value =  '{_ZERO}') AS is_w_delete,
            toUInt8(0) AS is_r_zero,
            toUInt8(0) AS is_r_nonzero
        FROM canonical_execution_storage_diffs
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT
            cityHash64(contract_address, slot) AS h,
            toUInt8(0) AS is_w_create,
            toUInt8(0) AS is_w_update,
            toUInt8(0) AS is_w_delete,
            toUInt8(value =  '{_ZERO}') AS is_r_zero,
            toUInt8(value != '{_ZERO}') AS is_r_nonzero
        FROM canonical_execution_storage_reads
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
    )
    GROUP BY h
)
SELECT
    n_w_create, n_w_update, n_w_delete, n_r_zero, n_r_nonzero,
    count() AS n_keys
FROM per_key
GROUP BY n_w_create, n_w_update, n_w_delete, n_r_zero, n_r_nonzero
ORDER BY n_w_create, n_w_update, n_w_delete, n_r_zero, n_r_nonzero
"""


def slot_update_coverage(bn_now: int, days: int) -> str:
    """Per-window count of update SSTORE events split into warm vs cold under EIP-8188.

    Definition (matches EIP-8188's Active-tier semantics within a single trailing window):
    an update event at (block, tx_index, internal_index) is **warm** iff the same slot has
    at least one prior create-or-update event in the same window. A deletion is not a
    warming event. The first update-or-create on a slot in window is, by definition, the
    warming event itself — if it's an update, that update is cold; subsequent updates on
    the slot are warm.

    Algorithm (one GROUP BY on `cityHash64(address, slot)`):
      - For each slot, count update events in window (`n_update`).
      - Identify whether the slot's earliest create-or-update event is an update (vs a
        create). If it's an update: `warm = n_update - 1` (the first update is cold).
        If it's a create: `warm = n_update` (the create primed the slot Active before
        any update in window).

    Returns a single row: `(total_updates, warm_updates, cold_updates, pct_warm)`.
    """
    bn_lo, bn_hi = _window(bn_now, days)
    # event_order packs (block, tx_index, internal_index) into a single UInt64 monotone in
    # event time. transaction_index < 2^32, internal_index < 2^32 in practice; pack with
    # generous bit widths and leave a gap so deletion sentinels can't collide.
    return f"""
WITH slot_events AS (
    SELECT
        cityHash64(address, slot) AS h,
        (toUInt64(block_number) * 1000000000) + (toUInt64(transaction_index) * 100000)
            + toUInt64(internal_index) AS event_order,
        (from_value != '{_ZERO}' AND to_value != '{_ZERO}') AS is_update,
        (from_value =  '{_ZERO}' AND to_value != '{_ZERO}') AS is_create
    FROM canonical_execution_storage_diffs
    WHERE meta_network_name = '{NETWORK}'
      AND block_number BETWEEN {bn_lo} AND {bn_hi}
),
per_slot AS (
    SELECT
        h,
        countIf(is_update) AS n_update,
        -- Among create/update events only: pick the is_update flag of the earliest one.
        -- For non-create/update events (deletions), set event_order to UInt64 max so they
        -- never become argmin. If the first create-or-update event is an UPDATE
        -- (`first_cu_is_update = 1`), that update is the cold one; all others on this
        -- slot are warm. If it's a CREATE, the create primed the slot Active so every
        -- update on the slot is warm.
        -- argMinIf picks the `is_update` flag of the row with the smallest event_order
        -- among rows satisfying `is_create OR is_update`. Deletion rows are filtered out.
        argMinIf(is_update, event_order, is_create OR is_update) AS first_cu_is_update
    FROM slot_events
    GROUP BY h
    HAVING n_update > 0
)
SELECT
    sum(n_update) AS total_updates,
    -- Warm count per slot = n_update - 1 if first_cu is an update; else n_update.
    -- Equivalent: n_update - first_cu_is_update (which is 0 or 1).
    sum(n_update - toUInt64(first_cu_is_update)) AS warm_updates,
    sum(toUInt64(first_cu_is_update)) AS cold_updates,
    round(100.0 * warm_updates / total_updates, 4) AS pct_warm
FROM per_slot
"""


def account_histogram(bn_now: int, days: int) -> str:
    """(slice, n_w, n_r, n_keys) over account-address keys for the trailing W-day window.

    Account writes come from `balance_diffs`, `nonce_diffs`, and `contracts` (account
    creation — keyed by `contract_address`). Account reads come from `balance_reads`,
    `nonce_reads`, and `address_appearances` filtered to non-ERC* relationships. The key
    is `cityHash64(address)`.
    """
    bn_lo, bn_hi = _window(bn_now, days)
    return f"""
WITH per_key AS (
    SELECT
        h,
        sum(is_w) AS n_w,
        sum(is_r) AS n_r
    FROM (
        SELECT cityHash64(address) AS h, 1 AS is_w, 0 AS is_r
        FROM canonical_execution_balance_diffs
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address) AS h, 1 AS is_w, 0 AS is_r
        FROM canonical_execution_nonce_diffs
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(contract_address) AS h, 1 AS is_w, 0 AS is_r
        FROM canonical_execution_contracts
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address) AS h, 0 AS is_w, 1 AS is_r
        FROM canonical_execution_balance_reads
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address) AS h, 0 AS is_w, 1 AS is_r
        FROM canonical_execution_nonce_reads
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address) AS h, 0 AS is_w, 1 AS is_r
        FROM canonical_execution_address_appearances
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
          AND relationship IN ({_RELATIONSHIP_LIST})
    )
    GROUP BY h
)
SELECT
    multiIf(n_w > 0 AND n_r > 0, 'rw',
            n_w > 0,             'w_only',
                                 'r_only') AS slice,
    n_w,
    n_r,
    count() AS n_keys
FROM per_key
GROUP BY slice, n_w, n_r
ORDER BY slice, n_w, n_r
"""
