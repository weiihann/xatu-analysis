# Hot vs cold state, with reads — a four-set view of warmth

A wider, reads-aware companion to the original `state_access` analysis. The existing report
classifies state by **writes alone** (the `_diffs` tables); this one adds the **reads**
dimension (the `_reads` tables and `address_appearances`), partitions every object into
**W-only / R-only / R∩W** sets within each window, and looks at three things: how big each
set is (warmth), how often per-object accesses happen within each (composition), and how
much access volume lands on a small head (concentration).

Static snapshot, anchored at block **24,870,000** (mainnet) — the largest round block where
all four source families (writes, slot reads, account reads, `address_appearances`) overlap
on the local cluster. Windows: `W ∈ {1, 7, 14, 30, 60, 90, 180, 365}` days. Object types:
**storage slots** `(contract, slot)` and **accounts** `(address)`.

## 1. Summary

- **Reads dominate the warm set.** For both slots and accounts, the read-set `R` is roughly
  **1.4× the size of the write-set `W`** at every W. The warm set (`R∪W`) reaches
  **4.3% of all live state at W=30d** and **35.2% at W=365d** (combined accounts + slots).
  Using writes-only as the "hot" definition would miss ~30% of the warm set.
- **There are essentially no W-only objects.** Almost every object written in window `W` is
  also read in the same window. At W ≤ 90d there are zero W-only slots; at W=180d only
  ~20k slots are write-without-read out of 240M total. Same for accounts. The
  SLOAD-before-SSTORE pattern is near-universal, and on the account side every transacting
  account also appears in at least one read relationship (`tx_from`, `call_from`, etc.).
- **R-only is the meaningful "extra" the reads data unlocks.** It captures the asymmetric
  half of the warm set — objects being inspected but not modified. R-only reaches
  **10.6% of slots / 4.5% of accounts / 9.4% combined** at W=365d. These are immutable
  parameters, oracle reads, ENS lookups, view-function targets.
- **Slot accesses are heavily concentrated.** At W=30d, the top 1% of accessed slots
  captures **76% of reads / 62% of writes**. By W=365d these rise to 82% / 68%.
  Concentration *grows* with W rather than diluting, because the dense head doesn't move
  much as the tail expands.
- **R-only concentration is the highest of any partition.** ~88% of R-only slot accesses
  land on the top 1% of R-only slots at W=365d; for accounts the figure is ~98%. The
  long-tail of "read once then never again" objects is enormous, but a tiny set absorbs
  almost all the read pressure.

## 2. What changed vs the original analysis

| | original | v2 |
|---|---|---|
| sources | `_diffs` only | `_diffs` + `_reads` + `address_appearances` |
| access types | W (writes) | W, R, R∩W, R∪W, plus W-only / R-only partition |
| object types | accounts, slots | accounts, slots |
| windows | 1–180d | 1–365d |
| cardinality | `uniq` (HLL ~1%) | exact, from per-key GROUP BY |

The reads data unlocks the R-only slice — the part of the warm set that an
existing-implementation snapshot from `_diffs` alone can't see. The window list is widened
to 365d because the per-key GROUP BY scales better than HLL did on the original
`_diffs`-only path: even a 365d window over slot reads (~560M unique keys, ~7.7B accesses)
runs in ~5 min.

## 3. Data and method

### Source tables

| set | source |
|---|---|
| writes (accounts) | `canonical_execution_balance_diffs`, `canonical_execution_nonce_diffs`, `canonical_execution_contracts` (account creation, keyed on `contract_address`) |
| writes (slots) | `canonical_execution_storage_diffs` |
| reads (slots) | `canonical_execution_storage_reads` |
| reads (accounts, direct) | `canonical_execution_balance_reads`, `canonical_execution_nonce_reads` |
| reads (accounts, derived) | `canonical_execution_address_appearances`, filtered to relationships `{call_from, call_to, tx_from, tx_to, miner_fee, factory, create, suicide_refund, suicide}` |

