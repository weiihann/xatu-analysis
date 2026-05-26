# state_delta

How Ethereum **live-state size** grows over time, measured from the per-block snapshots in
`execution_state_size` (counts and byte sizes for accounts, storage slots, and contract code).

**See [REPORT.md](REPORT.md)** for the written findings.

This is the *net* state delta — the change in live-state size per period — not a gross
write/delete split (a cumulative live-state count cannot separate the two). Two views:

1. **Net Δ per period** — daily / weekly / monthly / yearly net change in each metric.
2. **Per-block Δ distribution** — average / median / percentiles of the per-block net change,
   overall and per calendar year.

Post-Merge only (block 15,537,394 → latest), where 7,200 blocks ≈ 1 day.

## Run

```bash
# 1. Backfill execution_state_size into the primary node (queries then run locally).
uv run python -m scripts.backfill_state_size      # ~10M rows, resumable

# 2. Collect the two tables, then render the charts.
uv run python -m state_delta.collect              # primary -> data/*.parquet
uv run python -m state_delta.analysis             # data/ -> 9 PNG charts + printed tables
```

## Method notes

- **Client stitching.** No single client reports `execution_state_size` across the whole
  post-Merge range, so the series is stitched from two: `manual-backfill` below
  `SEAM_BLOCK` (23,000,000) and a `tysm` live node at/above it. They agree in the overlap
  (accounts exact, storages ~2e-6, bytes match) except for a ~1.7% step in `contract_codes`,
  left as a documented one-day seam artifact.
- **Daily levels.** `day_idx = intDiv(block - MERGE_BLOCK, 7200)`; per day the end-of-day
  (`argMax(metric, block_number)`) level is kept. Weekly/monthly/yearly are resampled from it.
- **Per-block deltas.** `lagInFrame` over consecutive blocks of the stitched series. Counts
  and bytes are cast to **Int64** before differencing — live-state totals *decrease* when
  storage slots are cleared, and unsigned subtraction would underflow.
- **Cross-check.** At block 24,870,000 the levels equal the `state_access` static snapshot
  (379,632,901 accounts / 1,552,604,459 storage slots).

## Outputs (`data/`)

| file | contents |
|------|----------|
| `state_delta_daily.parquet` | per-day end-of-day levels + daily net deltas (`d_*`) |
| `state_delta_perblock_stats.parquet` | per-block delta distribution, tidy by (scope, metric) |
| `delta_counts_{daily,weekly,monthly,yearly}.png` | net Δ entry counts per period |
| `delta_bytes_{daily,weekly,monthly,yearly}.png` | net Δ byte sizes per period |
| `perblock_by_year.png` | per-block mean vs median Δ by year (accounts, storage slots) |
