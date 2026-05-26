"""Tests for the state_delta query builders and pure transform helpers."""

from __future__ import annotations

import pandas as pd

from state_delta import queries
from state_delta.collect import stitch_daily, tidy_stats
from state_delta.config import (
    ANCHOR_BLOCK,
    BYTE_COLS,
    COUNT_COLS,
    METRIC_COLS,
    SEAM_BLOCK,
    START_BLOCK,
)


def test_config_ranges_are_ordered() -> None:
    assert START_BLOCK < SEAM_BLOCK < ANCHOR_BLOCK
    assert COUNT_COLS == ["accounts", "storages", "contract_codes"]
    assert BYTE_COLS == ["account_bytes", "storage_bytes", "contract_code_bytes"]
    assert METRIC_COLS == COUNT_COLS + BYTE_COLS


def test_daily_levels_query_shape() -> None:
    sql = queries.daily_levels("client-x", 100, 200)
    assert "meta_client_name='client-x'" in sql
    assert "block_number BETWEEN 100 AND 200" in sql
    assert "GROUP BY day_idx" in sql
    for c in METRIC_COLS:
        assert f"argMax({c}, block_number) AS {c}" in sql


def test_perblock_stats_query_shape() -> None:
    sql = queries.perblock_delta_stats(25_000_000)
    assert "WITH ROLLUP" in sql
    assert "lagInFrame" in sql
    assert "WHERE prev_block != 0" in sql
    # Counts/bytes can decrease, so deltas must be computed in signed Int64.
    for c in METRIC_COLS:
        assert f"toInt64({c}) - lagInFrame(toInt64({c}))" in sql
        assert f"quantiles(0.1, 0.25, 0.5, 0.75, 0.9)(d_{c})" in sql
    # Both clients are stitched at the seam.
    assert f"block_number < {SEAM_BLOCK}" in sql
    assert f"block_number >= {SEAM_BLOCK}" in sql


def _levels_row(day_idx: int, block: int, base: int) -> dict[str, object]:
    return {"day_idx": day_idx, "block": block,
            **{c: base + i for i, c in enumerate(METRIC_COLS)}}


def test_stitch_daily_dedups_seam_day_keeping_higher_block() -> None:
    # Day 5 is reported by both clients; the higher-block (recent) row should win.
    early = pd.DataFrame([_levels_row(4, 22_990_000, 1000), _levels_row(5, 22_999_999, 2000)])
    recent = pd.DataFrame([_levels_row(5, 23_003_000, 2222), _levels_row(6, 23_010_000, 3000)])

    out = stitch_daily(early, recent)

    assert list(out["day_idx"]) == [4, 5, 6]
    seam = out[out["day_idx"] == 5].iloc[0]
    assert seam["block"] == 23_003_000
    assert seam["accounts"] == 2222  # recent client's value, not early's 2000
    # First row's delta is undefined; later deltas are day-over-day differences.
    assert pd.isna(out["d_accounts"].iloc[0])
    assert out["d_accounts"].iloc[2] == out["accounts"].iloc[2] - out["accounts"].iloc[1]


def test_stitch_daily_handles_decreasing_metric() -> None:
    early = pd.DataFrame([_levels_row(0, 100, 500), _levels_row(1, 7300, 500)])
    early.loc[1, "storages"] = early.loc[0, "storages"] - 10  # a net deletion day
    out = stitch_daily(early, pd.DataFrame(columns=early.columns))
    assert out["d_storages"].iloc[1] == -10


def test_tidy_stats_expands_quantiles_and_labels_rollup() -> None:
    raw_row: dict[str, object] = {"year": 0, "n_blocks": 9}
    for c in METRIC_COLS:
        raw_row[f"{c}_avg"] = 1.5
        raw_row[f"{c}_q"] = [1.0, 2.0, 3.0, 4.0, 5.0]
        raw_row[f"{c}_max"] = 99
        raw_row[f"{c}_min"] = -7
    tidy = tidy_stats(pd.DataFrame([raw_row, {**raw_row, "year": 2024}]))

    assert set(tidy["scope"]) == {"overall", "2024"}
    acct = tidy[(tidy["scope"] == "overall") & (tidy["metric"] == "accounts")].iloc[0]
    assert acct["median"] == 3.0 and acct["p10"] == 1.0 and acct["p90"] == 5.0
    assert acct["max"] == 99 and acct["min"] == -7
    assert len(tidy) == 2 * len(METRIC_COLS)
