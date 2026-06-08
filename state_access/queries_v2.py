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
