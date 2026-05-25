"""SQL builders for the state_access analysis.

Each builder returns a ClickHouse SQL string parameterised by the anchor block
``bn_now`` and a window length ``days``. All counts use ``uniq`` (HyperLogLog,
~1% error) rather than ``uniqExact`` — the personal node has no Cloudflare 100s
cap, so exact dedup is unnecessary and the approximate aggregate is far faster.

The membership tests in steps 2-4 hash the key with ``cityHash64`` and use
``GLOBAL IN`` so the "past window" set is built once and broadcast across the
distributed cluster, instead of materialising a multi-million-row string set.
"""

from __future__ import annotations

from state_access.config import BLOCKS_PER_DAY, NETWORK

# Full 32-byte zero — the stored representation of an empty storage slot.
_ZERO = "0x" + "0" * 64


def state_touched(bn_now: int, days: int) -> str:
    """Unique accounts and storage slots modified in the last ``days`` days."""
    lo = bn_now - days * BLOCKS_PER_DAY
    return f"""
WITH {bn_now} AS bn_now, {lo} AS bn_lo
SELECT
    (
        SELECT uniq(address) FROM (
            SELECT address FROM canonical_execution_balance_diffs
              WHERE meta_network_name='{NETWORK}' AND block_number BETWEEN bn_lo AND bn_now
            UNION ALL
            SELECT address FROM canonical_execution_nonce_diffs
              WHERE meta_network_name='{NETWORK}' AND block_number BETWEEN bn_lo AND bn_now
            UNION ALL
            SELECT address FROM canonical_execution_storage_diffs
              WHERE meta_network_name='{NETWORK}' AND block_number BETWEEN bn_lo AND bn_now
        )
    ) AS unique_accounts,
    (
        SELECT uniq((address, slot)) FROM canonical_execution_storage_diffs
          WHERE meta_network_name='{NETWORK}' AND block_number BETWEEN bn_lo AND bn_now
    ) AS unique_storage_slots
"""


def _writes_warm(bn_now: int, days: int, table: str, key: str, today_filter: str = "") -> str:
    """Shared shape: of today's writes, how many hit a key seen in the past W days.

    ``today_filter`` constrains only *today's* writes (the numerator/denominator
    population). The past-W warm set is never filtered — a slot counts as warm if
    it was touched at all in the window, whether created or updated.
    """
    today_start = bn_now - BLOCKS_PER_DAY
    past_lo = today_start - days * BLOCKS_PER_DAY
    return f"""
WITH {bn_now} AS bn_now, {today_start} AS bn_today_start
SELECT
    count() AS today,
    countIf({key} GLOBAL IN (
        SELECT {key}
        FROM {table}
        WHERE meta_network_name='{NETWORK}'
          AND block_number BETWEEN {past_lo} AND bn_today_start - 1
    )) AS warm,
    round(100 * warm / today, 4) AS pct_warm
FROM {table}
WHERE meta_network_name='{NETWORK}'
  AND block_number BETWEEN bn_today_start AND bn_now{today_filter}
"""


def account_writes_warm(bn_now: int, days: int) -> str:
    """Account (balance) writes today that hit an address seen in the past W days."""
    return _writes_warm(bn_now, days, "canonical_execution_balance_diffs", "cityHash64(address)")


def storage_writes_warm(bn_now: int, days: int) -> str:
    """Storage-slot writes today that hit an (address, slot) seen in the past W days."""
    return _writes_warm(
        bn_now, days, "canonical_execution_storage_diffs", "cityHash64(address, slot)"
    )


def update_writes_warm(bn_now: int, days: int) -> str:
    """Storage *updates* only (from_value != 0) — feeds the gas-concentration view.

    Every SSTORE_RESET costs ~5,000 gas, so the count-weighted warm fraction equals
    the gas-weighted fraction. Fresh-slot creations (0 -> nonzero) are excluded
    because they are always priced at the Inactive tier regardless of the window.
    """
    return _writes_warm(
        bn_now,
        days,
        "canonical_execution_storage_diffs",
        "cityHash64(address, slot)",
        today_filter=f"\n          AND from_value != '{_ZERO}'",
    )


def totals(bn_now: int) -> str:
    """Latest live-state totals at or before the anchor (ethpandaops profile)."""
    return f"""
SELECT block_number AS snapshot_block, accounts, storages
FROM execution_state_size
WHERE meta_network_name='{NETWORK}' AND block_number <= {bn_now}
ORDER BY block_number DESC
LIMIT 1
"""
