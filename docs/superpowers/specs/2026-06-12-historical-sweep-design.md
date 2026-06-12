# Historical sweep of the v2 windowed analyses — design

2026-06-12 · branch `feat/gas-analysis` · extends `state_access/REPORT_v2.md`

## Goal

Every windowed result in REPORT_v2 is a single snapshot anchored at block 24,870,000.
Replay the windowed sections at **weekly anchors across the post-merge range** so each
becomes a time series: is the R/W ratio stable, did Dencun move the zero-probe share, is
§7's 94% warm-update coverage a constant or a trend?

Triage — what sweeps and what doesn't:

| REPORT_v2 section | swept? |
|---|---|
| §3 warmth (W / R / R∪W set sizes) | yes |
| §4 write structure (typed partition, touch rates, W_mixed 6-way) | yes |
| §5 read structure (zero/nonzero split) | yes |
| §6 concentration (top-1% / top-10%) | yes |
| §7 warm-update coverage | yes |
| §8 first-op (slots + accounts) + R-only empty split | yes (same grid) |
| full-history event totals | no — era-complete by construction |
| §2 method / denominators | no — static |

## Sweep grid

- **Windows:** T ∈ {30, 90, 180, 365} days (the v1 REPORT.md grid, for comparability).
- **Cadence:** weekly — anchors step 50,400 blocks (7 × 7,200), generated descending from
  `ANCHOR_BLOCK_V2 = 24,870,000` so the newest anchor coincides with the snapshot, then
  collected ascending.
- **Floor per window:** `MERGE_BLOCK + T·7200` — the whole lookback stays post-merge,
  where local read coverage exists and 7,200 blocks/day holds.
- Anchor counts ≈ 181 / 173 / 160 / 134 for T = 30 / 90 / 180 / 365 (≈ 648 total
  anchor-window cells).

**Cost (benchmarked at anchor 20,000,000):** the full per-anchor suite is 88s at T=30d;
cost scales ≈ linearly with T (worst single query: `account_r_empty_split` at T=365d,
141s). Estimated totals: ~4.5h (T=30) / ~13h (90) / ~25h (180) / ~40h (365) ≈ **~80h
cluster time, ±2×**. All resumable; run sequentially cheapest-first so full 30d coverage
lands on day one. The user accepted this budget explicitly.

## Architecture

**Chosen: in-SQL scalar summaries.** Each (anchor, T) query reuses the exact per-key
CTEs of the proven v2 builders but classifies inside SQL and returns one row of counts.
Identical memory profile to queries the node already survives at T=365; outputs are one
wide row per anchor, so committed parquets stay KB-sized.

Rejected: persisting full per-anchor histograms (~GBs across 648 anchor-windows);
a single mega-query per anchor (fragile on the memory-limited node).

§6 concentration is the one non-scalar: the driver fetches the existing
`slot_histogram` / `account_histogram` result for the (anchor, T) and immediately
reduces it in Python to top-1% / top-10% shares per access set (exact — same logic
as `analysis_v2.q3_concentration`, vectorized). The histogram itself is not persisted.

## Components

### 1. `config_v2.py` additions

- `SWEEP_WINDOWS = [30, 90, 180, 365]`, `SWEEP_STEP = 50_400`.
- `anchors_v2(T) -> list[int]` — descending generation from `ANCHOR_BLOCK_V2`, floor
  `MERGE_BLOCK + T*7200`, returned ascending.
