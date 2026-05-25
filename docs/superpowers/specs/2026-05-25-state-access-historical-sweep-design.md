# state_access historical sweep — design

## Context

The `state_access` analysis (in this repo) measures hot vs cold Ethereum state and the
EIP-8188 gas-concentration view at a **single anchor block** (24,870,000). The open question
it cannot answer: are those findings — ~85% of state cold at W=30, ~85% of SSTORE update-gas
concentrated in <3% of slots, ~29× concentration — **stable over time**, or artifacts of one
snapshot day?

This sweep turns each headline number into a **time series**: hold the active-window `W` fixed,
move the anchor block across post-Merge history, and plot the trend. It is the deferred
"later phase" recorded when the static replication was built.

## Decisions (locked during brainstorming)

| decision | value | rationale |
|----------|-------|-----------|
| Metrics | all three families: hot/cold state share, writes-to-cold, gas concentration | complete picture; denominators available per-block |
| Window `W` | **30 days**, fixed | the recommended sweet spot; one pass of queries per anchor |
| Range | **post-Merge only**: block 15,537,394 → 24,870,000 (~3.5 yr) | 7,200 blocks/day is exact only post-Merge, so day-windowing is correct with zero extra code |
| Step | **weekly** (~50,400 blocks), ~185 anchors | fine enough to show weekday/fork-level structure; ~1–1.5 hr runtime |
| Compute strategy | outer anchor-loop reusing existing `queries.py` builders | zero new SQL; trivially correct; resumable |

`execution_state_size` on the ethpandaops cluster has per-block snapshots (block 0 → 25.13M),
so the total-state denominator is exact at every anchor.

## Architecture

The slow-query / fast-chart split mirrors the existing `collect.py` + `analysis.py`.

### `state_access/history_config.py`
Sweep parameters:
- `START_BLOCK = 15_537_394` (Merge), `END_BLOCK = 24_870_000`, `STEP = 50_400` (weekly), `W = 30`.
- `MERGE_BLOCK = 15_537_394`, `MERGE_TS = 1_663_224_179` (2022-09-15 06:42:59 UTC, the first PoS block), `SECONDS_PER_BLOCK = 12` — for the deterministic block→timestamp used on the date axis. Approximate (missed slots add small drift), which is fine for a date axis.
- `anchors() -> list[int]`: generated **descending from `END_BLOCK` by `STEP`** down to `START_BLOCK`, then sorted ascending — guarantees the final anchor coincides with the static run (24,870,000) for continuity.
- `HISTORY_PARQUET = DATA_DIR / "history_w30.parquet"`.

### `state_access/collect_history.py`
Resumable outer loop:
1. Load `HISTORY_PARQUET` if present; collect the set of `anchor_block`s already done.
2. For each anchor in `anchors()` not yet done:
   - run the four existing builders at `W=30` against `primary`:
     `state_touched`, `account_writes_warm`, `storage_writes_warm`, `update_writes_warm`;
   - run `totals(anchor)` against `ethpandaops`;
   - assemble one row (schema below), derive percentages;
   - **rewrite the full parquet** (<200 rows — cheap, and gives free crash-resumability).
3. Print progress per anchor (`anchor`, date, the headline metrics).

Designed to run in the background (`uv run python -m state_access.collect_history`).
Re-running after an interruption skips completed anchors.

**Row schema** (one per anchor):
`anchor_block, date, unique_accounts, unique_storage_slots, total_accounts, total_storages,
pct_accounts_cold, pct_storage_cold, acct_writes_cold_pct, storage_writes_cold_pct,
pct_update_gas_warm, pct_state_warm, concentration_x`.

Derivations (identical to the static analysis):
- `pct_*_cold = 100 - 100 * unique / total`
- `acct/storage_writes_cold_pct = 100 - pct_warm`
- `pct_state_warm = 100 * unique_storage_slots / total_storages`
- `concentration_x = pct_update_gas_warm / pct_state_warm`
- `date = MERGE_TS + (anchor_block - MERGE_BLOCK) * SECONDS_PER_BLOCK` → UTC datetime.

### `state_access/analysis_history.py`
`# %%` cells loading `HISTORY_PARQUET` and rendering three time-series charts (plotly, PNGs to
`data/`), x-axis = date:
1. Hot/cold state share (`pct_accounts_cold`, `pct_storage_cold`) vs date.
2. Writes-to-cold (`acct_writes_cold_pct`, `storage_writes_cold_pct`) vs date.
3. `pct_update_gas_warm` + `concentration_x` (secondary y) vs date.

Each chart annotates fork boundaries with vertical lines: Shanghai (~17,034,870),
Dencun (~19,426,587), Pectra (~22,431,084) — so regime shifts are readable directly.

## Data flow

```
anchors() ─┐
           ├─ per anchor T ─ primary:  state_touched / *_writes_warm / update_writes_warm (W=30)
           │                ethpandaops: totals(T)  → exact denominator at T
           └─ row → rewrite history_w30.parquet (checkpoint)
                              │
analysis_history.py ─ read parquet → 3 charts + summary table
```

## Error handling & resumability

- Checkpoint after every anchor → an interruption loses at most the in-flight anchor.
- A failed query on one anchor aborts the run (fail fast with the anchor + SQL context); the
  completed anchors are already persisted, so a fixed re-run resumes from there.
- `totals(T)` empty → raise (cannot happen in-range, but asserted).

## Reuse / new code

- **Reused, unchanged:** all of `lib/clickhouse.py` and `state_access/queries.py` (every query
  already exists and is verified against the DB).
- **New:** `history_config.py`, `collect_history.py`, `analysis_history.py` — anchor loop,
  checkpoint/resume, block→date, and the three trend charts.

## Verification

1. **Dry run, 3 anchors:** run with a temporarily reduced `anchors()` (e.g. `[START, mid, END]`);
   confirm the parquet has 3 rows and the `END` row matches the static `collect.py` numbers
   (same anchor → same values, within `uniq` noise).
2. **Resumability:** interrupt mid-run, re-run, confirm it skips done anchors and completes.
3. **Sanity:** `concentration_x` > 1 everywhere; all percentages in [0, 100]; dates monotonic
   with `anchor_block`.
4. **Full run:** ~185 anchors complete; `analysis_history.py` renders 3 PNGs; eyeball the
   curves for stability vs the static point.

## Out of scope

- Pre-Merge anchors (would need timestamp-based windowing).
- Multiple W (fixed at 30; a W-family sweep is a future extension).
- Single-pass bucketed query optimization (Approaches B/C) — only if the loop proves too slow.
