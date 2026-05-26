"""Collect the state_delta tables from the primary node (run the backfill first).

Outputs (under ``state_delta/data/``):
  - state_delta_daily.parquet         : end-of-day cumulative levels + daily net deltas
  - state_delta_perblock_stats.parquet: per-block delta distribution, overall and per year

The daily series is stitched from two clients at SEAM_BLOCK; on the one overlapping day the
more complete (higher block) end-of-day row is kept.

Run with:  uv run python -m state_delta.collect
"""

from __future__ import annotations

import pandas as pd

from lib.clickhouse import run_query
from state_delta import queries
from state_delta.config import (
    ANCHOR_ACCOUNTS,
    ANCHOR_BLOCK,
    ANCHOR_STORAGES,
    COUNT_COLS,
    DATA_DIR,
    EARLY_CLIENT,
    METRIC_COLS,
    NETWORK,
    RECENT_CLIENT,
    SEAM_BLOCK,
    START_BLOCK,
    block_to_date,
)

# A single-block daily delta above this is implausible and would signal a broken stitch.
_MAX_PLAUSIBLE_DAILY_DELTA = 50_000_000


def max_block() -> int:
    """Highest block the recent client covers on the primary node."""
    sql = (f"SELECT max(block_number) AS b FROM execution_state_size "
           f"WHERE meta_network_name='{NETWORK}' AND meta_client_name='{RECENT_CLIENT}'")
    return int(run_query(sql, profile="primary").iloc[0]["b"])


def stitch_daily(early: pd.DataFrame, recent: pd.DataFrame) -> pd.DataFrame:
    """Combine the two clients' end-of-day levels into one continuous daily series.

    The seam day appears in both clients; the higher-block (fuller) end-of-day row wins.
    Day-over-day net deltas are added as ``d_<metric>`` columns.
    """
    daily = pd.concat([early, recent], ignore_index=True).sort_values(["day_idx", "block"])
    daily = daily.drop_duplicates("day_idx", keep="last").reset_index(drop=True)
    daily["date"] = daily["block"].map(block_to_date)
    # Levels arrive as uint64; cast to signed so day-over-day decreases don't underflow.
    daily[METRIC_COLS] = daily[METRIC_COLS].astype("int64")
    for c in METRIC_COLS:
        daily[f"d_{c}"] = daily[c].diff()
    cols = ["day_idx", "block", "date", *METRIC_COLS, *(f"d_{c}" for c in METRIC_COLS)]
    return daily[cols]


def fetch_daily_levels(hi: int) -> pd.DataFrame:
    """Stitched end-of-day levels: EARLY_CLIENT below the seam, RECENT_CLIENT at/above."""
    early = run_query(queries.daily_levels(EARLY_CLIENT, START_BLOCK, SEAM_BLOCK - 1),
                      profile="primary")
    recent = run_query(queries.daily_levels(RECENT_CLIENT, SEAM_BLOCK, hi), profile="primary")
    return stitch_daily(early, recent)


def tidy_stats(raw: pd.DataFrame) -> pd.DataFrame:
    """Reshape the wide ROLLUP stats query into one tidy row per (scope, metric)."""
    rows: list[dict[str, object]] = []
    for _, r in raw.iterrows():
        scope = "overall" if int(r["year"]) == 0 else str(int(r["year"]))
        for c in METRIC_COLS:
            p10, p25, p50, p75, p90 = r[f"{c}_q"]
            rows.append({
                "scope": scope, "metric": c, "n_blocks": int(r["n_blocks"]),
                "mean": float(r[f"{c}_avg"]), "p10": float(p10), "p25": float(p25),
                "median": float(p50), "p75": float(p75), "p90": float(p90),
                "max": int(r[f"{c}_max"]), "min": int(r[f"{c}_min"]),
            })
    return pd.DataFrame(rows)


def fetch_perblock_stats(hi: int) -> pd.DataFrame:
    """Tidy per-block delta distribution: one row per (scope, metric)."""
    return tidy_stats(run_query(queries.perblock_delta_stats(hi), profile="primary"))


def anchor_levels() -> tuple[int, int]:
    """Accounts and storages at the cross-check anchor block (recent client)."""
    sql = (f"SELECT argMax(accounts, updated_date_time) AS accounts, "
           f"argMax(storages, updated_date_time) AS storages FROM execution_state_size "
           f"WHERE meta_network_name='{NETWORK}' AND meta_client_name='{RECENT_CLIENT}' "
           f"AND block_number={ANCHOR_BLOCK}")
    r = run_query(sql, profile="primary").iloc[0]
    return int(r["accounts"]), int(r["storages"])


def _check(daily: pd.DataFrame) -> None:
    """Cross-check against state_access and guard against a broken client stitch."""
    accounts, storages = anchor_levels()
    assert accounts == ANCHOR_ACCOUNTS, f"anchor accounts {accounts:,} != {ANCHOR_ACCOUNTS:,}"
    assert storages == ANCHOR_STORAGES, f"anchor storages {storages:,} != {ANCHOR_STORAGES:,}"

    d_acct = daily["d_accounts"].dropna()
    assert (d_acct >= 0).mean() > 0.95, "accounts should grow on almost every day"
    for c in COUNT_COLS:
        worst = daily[f"d_{c}"].abs().max()
        assert worst < _MAX_PLAUSIBLE_DAILY_DELTA, f"implausible daily {c} delta {worst:,.0f}"
    assert daily[METRIC_COLS].notna().all().all(), "missing level values"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    hi = max_block()
    print(f"Stitching daily levels {START_BLOCK:,}..{hi:,} "
          f"(seam {SEAM_BLOCK:,}: {EARLY_CLIENT.split('/')[-1]} | {RECENT_CLIENT.split('/')[-1]})")

    daily = fetch_daily_levels(hi)
    print(f"  {len(daily)} daily rows, "
          f"{daily['date'].min():%Y-%m-%d} -> {daily['date'].max():%Y-%m-%d}")

    print("Computing per-block delta distribution (overall + per year)...")
    stats = fetch_perblock_stats(hi)

    _check(daily)

    daily.to_parquet(DATA_DIR / "state_delta_daily.parquet", index=False)
    stats.to_parquet(DATA_DIR / "state_delta_perblock_stats.parquet", index=False)
    print(f"\nWrote 2 parquets to {DATA_DIR}")

    overall = stats[stats["scope"] == "overall"].set_index("metric")
    print("\nPer-block net delta (overall):")
    print(f"{'metric':>20}{'mean':>14}{'median':>12}{'p90':>14}{'max':>16}")
    for c in METRIC_COLS:
        r = overall.loc[c]
        print(f"{c:>20}{r['mean']:>14,.1f}{r['median']:>12,.0f}{r['p90']:>14,.0f}{r['max']:>16,.0f}")


if __name__ == "__main__":
    main()
