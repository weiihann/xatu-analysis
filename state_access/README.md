# state_access

Replication of Toni Wahrstätter's `state_access.ipynb`: how much of Ethereum state is
"hot" (modified within the last N days) vs "cold", and how that maps onto EIP-8188-style
state tiering (Active/Inactive pricing).

**See [REPORT.md](REPORT.md)** for the written findings (static snapshot + historical sweep).

Anchored at block **24,870,000** (mainnet). Three questions:

1. **Hot vs cold state** — what share of accounts/storage slots was modified within a
   rolling window of N days.
2. **Tiering tradeoff** — as the active-window `W` grows, the cold bucket shrinks (cost)
   while fewer of today's writes hit the cold tier (benefit).
3. **Gas concentration** — what share of today's SSTORE *update* gas hits the warm tier,
   and how concentrated that is relative to the warm tier's size.

## Run

```bash
# 1. Fill ETHPANDAOPS_CLICKHOUSE_PASSWORD in ../.env (totals come from that cluster).
uv run python -m state_access.collect     # queries ClickHouse → data/*.parquet + totals.json
uv run python -m state_access.analysis     # data/ → 4 PNG charts + printed tables
```

## Historical sweep

`collect_history.py` / `analysis_history.py` extend the static analysis into a post-Merge
weekly time series at fixed W=30 (block 15.54M → 24.87M, ~186 anchors), to test whether the
findings are stable over time. It reuses the same query builders, one anchor at a time,
checkpointing `data/history_w30.parquet` after each anchor (resumable).

```bash
uv run python -m state_access.collect_history   # ~1–1.5 hr, resumable
uv run python -m state_access.analysis_history   # 3 trend charts
```

Outputs: `data/history_w30.parquet` and `history_{state_cold,writes_cold,gas_concentration}.png`.

**Data-quality note:** the personal node originally had a storage-diff ingestion gap
(blocks ~23,270,400 → 23,659,200, ~54 days, late-Aug–mid-Oct 2025, plus a small dip at
~23,097,600–23,112,000). It has since been backfilled from the ethPandaOps cluster via
`scripts/backfill_storage_diffs.py`, restoring the region to ~99.9% coverage. `analysis_history.py`
keeps a safety filter that drops anchors below 70% of the median storage-slot count; with the
gap filled it now excludes 0 of 186.

## How it differs from the original

- **`uniq` (HyperLogLog, ~1% error) for every window.** The personal node has no Cloudflare
  100s HTTP cap, so the original's `uniqExact` / `uniq` / HLL-chunked-merge split is
  unnecessary — `uniq` is fast enough even for the 180-day, three-table union.
- **`pct_state_warm` is measured directly** at each window, not log-linearly interpolated.
  The original interpolated only because exact counts were expensive to obtain per window.
- **Totals come from `execution_state_size`** on the ethPandaOps cluster (the personal node's
  copy is empty), recorded with their snapshot block in `data/totals.json`.

## Method notes (carried from the original)

- "Modified" = balance, nonce, or any storage slot changed at least once in the window.
- Writes-to-warm queries hash the key with `cityHash64` and use `GLOBAL IN` so the past-W
  membership set is built once and broadcast (collision risk is negligible at this scale).
- Update-only counts exclude fresh-slot creations (`from_value = 0`), which are always
  Inactive-priced. Each SSTORE_RESET costs ~5,000 gas, so the count-weighted warm fraction
  equals the gas-weighted fraction.
- No `FINAL` on the diff tables (matches the original; minor ReplacingMergeTree dup risk on
  raw write counts).

## Outputs (`data/`)

| file | contents |
|------|----------|
| `hot_cold_state.parquet` | per-window unique accounts / storage slots |
| `tradeoff.parquet` | per-window cold-state % and writes-to-cold % |
| `gas_concentration.parquet` | per-window warm-state %, warm update-gas %, concentration |
| `totals.json` | live-state denominators + their snapshot block |
| `*.png` | the four charts |