`address_appearances` relationships `erc20_*` and `erc721_*` are excluded — they're token
log-emission artifacts, not state-access events.

### Set partition

Per `(window_days, object_type)` we partition every touched object into three disjoint
slices, with `(n_w, n_r)` per object measuring how many write events and how many read
events touched it inside the window:

- **W-only** — `n_w > 0 ∧ n_r = 0`
- **R-only** — `n_w = 0 ∧ n_r > 0`
- **R∩W** — `n_w > 0 ∧ n_r > 0`

Derived sets follow by union:
`|W| = |W-only| + |R∩W|`,
`|R| = |R-only| + |R∩W|`,
`|R∪W| = |W-only| + |R-only| + |R∩W|`.

### Query mechanism

One ClickHouse query per `(W, object_type)` returns a histogram with rows
`(slice, n_w, n_r, n_keys)`. The inner UNION ALL tags each row as write or read; an outer
GROUP BY on `cityHash64(key)` sums `n_w` / `n_r` per object; a final GROUP BY collapses to
the per-slice access-count histogram. The hash key keeps the distributed shuffle compact;
the histogram-of-counts result is small enough to land in Python (a few hundred thousand
rows max) where Q1 / Q2 / Q3 are derived. Code: `state_access/queries_v2.py`,
`collect_v2.py`, `analysis_v2.py`.

## 4. Q1 — Warmth (set sizes)

Per `(access_type, W, object_type)`, set size as a share of the live-state denominator.
Live-state totals come from `execution_state_size` at the anchor: **1,552,604,459 slots**,
**379,632,901 accounts**, **1,932,237,360 combined** (block 24,870,000).

**Slots** (% of 1.55B live slots):

| W (days) | R∪W | R∩W | R-only | W-only |
|---:|---:|---:|---:|---:|
| 1   |  0.17% |  0.10% |  0.07% | 0.000% |
| 7   |  0.99% |  0.64% |  0.36% | 0.000% |
| 14  |  2.07% |  1.42% |  0.64% | 0.000% |
| 30  |  4.18% |  2.93% |  1.25% | 0.000% |
| 60  |  8.11% |  5.77% |  2.34% | 0.000% |
| 90  | 11.26% |  7.98% |  3.28% | 0.000% |
| 180 | 21.40% | 15.49% |  5.91% | 0.001% |
| 365 | 36.05% | 25.43% | 10.61% | 0.001% |

**Accounts** (% of 380M live accounts):

| W (days) | R∪W | R∩W | R-only | W-only |
|---:|---:|---:|---:|---:|
| 1   |  0.23% |  0.19% |  0.04% | 0.000% |
| 7   |  1.29% |  1.13% |  0.15% | 0.000% |
| 14  |  2.21% |  1.93% |  0.28% | 0.000% |
| 30  |  4.53% |  4.07% |  0.46% | 0.000% |
| 60  |  8.29% |  7.60% |  0.69% | 0.000% |
| 90  | 12.34% | 11.46% |  0.88% | 0.000% |
| 180 | 21.16% | 19.04% |  2.12% | 0.000% |
| 365 | 31.84% | 27.35% |  4.49% | 0.000% |

**Combined** — pooling slots + accounts against the combined denominator (1.93B):

| W (days) | R∪W | R∩W | R-only | W-only |
|---:|---:|---:|---:|---:|
| 1   |  0.18% |  0.12% |  0.06% | 0.000% |
| 7   |  1.05% |  0.74% |  0.32% | 0.000% |
| 14  |  2.10% |  1.52% |  0.57% | 0.000% |
| 30  |  4.25% |  3.16% |  1.10% | 0.000% |
| 60  |  8.15% |  6.13% |  2.02% | 0.000% |
| 90  | 11.47% |  8.66% |  2.81% | 0.000% |
| 180 | 21.35% | 16.19% |  5.17% | 0.001% |
| 365 | 35.22% | 25.81% |  9.41% | 0.001% |