- Reuse `history_config.block_to_date` and `history_config.FORKS` (import, don't copy).

### 2. `queries_v2.py` — two new builders

- `slot_sweep_summary(bn_now, days)` — inner per-key CTE identical to
  `slot_typed_histogram` (five typed counters per `cityHash64` key); outer SELECT returns
  ONE row via `countIf` / `sumIf` over per-key aggregates:
  `W, R, RW_union, W_only_create, W_only_update, W_only_delete, W_mixed,`
  `mixed_cu, mixed_cd1, mixed_cdm, mixed_ud, mixed_cud1, mixed_cudm,`
  `W_any_create, W_any_update, W_any_delete, R_only_zero, R_only_nonzero, R_mixed`.
  Classification rules copied from `analysis_v2._classify_mixed` /
  `q1_warmth_slot_typed` (single create ⇒ 1-cycle, etc.).
- `account_sweep_summary(bn_now, days)` — inner CTE identical to `account_histogram`;
  outer returns `W, R, RW_union`.
- §6 reuses `slot_histogram` / `account_histogram`; §7 reuses `slot_update_coverage`;
  §8 reuses `slot_first_op`, `account_first_op`, `account_r_empty_split` — all unchanged.

### 3. `collect_v2_sweep.py` — the driver (v1 `collect_history.py` pattern)

- Per-window parquet `data/v2/sweep_w{T}.parquet`; one wide row per anchor containing:
  anchor_block, date, both summary rows, 12 concentration scalars
  (top{1,10} × {W, R, RW} × {slot, account}), §7 row, §8 rows (slot_first_op,
  account_first_op, account_r_empty_split), and per-anchor denominators.
- Denominators: `SELECT accounts, storages FROM execution_state_size WHERE block_number
  <= anchor ORDER BY block_number DESC LIMIT 1` on **`primary`** (the local copy covers
  the whole post-merge range; the ethpandaops copy is now TTL'd to a recent band).
  Fail fast if the nearest snapshot is > 50,400 blocks older than the anchor.
- Checkpoint after every anchor; resume by `anchor_block`; retry `OperationalError`
  with backoff (6 attempts, 20s·attempt) for node/Tailscale blips.
- Settings: the HEAVY spill dict from `_sweep_resume_all.py` for all queries.
- CLI: `uv run python -m state_access.collect_v2_sweep [T ...]` (default: all four,
  ascending so the cheap full series lands first).

### 4. `analysis_v2_sweep.py` — derivation + charts (new module; analysis_v2 stays as-is)

- Loads the four sweep parquets; emits time-series PNGs (x = anchor date, fork
  annotation lines from `FORKS`), each overlaying the four windows where defined:
  - `sweep_warmth_{slot,account,combined}.png` — W / R / R∪W as % of live state
  - `sweep_write_structure.png` — create-only share of |W|, any-update share of |W|
  - `sweep_mixed_decomp.png` — W_mixed combo shares (T=365 only; stacked)
  - `sweep_read_structure.png` — zero-share of |R|
  - `sweep_concentration.png` — top-1% share, W vs R (slots + accounts)
  - `sweep_update_coverage.png` — §7 pct_warm
  - `sweep_first_op.png` — first=nonzero-read share (slots + accounts)
  - `sweep_empty_split.png` — non-empty share of R-only accounts
- Derived parquet `sweep_summary.parquet` (all windows, long format) for the report.
- **Verification:**
  - the newest anchor (24,870,000) must reproduce the committed snapshot numbers
    exactly (q1_warmth, q3_concentration, slot_update_coverage, first-op, empty-split);
  - per row: `RW = W + R`, partition sums (`W = only_c + only_u + only_d + mixed`,
    `mixed = Σ combos`, `R = zero + nonzero + mixed`), §8 shares sum to 100%;
  - denominator staleness ≤ 1 week of blocks.

### 5. Report — new "Part III — Historical sweep (post-merge)" in REPORT_v2.md

One subsection per swept family: chart + 2–3 readings, answering at minimum: stability
of the R/W ratio; Dencun/Pectra effects on zero-probe share and warmth; whether §7's
94% (T=30d) is flat; whether §8's policy-bad shares drift. Carries the caveats:
per-anchor denominators from the local `execution_state_size`, weekly anchor floor per
window, events are per-(tx, object) units (§2).

## Out of scope

- Pre-merge anchors (no local read coverage; sweep is post-merge by definition here).
- Sweeping the full-history event totals (era-complete already).
- Vectorizing `analysis_v2.q3_concentration`'s `.apply` (known slow spot — separate
  cleanup; the sweep driver uses its own vectorized reduction).

## Execution order

1. Code + verify on a 2-anchor smoke run (newest anchor must equal snapshot).
2. `collect_v2_sweep 30` overnight → analysis + charts for T=30 → sanity-read.
3. `collect_v2_sweep 90 180 365` over subsequent nights (resumable).
4. Part III report sections once all four series exist.
