"""SQL builders for the state_delta analysis (run on the primary node after backfill).

Two views over execution_state_size, the per-block snapshot of live-state size:

- ``daily_levels`` — end-of-day cumulative level per metric, one client at a time. The
  collector calls it once per client range and stitches the results at ``SEAM_BLOCK``.
- ``perblock_delta_stats`` — the distribution (avg/median/percentiles) of the *per-block*
  net change in each metric, over the stitched single series, overall and per calendar year.

Counts and byte sizes are cast to Int64 before differencing because live-state totals can
*decrease* (cleared storage slots, self-destructs); UInt64 subtraction would underflow.
"""

from __future__ import annotations

from state_delta.config import (
    BLOCKS_PER_DAY,
    EARLY_CLIENT,
    MERGE_BLOCK,
    MERGE_TS,
    METRIC_COLS,
    NETWORK,
    RECENT_CLIENT,
    SEAM_BLOCK,
    SECONDS_PER_BLOCK,
    START_BLOCK,
)

# Approximate quantiles reported for the per-block delta distribution.
QUANTILES = [0.1, 0.25, 0.5, 0.75, 0.9]


def _argmax_by(order: str) -> str:
    """`argMax(col, order) AS col` for every metric — picks one value per group."""
    return ",\n    ".join(f"argMax({c}, {order}) AS {c}" for c in METRIC_COLS)


def daily_levels(client: str, lo: int, hi: int) -> str:
    """End-of-day cumulative level per metric for one client over [lo, hi].

    ``day_idx`` is anchored at the Merge block so it is continuous across the seam when the
    two clients' results are concatenated. ``argMax(metric, block_number)`` takes the last
    block seen within each day.
    """
    return f"""
SELECT
    intDiv(block_number - {MERGE_BLOCK}, {BLOCKS_PER_DAY}) AS day_idx,
    max(block_number) AS block,
    {_argmax_by("block_number")}
FROM execution_state_size
WHERE meta_network_name='{NETWORK}'
  AND meta_client_name='{client}'
  AND block_number BETWEEN {lo} AND {hi}
GROUP BY day_idx
ORDER BY day_idx
"""


def _stitched_per_block(hi: int) -> str:
    """One deduplicated row per block over the stitched two-client series, [START_BLOCK, hi]."""
    return f"""
    SELECT
        block_number,
        {_argmax_by("updated_date_time")}
    FROM execution_state_size
    WHERE meta_network_name='{NETWORK}'
      AND block_number BETWEEN {START_BLOCK} AND {hi}
      AND ( (meta_client_name='{EARLY_CLIENT}' AND block_number < {SEAM_BLOCK})
         OR (meta_client_name='{RECENT_CLIENT}' AND block_number >= {SEAM_BLOCK}) )
    GROUP BY block_number
"""


def _delta_exprs() -> str:
    """Per-block delta of each metric (Int64 to allow decreases) via the ordered window."""
    return ",\n        ".join(
        f"toInt64({c}) - lagInFrame(toInt64({c})) OVER w AS d_{c}" for c in METRIC_COLS
    )


def _stat_exprs() -> str:
    """avg / quantiles / max / min of each per-block delta."""
    q = ", ".join(str(x) for x in QUANTILES)
    parts: list[str] = []
    for c in METRIC_COLS:
        parts.append(f"avg(d_{c}) AS {c}_avg")
        parts.append(f"quantiles({q})(d_{c}) AS {c}_q")
        parts.append(f"max(d_{c}) AS {c}_max")
        parts.append(f"min(d_{c}) AS {c}_min")
    return ",\n    ".join(parts)


def perblock_delta_stats(hi: int) -> str:
    """Distribution of per-block net deltas, grouped by calendar year with an overall rollup.

    ``year = 0`` is the WITH ROLLUP overall row. The very first block has no predecessor
    (``prev_block = 0`` from ``lagInFrame``'s default) and is dropped.
    """
    year_expr = (f"toYear(toDateTime({MERGE_TS} + (block_number - {MERGE_BLOCK}) "
                 f"* {SECONDS_PER_BLOCK}))")
    return f"""
WITH per_block AS ({_stitched_per_block(hi)}),
deltas AS (
    SELECT
        {year_expr} AS year,
        lagInFrame(block_number) OVER w AS prev_block,
        {_delta_exprs()}
    FROM per_block
    WINDOW w AS (ORDER BY block_number)
)
SELECT
    year,
    count() AS n_blocks,
    {_stat_exprs()}
FROM deltas
WHERE prev_block != 0
GROUP BY year WITH ROLLUP
ORDER BY year
"""
