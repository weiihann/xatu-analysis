# Hot vs cold state and warm-tier gas concentration on Ethereum mainnet

A replication and temporal extension of Toni Wahrstätter's `state_access` analysis, measuring
how much of Ethereum state is touched over rolling time windows and how that maps onto an
EIP-8188-style state-tiering scheme. Static snapshot anchored at block **24,870,000** (mainnet);
historical sweep over the post-Merge range at fixed windows of 30, 90, 180, and 365 days.

## 1. Summary

State-tiering proposals (EIP-8188, building on EIP-8037) price storage access by recency: a slot
modified within the last `W` days is **Active** (cheap to write); one that hasn't is **Inactive**
(expensive). The design question is where to put `W`.

Three findings, consistent from a single snapshot through ~3.5 years of post-Merge history:

- **Write traffic is heavily concentrated in a small, recently-touched slot set.** At `W = 30d`,
  the **2.96%** of storage slots that are warm absorb **84.8%** of SSTORE *update* gas — a **29×**
  concentration over their share of the slot population.
- **Coverage saturates early; widening `W` buys little.** Update-gas coverage rises from 72% at
  `W = 1d` to 85% at `W = 30d`, then only to ~88% at `W = 365d`. The marginal benefit of a wider
  window collapses well before 90 days.
- **The effect is stable over time.** Across every week from the Merge to April 2026, the warm
  tier captures ~80–88% of update gas; concentration matured from a post-Merge low to a steady
  regime by 2024 and held.

Together these say a **~30-day Active window** is the efficient operating point: it keeps the
Active state small (<3% of slots) while leaving the dominant majority of real write traffic in the
cheap tier. Pushing `W` higher inflates the Active set that nodes must maintain for negligible
additional coverage.

## 2. Background

Under state tiering, every storage slot is Active or Inactive depending on whether it was modified
within the active window `W`. Writing to an Active slot is cheap; writing to an Inactive slot pays
a premium. `W` is the knob the proposal sets.

SSTORE gas splits into two cases set by the value transition, independent of `W`:

