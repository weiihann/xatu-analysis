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
      - `create`: `from_value = 0`, `to_value != 0` (~20k gas; no prior write age, so
        write-age tiering can't discount it)
      - `update`: `from_value != 0`, `to_value != 0` (~5k gas, the case tiering reprices)
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


def slot_first_op(bn_now: int, days: int) -> str:
    """Per-window count of slots split by what their FIRST operation in window is.

    Motivation: under a hypothetical "EIP-8188 with read-side period bumping" (the first
    read of an inactive slot would also bump its period — turning reads into write-like
    operations from the user's perspective), the policy cost falls on slots whose first
    in-window operation is a **nonzero read**. Zero reads can't bump the period because
    the slot has no period to bump (it doesn't exist yet at value=0). Writes already
    bump the period under base EIP-8188.

    Event ordering: `(block_number, transaction_index)`. `internal_index` is per-table
    (per-read-table or per-diff-table), not a single tx-wide opcode index, so it can't
    be used for cross-table ordering. Ties (same block, same tx) are broken **in favour
    of writes** (writes come before reads when both happen in the same tx) — a
    conservative choice that under-counts the "first-op-is-read" set rather than over-
    counting it.

    Returns one row: `(total_slots, first_is_write, first_is_zero_read,
    first_is_nonzero_read)`. The three subset counts sum to `total_slots` exactly.
    """
    bn_lo, bn_hi = _window(bn_now, days)
    # event_order packs (block, tx_idx, op_kind) so that within (block, tx_idx) the
    # write slot (op_kind=0) sorts before the read slot (op_kind=1).
    return f"""
WITH slot_events AS (
    SELECT
        cityHash64(address, slot) AS h,
        toUInt64(block_number) * 1000000 + toUInt64(transaction_index) * 10 + 0 AS event_order,
        toUInt8(0) AS is_read,
        toUInt8(0) AS read_is_nonzero
    FROM canonical_execution_storage_diffs
    WHERE meta_network_name = '{NETWORK}'
      AND block_number BETWEEN {bn_lo} AND {bn_hi}
    UNION ALL
    SELECT
        cityHash64(contract_address, slot) AS h,
        toUInt64(block_number) * 1000000 + toUInt64(transaction_index) * 10 + 1 AS event_order,
        toUInt8(1) AS is_read,
        toUInt8(value != '{_ZERO}') AS read_is_nonzero
    FROM canonical_execution_storage_reads
    WHERE meta_network_name = '{NETWORK}'
      AND block_number BETWEEN {bn_lo} AND {bn_hi}
),
per_slot AS (
    SELECT
        h,
        argMin(is_read,         event_order) AS first_is_read,
        argMin(read_is_nonzero, event_order) AS first_read_is_nonzero
    FROM slot_events
    GROUP BY h
)
SELECT
    count() AS total_slots,
    sum(toUInt8(first_is_read = 0)) AS first_is_write,
    sum(toUInt8(first_is_read = 1 AND first_read_is_nonzero = 0)) AS first_is_zero_read,
    sum(toUInt8(first_is_read = 1 AND first_read_is_nonzero = 1)) AS first_is_nonzero_read
FROM per_slot
"""


def account_first_op(bn_now: int, days: int) -> str:
    """Per-window count of accounts split by what their FIRST operation in window is.

    Same motivation as `slot_first_op` — for a hypothetical read-side period-bumping
    extension of EIP-8188, the policy cost falls on accounts whose first in-window
    operation is a **nonzero read** (i.e. the read returns a populated balance or nonce).

    Source tables:
      writes — `balance_diffs`, `nonce_diffs`, `contracts` (account creation)
      reads with values — `balance_reads.balance`, `nonce_reads.nonce`
      reads without values — `address_appearances` (relationship-filtered)

    Classification of the earliest event per account:
      first_is_write           — earliest event is from a write source
      first_is_nonzero_read    — earliest event is a balance_read with balance != 0
                                 OR a nonce_read with nonce != 0
      first_is_zero_read       — earliest event is balance_read with balance=0 or
                                 nonce_read with nonce=0
      first_is_appearance_read — earliest event is from address_appearances (no value
                                 column; account state at read time unknown without a
                                 cross-table join — reported separately)

    Tie-breaking at the same `(block, tx_idx)`: priority writes > nonzero reads > zero
    reads > appearance reads. Same ordering convention as `slot_first_op`.

    No JOIN against `canonical_execution_transaction`. Two simplifications make the
    `transaction_index` lookup unnecessary:

    - `canonical_execution_contracts` is dropped. Every contract creation also emits a
      `nonce_diff` (nonce 0->1 under EIP-161) on the same address at the same real
      transaction_index, so the account is already captured as a write via `nonce_diffs`
      with correct ordering. Dropping contracts changes neither the event set's first-op
      result nor the R∪W denominator (contract addresses ⊆ nonce_diff addresses).
    - `address_appearances` (which lacks `transaction_index`) is given an *end-of-block*
      sort position (`+ 9999999`) instead of a real tx_idx. Because the block number
      dominates `event_order`, cross-block ordering stays exact; within a block the
      appearance loses every tie — which matches the intended priority (appearance is the
      lowest). Empirically appearances never win the first-op anyway. This keeps
      appearance-only accounts in the R∪W denominator without needing their tx_idx.
    """
    bn_lo, bn_hi = _window(bn_now, days)
    # event_order packs (block, tx_idx, op_priority).
    #   op_priority: write=0, nonzero_read=1, zero_read=2; appearance uses end-of-block.
    # block multiplier 10_000_000 >> max(tx_idx*10 + priority), so blocks never overlap.
    return f"""
WITH all_events AS (
    SELECT
        cityHash64(address) AS h,
        toUInt64(block_number) * 10000000 + toUInt64(transaction_index) * 10 + 0 AS event_order,
        'write' AS op
    FROM canonical_execution_balance_diffs
    WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
    UNION ALL
    SELECT
        cityHash64(address) AS h,
        toUInt64(block_number) * 10000000 + toUInt64(transaction_index) * 10 + 0 AS event_order,
        'write' AS op
    FROM canonical_execution_nonce_diffs
    WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
    UNION ALL
    SELECT
        cityHash64(address) AS h,
        toUInt64(block_number) * 10000000 + toUInt64(transaction_index) * 10
            + if(balance != 0, 1, 2) AS event_order,
        if(balance != 0, 'nonzero_read', 'zero_read') AS op
    FROM canonical_execution_balance_reads
    WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
    UNION ALL
    SELECT
        cityHash64(address) AS h,
        toUInt64(block_number) * 10000000 + toUInt64(transaction_index) * 10
            + if(nonce != 0, 1, 2) AS event_order,
        if(nonce != 0, 'nonzero_read', 'zero_read') AS op
    FROM canonical_execution_nonce_reads
    WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
    UNION ALL
    SELECT
        cityHash64(address) AS h,
        toUInt64(block_number) * 10000000 + 9999999 AS event_order,
        'appearance_read' AS op
    FROM canonical_execution_address_appearances
    WHERE meta_network_name = '{NETWORK}'
      AND block_number BETWEEN {bn_lo} AND {bn_hi}
      AND relationship IN ({_RELATIONSHIP_LIST})
),
per_acct AS (
    SELECT
        h,
        argMin(op, event_order) AS first_op
    FROM all_events
    GROUP BY h
)
SELECT
    count() AS total_accounts,
    sum(toUInt8(first_op = 'write'))           AS first_is_write,
    sum(toUInt8(first_op = 'nonzero_read'))    AS first_is_nonzero_read,
    sum(toUInt8(first_op = 'zero_read'))       AS first_is_zero_read,
    sum(toUInt8(first_op = 'appearance_read')) AS first_is_appearance_read
FROM per_acct
"""


def account_r_empty_split(bn_now: int, days: int) -> str:
    """Of accounts in R (deduped against writes), how many are empty vs non-empty?

    Empty = balance is observed as 0 AND nonce is observed as 0 throughout the window.
    Since these accounts have NO writes in window (they're in R, not W), the balance
    and nonce are constant within window — so per-account aggregations
    `max(balance) = 0 AND max(nonce) = 0` correctly identifies empty accounts.

    Accounts in R that only appear via `address_appearances` (no balance/nonce reads at
    all) are reported separately as `unknown`: we have no value data for them within
    this query alone.

    Returns one row: `(total_r, empty_accounts, nonempty_accounts, unknown_accounts)`.

    Single GROUP BY, no JOINs. Each source row is tagged with per-event flags, then
    aggregated per account with `max`. Two things keep this cheap enough to run at
    W=365:

    - The per-group state is a handful of `UInt8` flags, not a `UInt256` balance. We only
      need to know whether a balance/nonce was *ever* nonzero, so we aggregate
      `max(balance != 0)` rather than `max(balance)` — one byte instead of 32.

    Unlike `account_first_op`, the write set here keeps `canonical_execution_contracts`:
    ~1% of contract-creation accounts have no `nonce_diff`/`balance_diff` in the same
    window, and dropping contracts would wrongly admit those (genuinely-written) accounts
    into R. No JOIN is needed — we only read the `contract_address` for the write flag.
    """
    bn_lo, bn_hi = _window(bn_now, days)
    return f"""
WITH per_acct AS (
    SELECT
        h,
        max(is_write)     AS any_write,
        max(bal_nonzero)  AS bal_nonzero,
        max(non_nonzero)  AS non_nonzero,
        max(has_bal_read) AS has_bal_read,
        max(has_non_read) AS has_non_read
    FROM (
        SELECT cityHash64(address) AS h, toUInt8(1) AS is_write,
               toUInt8(0) AS bal_nonzero, toUInt8(0) AS non_nonzero,
               toUInt8(0) AS has_bal_read, toUInt8(0) AS has_non_read
        FROM canonical_execution_balance_diffs
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address), toUInt8(1),
               toUInt8(0), toUInt8(0), toUInt8(0), toUInt8(0)
        FROM canonical_execution_nonce_diffs
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(contract_address), toUInt8(1),
               toUInt8(0), toUInt8(0), toUInt8(0), toUInt8(0)
        FROM canonical_execution_contracts
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address), toUInt8(0),
               toUInt8(balance != 0), toUInt8(0), toUInt8(1), toUInt8(0)
        FROM canonical_execution_balance_reads
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address), toUInt8(0),
               toUInt8(0), toUInt8(nonce != 0), toUInt8(0), toUInt8(1)
        FROM canonical_execution_nonce_reads
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address), toUInt8(0),
               toUInt8(0), toUInt8(0), toUInt8(0), toUInt8(0)
        FROM canonical_execution_address_appearances
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
          AND relationship IN ({_RELATIONSHIP_LIST})
    )
    GROUP BY h
)
SELECT
    countIf(any_write = 0) AS total_r,
    countIf(any_write = 0 AND bal_nonzero = 0 AND non_nonzero = 0
            AND (has_bal_read = 1 OR has_non_read = 1)) AS empty_accounts,
    countIf(any_write = 0 AND (bal_nonzero = 1 OR non_nonzero = 1)) AS nonempty_accounts,
    countIf(any_write = 0 AND has_bal_read = 0 AND has_non_read = 0) AS unknown_accounts
FROM per_acct
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


# ---------------------------------------------------------------------------
# Full-history event totals (additive countIf aggregates — no per-key GROUP BY).
#
# Each builder takes an inclusive block range `(bn_lo, bn_hi)` and returns a single
# aggregate row (the appearance builder returns one row per relationship). Counts are
# additive across disjoint ranges, so the collect driver can chunk arbitrarily and a
# timed-out chunk can be split and retried losslessly. Every builder also returns
# `n_total` so the analysis can verify the metric partition sums to the row count.
# ---------------------------------------------------------------------------


def slot_write_event_totals(bn_lo: int, bn_hi: int) -> str:
    """Slot write events by value transition (same definitions as `slot_typed_histogram`)."""
    return f"""
SELECT
    countIf(from_value =  '{_ZERO}' AND to_value != '{_ZERO}') AS n_create,
    countIf(from_value != '{_ZERO}' AND to_value != '{_ZERO}') AS n_update,
    countIf(from_value != '{_ZERO}' AND to_value =  '{_ZERO}') AS n_delete,
    count() AS n_total
FROM canonical_execution_storage_diffs
WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
"""


def slot_read_event_totals(bn_lo: int, bn_hi: int) -> str:
    """Slot read events by returned value (zero = empty-slot probe, nonzero = populated)."""
    return f"""
SELECT
    countIf(value =  '{_ZERO}') AS n_zero,
    countIf(value != '{_ZERO}') AS n_nonzero,
    count() AS n_total
FROM canonical_execution_storage_reads
WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
"""


def account_balance_write_totals(bn_lo: int, bn_hi: int) -> str:
    """Balance-diff events by transition: fund (0→x) / adjust (x→y) / drain (x→0).

    `from_value`/`to_value` are UInt256 — only compared against 0, never aggregated.
    """
    return f"""
SELECT
    countIf(from_value =  0 AND to_value != 0) AS n_fund,
    countIf(from_value != 0 AND to_value != 0) AS n_adjust,
    countIf(from_value != 0 AND to_value =  0) AS n_drain,
    count() AS n_total
FROM canonical_execution_balance_diffs
WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
"""


def account_nonce_write_totals(bn_lo: int, bn_hi: int) -> str:
    """Nonce-diff events: first use (from 0 — fresh EOA tx or contract init) vs subsequent."""
    return f"""
SELECT
    countIf(from_value = 0)  AS n_first_use,
    countIf(from_value != 0) AS n_subsequent,
    count() AS n_total
FROM canonical_execution_nonce_diffs
WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
"""


def account_contract_create_totals(bn_lo: int, bn_hi: int) -> str:
    """Contract-creation events (one row per deployment in `contracts`)."""
    return f"""
SELECT count() AS n_create, count() AS n_total
FROM canonical_execution_contracts
WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
"""


def account_balance_read_totals(bn_lo: int, bn_hi: int) -> str:
    """Balance-read events by observed value (zero vs nonzero)."""
    return f"""
SELECT
    countIf(balance =  0) AS n_zero,
    countIf(balance != 0) AS n_nonzero,
    count() AS n_total
FROM canonical_execution_balance_reads
WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
"""


def account_nonce_read_totals(bn_lo: int, bn_hi: int) -> str:
    """Nonce-read events by observed value (zero vs nonzero)."""
    return f"""
SELECT
    countIf(nonce =  0) AS n_zero,
    countIf(nonce != 0) AS n_nonzero,
    count() AS n_total
FROM canonical_execution_nonce_reads
WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
"""


def account_appearance_read_totals(bn_lo: int, bn_hi: int) -> str:
    """Appearance-read events per relationship (filtered to `READ_RELATIONSHIPS`).

    Long output: one row per relationship present in the range. The driver maps the
    relationship to the metric name.
    """
    return f"""
SELECT relationship AS metric, count() AS n
FROM canonical_execution_address_appearances
WHERE meta_network_name = '{NETWORK}'
  AND block_number BETWEEN {bn_lo} AND {bn_hi}
  AND relationship IN ({_RELATIONSHIP_LIST})
GROUP BY relationship
"""


def slot_sweep_summary(bn_now: int, days: int) -> str:
    """One-row scalar summary of the slot typed view for a (anchor, window) cell.

    Same per-key GROUP BY as `slot_typed_histogram`, but the classification that
    `analysis_v2.q1_warmth_slot_typed` / `_classify_mixed` do in pandas happens in SQL,
    so the sweep stores ~20 counts instead of a 100k-row histogram per anchor.
    """
    bn_lo, bn_hi = _window(bn_now, days)
    return f"""
WITH per_key AS (
    SELECT
        h,
        sum(is_w_create) AS c,
        sum(is_w_update) AS u,
        sum(is_w_delete) AS d,
        sum(is_r_zero)   AS rz,
        sum(is_r_nonzero) AS rn
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
            toUInt8(0), toUInt8(0), toUInt8(0),
            toUInt8(value =  '{_ZERO}') AS is_r_zero,
            toUInt8(value != '{_ZERO}') AS is_r_nonzero
        FROM canonical_execution_storage_reads
        WHERE meta_network_name = '{NETWORK}'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
    )
    GROUP BY h
)
SELECT
    countIf(c + u + d > 0)                              AS W,
    countIf(c + u + d = 0 AND rz + rn > 0)              AS R,
    count()                                             AS RW_union,
    countIf(c > 0 AND u = 0 AND d = 0)                  AS W_only_create,
    countIf(u > 0 AND c = 0 AND d = 0)                  AS W_only_update,
    countIf(d > 0 AND c = 0 AND u = 0)                  AS W_only_delete,
    countIf(toUInt8(c > 0) + toUInt8(u > 0) + toUInt8(d > 0) >= 2) AS W_mixed,
    countIf(c > 0 AND u > 0 AND d = 0)                  AS mixed_cu,
    countIf(c > 0 AND d > 0 AND u = 0 AND c = 1)        AS mixed_cd1,
    countIf(c > 0 AND d > 0 AND u = 0 AND c >= 2)       AS mixed_cdm,
    countIf(u > 0 AND d > 0 AND c = 0)                  AS mixed_ud,
    countIf(c > 0 AND u > 0 AND d > 0 AND c = 1)        AS mixed_cud1,
    countIf(c > 0 AND u > 0 AND d > 0 AND c >= 2)       AS mixed_cudm,
    countIf(c > 0)                                      AS W_any_create,
    countIf(u > 0)                                      AS W_any_update,
    countIf(d > 0)                                      AS W_any_delete,
    countIf(c + u + d = 0 AND rz > 0 AND rn = 0)        AS R_only_zero,
    countIf(c + u + d = 0 AND rn > 0 AND rz = 0)        AS R_only_nonzero,
    countIf(c + u + d = 0 AND rz > 0 AND rn > 0)        AS R_mixed
FROM per_key
"""


def account_sweep_summary(bn_now: int, days: int) -> str:
    """One-row W / R / R∪W account summary (same sources as `account_histogram`)."""
    bn_lo, bn_hi = _window(bn_now, days)
    return f"""
WITH per_key AS (
    SELECT h, sum(is_w) AS n_w, sum(is_r) AS n_r
    FROM (
        SELECT cityHash64(address) AS h, 1 AS is_w, 0 AS is_r
        FROM canonical_execution_balance_diffs
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address), 1, 0
        FROM canonical_execution_nonce_diffs
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(contract_address), 1, 0
        FROM canonical_execution_contracts
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address), 0, 1
        FROM canonical_execution_balance_reads
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address), 0, 1
        FROM canonical_execution_nonce_reads
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address), 0, 1
        FROM canonical_execution_address_appearances
        WHERE meta_network_name = '{NETWORK}' AND block_number BETWEEN {bn_lo} AND {bn_hi}
          AND relationship IN ({_RELATIONSHIP_LIST})
    )
    GROUP BY h
)
SELECT
    countIf(n_w > 0)              AS W,
    countIf(n_w = 0 AND n_r > 0)  AS R,
    count()                       AS RW_union
FROM per_key
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