![Q1 warmth — slots](data/v2/q1_warmth_slot.png)
![Q1 warmth — accounts](data/v2/q1_warmth_account.png)
![Q1 warmth — combined](data/v2/q1_warmth_combined.png)

The four lines are the partition pieces; `R∪W` is the warm set, `R∩W` is the "truly
active" sub-tier. Two observations:

1. **`R∩W` (purple) sits on top of `R∪W` (gray) closely.** The vertical gap between them
   is `R-only`. By W=30d that gap is ~30% of the warm set, growing to ~30–40% at W=365d.
   That's the share of the warm set the existing writes-only analysis was missing.
2. **`W-only` (dashed orange) is on the x-axis everywhere.** Empirically, every writer is
   also a reader inside any window of meaningful size. The tiny ~20k W-only slots that
   appear at W=180/365 likely come from contracts that write storage as part of a
   constructor or self-destruct path that produces no separate SLOAD trace inside the
   window.

## 5. Q2 — Composition (access-frequency tail)

For each slice, the distribution of per-object access counts inside the window, binned
into `{1, 2-5, 6-50, 51-500, 500+}`. Stacked-bar shows share of the slice's objects in
each bin per W.

![Q2 composition — slots](data/v2/q2_composition_slot.png)
![Q2 composition — accounts](data/v2/q2_composition_account.png)

**Slot R-only** is dominated by **singly-touched objects** — ~72% of R-only slots in any W
are read exactly once and never again. The shape is stable across W: longer windows don't
"thicken" R-only by accumulating heavier-touch objects — they thicken it with another
generation of one-shot reads. This is the long-tail signature you'd expect from view
calls, oracle reads, ENS lookups, etc.

| W | bin "1" | bin "2-5" | bin "6-50" | bin "51-500" | bin "500+" |
|---:|---:|---:|---:|---:|---:|
| 1   | 71.8% | 19.4% | 7.5% | 1.2% | 0.1% |
| 30  | 71.9% | 21.1% | 6.0% | 0.9% | 0.2% |
| 365 | 75.8% | 18.8% | 4.7% | 0.6% | 0.1% |

**Slot R∩W** is shaped differently: ~90% of R∩W slots get 2–5 accesses in window. Almost
no R∩W slot is singly-touched (the slice excludes single-access keys by construction —
the slot was both read and written, so n_w + n_r ≥ 2).

**Account R-only** lives in a different regime entirely: instead of "read once and never
again", R-only accounts cluster in the **6–50 access** bin (~70–85% at every W). These
are addresses being called repeatedly within the window but not written — popular
contracts that contracts call but don't modify (router targets, factory addresses,
implementation addresses behind proxies). The singly-touched bin is <2%.

| W | bin "1" | bin "2-5" | bin "6-50" | bin "51-500" | bin "500+" |
|---:|---:|---:|---:|---:|---:|
| 1   | 0.6% | 10.9% | 73.5% | 12.7% | 2.3% |
| 30  | 1.5% | 10.6% | 83.1% |  3.4% | 1.4% |
| 365 | 1.3% | 27.3% | 69.1% |  1.8% | 0.4% |

**Account R∩W** also clusters at 6–50 accesses, but with a noticeably heavier 51–500 tail
than R-only at small W. These are accounts both written and read repeatedly — most likely
high-traffic EOAs and contract-treasury addresses with many transactions in window.

## 6. Q3 — Concentration (top-N share of accesses)

For each `(access_type, W, object_type)`, the share of accesses captured by the
top-1% and top-10% of objects (denominator: objects in the access-type set).

![Q3 concentration top-1% — slots](data/v2/q3_concentration_top1_slot.png)
![Q3 concentration top-10% — slots](data/v2/q3_concentration_top10_slot.png)
![Q3 concentration top-1% — accounts](data/v2/q3_concentration_top1_account.png)
![Q3 concentration top-10% — accounts](data/v2/q3_concentration_top10_account.png)