| transition | meaning | nominal gas | tier |
| --- | --- | --- | --- |
| `0 → nonzero` | slot creation | ~22,100 | always **Inactive** (a new slot can't have been recently active) |
| `nonzero → nonzero` | slot update | ~5,000 | depends on `W` |

Because creations are always Inactive-priced, the window only governs the **update** portion of
SSTORE gas. The gas-concentration analysis is therefore restricted to updates.

## 3. Data and method

**Source.** Xatu `canonical_execution_balance_diffs`, `canonical_execution_nonce_diffs`, and
`canonical_execution_storage_diffs` (mainnet) on a self-hosted node, ~8.9B storage-diff rows.
Live-state totals (the denominators) come from `execution_state_size` on the ethPandaOps Xatu
cluster, which carries per-block snapshots; the self-hosted copy is empty. A two-profile ClickHouse
layer (`lib/clickhouse.py`) reads bulk diffs from the node and totals from ethPandaOps.

**Definitions.**

- *Modified / hot* — an account whose balance, nonce, or any storage slot changed at least once in
  the window; a storage slot counts if `(address, slot)` appears in the storage diffs over the
  window. *Cold* is the complement against the live-state total.
- *Warm set* — the distinct `(address, slot)` pairs modified in the `W` days before "today".
- *Update gas* — gas from `nonzero → nonzero` SSTOREs only. Each costs a near-constant ~5,000 gas,
  so the count-weighted warm fraction equals the gas-weighted fraction (the `pct_update_gas_warm`
  column).
- *Concentration* — `(% of update gas hitting warm) ÷ (% of slots that are warm)`, i.e. `Y / X`.
  A value of `1×` is no concentration (writes spread evenly); higher means the warm slots pull
  above their weight.

**Counting.** Cardinalities use `uniq` (HyperLogLog, ~1% error) rather than exact dedup; the node
has no proxy timeout forcing the original's chunked-sketch workaround. Warm-set membership hashes
the key with `cityHash64` and tests it with `GLOBAL IN`, so the past-window set is built once and
broadcast across the distributed tables instead of materialising a multi-million-row set.

**Windowing.** All windows use 7,200 blocks/day (12s post-Merge cadence). The historical sweep is
restricted to post-Merge anchors, with a per-window floor `start_block(W) = merge_block + (W+1)·7200`
so a window's entire lookback stays in the 12s-cadence regime.

## 4. Static snapshot — block 24,870,000

Live-state totals at the anchor: **379,632,901 accounts** and **1,552,604,459 storage slots**.

### Hot vs cold state by window

The cold tail dominates at every horizon tested. Even a 180-day "hot" lookback leaves ~84% of
storage slots cold.

| window | storage slots hot | storage cold |
| ---: | ---: | ---: |
| 1d | 0.10% | 99.90% |
| 30d | 2.96% | 97.04% |
| 90d | 8.03% | 91.97% |
| 180d | 15.62% | 84.38% |

![Hot share vs window](data/hot_share_vs_window.png)

### The tiering tradeoff

Widening `W` shrinks the cold bucket (an architectural cost — more state to keep Active) while
fewer of today's writes land in the cold tier (a user benefit). The benefit per step drops off
fast: going from a 1-day to a 7-day window moves storage writes-to-cold down ~8 points, but each
later step moves it by ~1 point or less.

| window | state cold | account writes → cold | storage writes → cold |
| ---: | ---: | ---: | ---: |
| 1d | 99.90% | 16.50% | 40.17% |
| 7d | 99.36% | 12.23% | 32.06% |
| 14d | 98.57% | 11.21% | 30.43% |
| 30d | 97.04% | 10.20% | 28.86% |
| 60d | 94.19% | 9.58% | 27.93% |

![Tiering tradeoff](data/tradeoff_cold_vs_writes.png)

### Gas concentration

A tiny warm slice captures most update gas, and the concentration is extreme at short windows.

| window | warm slots | update-gas warm | concentration |
| ---: | ---: | ---: | ---: |
| 1d | 0.10% | 72.47% | 726× |
| 7d | 0.64% | 81.40% | 127× |
| 14d | 1.44% | 83.11% | 58× |
| **30d** | **2.96%** | **84.79%** | **29×** |
| 60d | 5.81% | 85.74% | 15× |

Doubling the window from 30d to 60d doubles the warm-state cost (2.96% → 5.81% of slots) and halves
the concentration (29× → 15×) to gain under one point of update-gas coverage. That is the
diminishing-returns elbow: the heavy-hitting slots (stablecoin balances, DEX reserves, busy router
storage) are already warm at 30 days, and a wider window only sweeps in low-traffic slots.

![Gas concentration](data/gas_concentration.png)

## 5. Historical sweep — post-Merge, weekly anchors

The static numbers are one snapshot. To test stability, the same metrics were recomputed at weekly
anchors across the post-Merge range, at fixed windows of 30, 90, 180, and 365 days.

| window | anchors | span |
| ---: | ---: | --- |
| 30d | 186 | 2022-09 → 2026-04 |
| 90d | 173 | 2022-12 → 2026-04 |
| 180d | 160 | 2023-03 → 2026-04 |
| 365d | 133 | 2023-09 → 2026-04 |

### Across windows (2025 mean)

The relationships are monotonic in `W`: a wider window touches more state (cold% falls), captures a
little more update gas, and is far less concentrated (the warm tier is bigger but only marginally
more gas-laden).

| window | storage cold | update-gas warm | concentration |
| ---: | ---: | ---: | ---: |
| 30d | 97.5% | 82.5% | 34× |
| 90d | 93.4% | 84.2% | 13× |
| 180d | 88.0% | 85.7% | 7× |
| 365d | 78.0% | 87.1% | 4× |

The jump from 30d to 365d in update-gas coverage is only ~5 points (82.5% → 87.1%), while the warm
state and the maintenance cost it implies grow several-fold. This is the same elbow the static
snapshot shows, now confirmed against years of data.

### Over time

Concentration was lowest right after the Merge and matured to a steady regime by ~2024, as the set
of heavy-hitting slots established itself. It is stable thereafter at every window.

| window | 2022 | 2023 | 2024 | 2025 | 2026 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 30d | 15× | 23× | 35× | 34× | 28× |
| 90d | 6× | 8× | 13× | 13× | 10× |
| 180d | — | 5× | 7× | 7× | 6× |
| 365d | — | 3× | 3× | 4× | 4× |

**Update-gas coverage and concentration** (dual-axis: warm-gas % and the Y/X ratio):

![Concentration over time, W=30](data/history_w30_gas_concentration.png)
![Concentration over time, W=90](data/history_w90_gas_concentration.png)
![Concentration over time, W=180](data/history_w180_gas_concentration.png)
![Concentration over time, W=365](data/history_w365_gas_concentration.png)

**Cold-state share** — flat within each window; the level steps down as `W` widens (more state is
touched over a longer lookback):

![Cold state over time, W=30](data/history_w30_state_cold.png)
![Cold state over time, W=90](data/history_w90_state_cold.png)
![Cold state over time, W=180](data/history_w180_state_cold.png)
![Cold state over time, W=365](data/history_w365_state_cold.png)

**Writes hitting the cold tier** — the share of today's account and storage writes that land cold,
also stable over time and falling as `W` widens:

![Writes to cold over time, W=30](data/history_w30_writes_cold.png)
![Writes to cold over time, W=90](data/history_w90_writes_cold.png)
![Writes to cold over time, W=180](data/history_w180_writes_cold.png)
![Writes to cold over time, W=365](data/history_w365_writes_cold.png)

## 6. Data-quality note

The self-hosted node had a storage-diff ingestion gap: blocks ~23.27M–23.66M (~54 days, Sep–Oct
2025) were only ~0.5–1% present, plus a smaller dip around 23.10M. The gap first surfaced as
physically impossible results in the sweep — storage 100% cold and concentration up to ~30,000× —
which traced back to near-empty 30-day windows over the affected anchors.

The gap was confirmed by per-block coverage on the raw table, then backfilled from the ethPandaOps
cluster (which had ~99.9% coverage of the range) via `scripts/backfill_storage_diffs.py` — a
chunked, resumable copy that is idempotent against the ReplacingMergeTree table. After backfill the
region returned to ~99.9% coverage. Two downstream corrections followed:

- The static `W = 180d` window reaches back into the gap; its storage-slot count corrected from
  227.7M to **242.5M** (cold 85.3% → 84.4%). Windows ≤128d don't reach the gap and were unaffected.
- The sweep's gap-affected anchors were re-collected. With the gap filled, the per-window
  completeness filter (drop anchors below 70% of the median storage-slot count) excludes none, and
  all four series are unbroken.

All figures in this report are post-backfill.

## 7. Appendix

### Queries (`state_access/queries.py`)

Five builders, parameterised by anchor block `bn_now` and window `W` days; `7200` blocks = 1 day
(post-Merge). Mainnet only.

**1. `state_touched`** — unique accounts (3-way diff union) and unique storage slots in the window:

```sql
WITH {bn_now} AS bn_now, {bn_now - W*7200} AS bn_lo
SELECT
    (
        SELECT uniq(address) FROM (
            SELECT address FROM canonical_execution_balance_diffs
              WHERE meta_network_name='mainnet' AND block_number BETWEEN bn_lo AND bn_now
            UNION ALL
            SELECT address FROM canonical_execution_nonce_diffs
              WHERE meta_network_name='mainnet' AND block_number BETWEEN bn_lo AND bn_now
            UNION ALL
            SELECT address FROM canonical_execution_storage_diffs
              WHERE meta_network_name='mainnet' AND block_number BETWEEN bn_lo AND bn_now
        )
    ) AS unique_accounts,
    (
        SELECT uniq((address, slot)) FROM canonical_execution_storage_diffs
          WHERE meta_network_name='mainnet' AND block_number BETWEEN bn_lo AND bn_now
    ) AS unique_storage_slots
```

**2–4. writes-to-warm** — of *today's* writes (`[bn_now − 7200, bn_now]`), how many hit a key seen
in the prior `W` days. The prior-window set is never filtered: a slot is warm if it was touched at
all, created or updated. Shown for storage; `account_writes_warm` uses
`canonical_execution_balance_diffs` keyed on `cityHash64(address)`, and `update_writes_warm` adds
the `from_value` filter (commented below) to count updates only:

```sql
WITH {bn_now} AS bn_now, {bn_now - 7200} AS bn_today_start
SELECT
    count() AS today,
    countIf(cityHash64(address, slot) GLOBAL IN (
        SELECT cityHash64(address, slot)
        FROM canonical_execution_storage_diffs
        WHERE meta_network_name='mainnet'
          AND block_number BETWEEN {bn_today_start - W*7200} AND bn_today_start - 1
    )) AS warm,
    round(100 * warm / today, 4) AS pct_warm
FROM canonical_execution_storage_diffs
WHERE meta_network_name='mainnet'
  AND block_number BETWEEN bn_today_start AND bn_now
  -- update_writes_warm only: AND from_value != '0x0000…0000'  (32-byte zero; excludes creations)
```

**5. `totals`** — latest live-state snapshot at or before the anchor (ethPandaOps cluster):

```sql
SELECT block_number AS snapshot_block, accounts, storages
FROM execution_state_size
WHERE meta_network_name='mainnet' AND block_number <= {bn_now}
ORDER BY block_number DESC
LIMIT 1
```

### Data files (`state_access/data/`)

- Static: `hot_cold_state.parquet`, `tradeoff.parquet`, `gas_concentration.parquet`, `totals.json`,
  and the four `*_vs_window.png` / `*.png` charts.
- Historical: `history_w{30,90,180,365}.parquet` and `history_w{30,90,180,365}_{state_cold,writes_cold,gas_concentration}.png`.
