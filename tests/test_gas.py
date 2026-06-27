"""Tests for the state_access gas analysis helpers."""

from __future__ import annotations

import pandas as pd

from state_access.v1.collect_gas import daily_gas_query, trailing_window_sum


def test_daily_gas_query_shape() -> None:
    sql = daily_gas_query()
    # Deduped per block before aggregating (ReplacingMergeTree).
    assert "argMax(gas_used, updated_date_time)" in sql
    assert "argMax(gas_limit, updated_date_time)" in sql
    assert "GROUP BY block_number" in sql
    # Then summed/averaged per day.
    assert "sum(gu) AS gas_used" in sql
    assert "avg(gl) AS gas_limit" in sql
    assert "intDiv(block_number - 15537394, 7200) AS day_idx" in sql


def test_trailing_window_sum_over_full_window() -> None:
    # daily series 0,1,2,...,9 -> cumulative sums.
    cum = pd.Series(range(10)).cumsum()  # cum[d] = d*(d+1)/2
    # Sum over the 3 days ending at day 5 = days 3+4+5 = 12 = cum[5]-cum[2].
    assert trailing_window_sum(cum, 5, 3) == cum[5] - cum[2]
    assert trailing_window_sum(cum, 5, 3) == 12


def test_trailing_window_sum_truncates_before_series_start() -> None:
    # When day - w < 0 the window has no lower bound in the data, so it sums from the start.
    cum = pd.Series([10, 30, 60]).cumsum()  # cum = [10, 40, 100]
    # day 1, w 5 -> day-w = -4 (absent) -> defaults to 0, returns cum[1] = 40 (truncated).
    assert trailing_window_sum(cum, 1, 5) == 40
