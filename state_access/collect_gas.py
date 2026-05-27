"""Collect daily gas (used + limit) for the state_access gas analysis.

Block gas bounds how much work — and thus how much state access — can happen. This pulls
per-day gas totals from `canonical_execution_block` (ethPandaOps) so the historical sweep's
warm set / cold share can be related to gas capacity and usage over time.

One aggregation query (no backfill). Run with:  uv run python -m state_access.collect_gas
"""

from __future__ import annotations

import pandas as pd

from lib.clickhouse import run_query
from state_access.config import DATA_DIR
from state_access.history_config import MERGE_BLOCK, block_to_date

NETWORK = "mainnet"


def daily_gas_query() -> str:
    """Per-day gas: summed gas_used and average gas_limit, post-Merge.

    `canonical_execution_block` is ReplacingMergeTree (a few duplicate rows per block), so
    rows are deduped per block via `argMax(updated_date_time)` before aggregating by day.
    `day_idx = intDiv(block - MERGE_BLOCK, 7200)` matches the sweep's day convention.
    """
    return f"""
WITH dedup AS (
    SELECT block_number,
           argMax(gas_used, updated_date_time) AS gu,
           argMax(gas_limit, updated_date_time) AS gl
    FROM canonical_execution_block
    WHERE meta_network_name='{NETWORK}' AND block_number >= {MERGE_BLOCK}
    GROUP BY block_number
)
SELECT
    intDiv(block_number - {MERGE_BLOCK}, 7200) AS day_idx,
    max(block_number) AS block,
    sum(gu) AS gas_used,
    avg(gl) AS gas_limit
FROM dedup
GROUP BY day_idx
ORDER BY day_idx
"""


def trailing_window_sum(cum: pd.Series, day: int, w: int) -> float:
    """Sum of a daily series over the `w` days ending at `day`, from its cumulative series.

    `cum` must be indexed by a gap-free day index. Callers must ensure `day - w >= 0` (a full
    window of data exists); otherwise the result is silently truncated at the series start.
    """
    return float(cum.get(day, cum.iloc[-1]) - cum.get(day - w, 0))


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df = run_query(daily_gas_query(), profile="ethpandaops")
    df["gas_used"] = df["gas_used"].astype("int64")
    df["gas_limit"] = df["gas_limit"].astype("float64")
    df["date"] = df["block"].map(block_to_date)
    df = df[["day_idx", "block", "date", "gas_used", "gas_limit"]]
    df.to_parquet(DATA_DIR / "gas_daily.parquet", index=False)

    print(f"Wrote {len(df)} daily gas rows to {DATA_DIR / 'gas_daily.parquet'}")
    print(f"  {df['date'].min():%Y-%m-%d} -> {df['date'].max():%Y-%m-%d}")
    print(f"  gas_limit {df['gas_limit'].min() / 1e6:.0f}M -> {df['gas_limit'].max() / 1e6:.0f}M; "
          f"mean daily gas_used {df['gas_used'].mean() / 1e12:.2f}e12")


if __name__ == "__main__":
    main()