**Slots, top-1% share of accesses (selected):**

| W (days) | W | R | R∩W | R∪W | R-only |
|---:|---:|---:|---:|---:|---:|
| 1   | 48.0% | 60.0% | 49.9% | 56.4% | 65.7% |
| 30  | 62.3% | 76.3% | 65.5% | 72.3% | 83.8% |
| 90  | 66.1% | 79.8% | 70.0% | 76.1% | 87.1% |
| 365 | 68.2% | 81.5% | 73.5% | 77.9% | 87.7% |

**Accounts, top-1% share of accesses (selected):**

| W (days) | W | R | R∩W | R∪W | R-only |
|---:|---:|---:|---:|---:|---:|
| 1   | 40.8% | 61.4% | 46.7% | 57.5% | 78.9% |
| 30  | 60.5% | 80.8% | 67.8% | 77.7% | 96.0% |
| 90  | 64.0% | 85.1% | 74.3% | 82.0% | 98.0% |
| 365 | 68.4% | 88.9% | 81.1% | 86.6% | 97.7% |

Three readings:

1. **Reads concentrate more than writes.** At every W and every object type, `R` and
   `R-only` sit above `W` and `R∩W`. The most concentrated bucket is R-only, where ~88%
   of slot accesses and ~98% of account accesses land on the top 1% of objects at W ≥ 90d.
2. **Concentration grows with W.** Wider windows add tail keys that are themselves
   lightly accessed, so the head's relative weight rises, not falls. The effect is
   monotonic up to W=180d for slots and to W=365d for accounts.
3. **Slots and accounts have different concentration regimes.** Account R-only hits 98%
   of accesses in the top 1% of accounts at W=90d. That is a *one-thousand-to-one*
   compression — the read traffic on accounts is dominated by an extremely small set of
   popular contracts (likely DEX routers, multicall, weth9, common implementation
   contracts behind proxies). Slot R-only is heavily concentrated too but tops out at
   88%, reflecting that slot access is spread across many contracts.

## 7. What this opens up

The clearest findings worth pulling on next:

- **W-only is dead.** Any future "hot tier" definition keyed only on writes is going to
  miss ~30% of the warm set on either axis. If a future EIP wants a read-aware tier
  marker, the analysis basis is here.
- **R-only is the new lens.** The asymmetric slice — read but not written — is where the
  most interesting structure lives: heavy concentration, distinct composition (long-tail
  for slots, mid-range for accounts), and clean separation from write traffic. Worth a
  dedicated drill-down: which contracts dominate R-only? Which slots within those
  contracts? What's the contract-class breakdown of the R-only head?
- **Historical sweep.** Snapshot is one anchor (24,870,000, late-April 2026 in
  block-clock). Sweeping weekly over the post-Merge range would tell us whether the
  R/W ratio of ~1.4 has been stable, whether R-only's share grew after Dencun (blob /
  calldata changes), and whether the R-only-account concentration spike is recent.
- **Per-tx all-hot fraction.** A natural follow-on: what share of transactions touch
  *only* warm state (under the chosen `W`)? Per-tx framing gives a user-impact answer
  rather than a population-level one.

## Appendix — outputs

```
state_access/data/v2/
  slot_histogram.parquet         # raw (slice, n_w, n_r, n_keys) per W
  account_histogram.parquet
  q1_warmth_{slot,account}.parquet
  q2_composition_{slot,account}.parquet
  q3_concentration_{slot,account}.parquet
  q1_warmth_{slot,account}.png
  q2_composition_{slot,account}.png
  q3_concentration_{top1,top10}_{slot,account}.png
```

Reproduce: `uv run python -m state_access.collect_v2 && uv run python -m state_access.analysis_v2`.
`collect_v2` is resumable per `(W, object_type)` cell; delete the histogram parquet to force a
full re-pull. Verification checks (identity, partition-sum, monotonicity) live in
`analysis_v2.run_one`.
