# Hot vs cold state, with reads — additive W / R / R∪W view

This analysis measures Ethereum state access over trailing time windows, separating
**writes** from **reads**. For each window it reports two disjoint sets — **W (writes)**
and **R (pure reads, deduped against W)** — plus their union **R∪W = W + R** (the full
warm set). It then asks, descriptively, what that state access looks like, and, as policy
counterfactuals, what an EIP-8188-style tiering scheme would do with it.

**Notation.** `W` / `R` / `R∪W` denote the access **sets** (writes / reads / union).
`T` denotes the **trailing window length** in days — to avoid colliding with the writes
set, the window is always `T`, never `W`.

Static snapshot, anchored at block **24,870,000** (mainnet) — the largest round block
where all four source families (writes, slot reads, account reads, `address_appearances`)
overlap on the local cluster. Windows: `T ∈ {1, 7, 14, 30, 60, 90, 180, 365}` days. Object
types: **storage slots** `(contract, slot)` and **accounts** `(address)`.

## 1. Summary

- **Writes are a small slice of live state.** `|W|` at T=30d is **2.93% of live slots**
  and **4.07% of live accounts**; at T=365d, 25.4% / 27.4%.
- **R is the reads dimension the `_diffs` tables can't see.** At T=30d, **1.25% of slots
  and 0.46% of accounts** are read-but-not-written in window. At T=365d these grow to
  **10.6% / 4.5%**. By construction R never overlaps W.
- **The full warm set R∪W is 30–40% larger than W alone** at every T. At T=30d the
  combined-state warm set is **4.25%** (vs 3.16% for writes alone); at T=365d it's
  **35.2%** (vs 25.8%). The mass you'd miss with a writes-only definition is consistently
  **~30%**.
- **R is much more concentrated than W.** At T=30d the top 1% of objects captures **84%
  of read events** (slots) or **96%** (accounts), versus **62% / 61%** for writes. R-only
  objects are a long tail dominated by one-shot view-call targets — but the head of that
  tail is extremely heavy.
- **Slot W is dominated by creations, not updates.** At T=365d, **88% of W slots have any
  creation event** and only **21% have any update**; on a disjoint partition, **62% of W
  slots are create-only** (write was a `0→nonzero` initialization, nothing else in
  window). Most "warm slots" by W's definition are warm because they're being **born**,
  not modified — state growth, not state churn. EIP-8188 only reprices updates, so the
  policy-relevant write set is closer to 4% of state at T=365d (update-touching slots),
  not 25%.
- **Slot R is dominated by empty-slot probes.** At T=365d, **93% of R slots had at least
  one read returning `value=0`** ("does this slot exist?" checks). Only **7% of R slots**
  had any read return populated data. The `R_mixed` partition (slots with both zero and
  nonzero reads) is identically **zero** by structure: an R-only slot has no writes in
  window, so its value is stable through the window, so all reads return the same value.
- **EIP-8188 covers update gas very well at the policy-relevant window.** Under per-event
  semantics (intra-window promotion accounted for), **94% of update SSTORE events at
  T=30d would keep the Active price**; 97% at T=90d. The Inactive premium only affects
  ~3–6% of updates at policy-relevant T.

## 2. Data and method

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

### Set definitions

For each `(T, object_type)`:

- **W** — objects that appear in the writes-source tables in window (raw, no dedup).
- **R** — objects that appear in the reads-source tables AND not in the writes sources
  in the same window. Deduped against W by construction, so R ∩ W = ∅.
- **R∪W = W + R** — additive union, the full warm set.

**Why a 2-set additive view rather than a 3-way partition.** An earlier cut used a
`(W-only, R-only, R∩W)` partition. Empirically **W-only ≈ 0** at every window — every
written object is also read in the same window:

- **Slots:** Solidity codegen for `x = f(x)` emits `SLOAD; ...; SSTORE`. The pre-SSTORE
  `SLOAD` lands in `storage_reads`, so almost every modified slot is also read in window.
- **Accounts:** every transaction's `tx_from` appears in `address_appearances` (the
  sender's nonce is read for validation), and the same transaction emits balance / nonce
  diffs. So every written EOA / contract is also read in window.

With W-only essentially empty, the partition collapses to `W ∪ (R \ W) = R∪W`. That's the
additive view used throughout.

### Denominators

Set sizes are reported as a share of live state. Live-state denominators come from
`execution_state_size` at the anchor: **1,552,604,459 slots**, **379,632,901 accounts**,
**1,932,237,360 combined**. All SQL is in Appendix A. Code:
`state_access/queries_v2.py`, `collect_v2.py`, `analysis_v2.py`.

---

# Part I — Descriptive: what state access looks like

## 3. Warmth — set sizes (W / R / R∪W as % of live state)

**Slots** (% of 1.55B live slots):

| T (days) | W | R | R∪W |
|---:|---:|---:|---:|
| 1   |  0.10% |  0.07% |  0.17% |
| 7   |  0.64% |  0.36% |  0.99% |
| 14  |  1.42% |  0.64% |  2.07% |
| 30  |  2.93% |  1.25% |  4.18% |
| 60  |  5.77% |  2.34% |  8.11% |
| 90  |  7.98% |  3.28% | 11.26% |
| 180 | 15.49% |  5.91% | 21.40% |
| 365 | 25.43% | 10.61% | 36.05% |

**Accounts** (% of 380M live accounts):

| T (days) | W | R | R∪W |
|---:|---:|---:|---:|
| 1   |  0.19% |  0.04% |  0.23% |
| 7   |  1.13% |  0.15% |  1.29% |
| 14  |  1.93% |  0.28% |  2.21% |
| 30  |  4.07% |  0.46% |  4.53% |
| 60  |  7.60% |  0.69% |  8.29% |
| 90  | 11.46% |  0.88% | 12.34% |
| 180 | 19.04% |  2.12% | 21.16% |
| 365 | 27.35% |  4.49% | 31.84% |

**Combined** — pooling slots + accounts against the combined denominator (1.93B):

| T (days) | W | R | R∪W |
|---:|---:|---:|---:|
| 1   |  0.12% |  0.06% |  0.18% |
| 7   |  0.74% |  0.32% |  1.05% |
| 14  |  1.52% |  0.57% |  2.10% |
| 30  |  3.16% |  1.10% |  4.25% |
| 60  |  6.13% |  2.02% |  8.15% |
| 90  |  8.66% |  2.81% | 11.47% |
| 180 | 16.19% |  5.17% | 21.35% |
| 365 | 25.81% |  9.41% | 35.22% |

![Warmth — slots](data/v2/q1_warmth_slot.png)
![Warmth — accounts](data/v2/q1_warmth_account.png)
![Warmth — combined](data/v2/q1_warmth_combined.png)

Two observations:

1. **R for slots is much larger than R for accounts at all windows.** At T=365d,
   R-slots = 10.6% vs R-accounts = 4.5%. Slot-level reads have a deeper unread tail
   (every contract storage has view-only parameters); account-level reads cluster on a
   smaller universe of popular contracts.
2. **R grows faster than W as T increases.** For slots, R/W is 0.67× at T=1d but 0.42×
   at T=365d — R adds more relative to W at small T. For accounts, R/W is 0.19× at T=1d
   and 0.16× at T=365d — the read-only account tail grows much slower than writes. Both
   ratios are bounded above zero, so reads always add something.

## 4. Write structure — slot W by value transition

For storage slots, the writes carry a value transition that's EIP-8188-relevant:
`create` (0→nonzero, ~20k gas, always Inactive-priced under EIP-8188), `update`
(X→Y nonzero→nonzero, ~5k gas, what the policy actually reprices), `delete`
(nonzero→0, refund). A single slot can have multiple write types in window (created
early, updated later); the disjoint partition below picks slots whose writes are ALL of
one type ("create-only" etc.) and lumps the rest into "mixed".

### Slot W partitioned by transition type (% of live state)

Stacked total = W. Verification: the four columns sum exactly to W from §3.

| T (days) | create-only | update-only | delete-only | mixed (≥2 types) | W |
|---:|---:|---:|---:|---:|---:|
| 1   |  0.045% | 0.034% | 0.005% | 0.016% |  0.10% |
| 7   |  0.327% | 0.135% | 0.043% | 0.133% |  0.64% |
| 14  |  0.827% | 0.248% | 0.091% | 0.257% |  1.42% |
| 30  |  1.748% | 0.472% | 0.162% | 0.552% |  2.93% |
| 60  |  3.635% | 0.672% | 0.258% | 1.206% |  5.77% |
| 90  |  4.876% | 0.862% | 0.344% | 1.893% |  7.98% |
| 180 |  9.874% | 1.407% | 0.650% | 3.559% | 15.49% |
| 365 | 15.675% | 2.078% | 0.939% | 6.743% | 25.43% |

### Slot W subtype touch rates (overlap allowed, % of |W|)

A slot can have multiple write types in window. These are the share of |W| with at least
one event of each type — the rows do not sum to 100%.

| T (days) | any create | any update | any delete |
|---:|---:|---:|---:|
| 1   | 59.6% | 40.3% | 17.7% |
| 7   | 71.4% | 28.7% | 23.3% |
| 30  | 78.0% | 23.5% | 19.7% |
| 90  | 84.6% | 21.6% | 20.4% |
| 365 | 87.8% | 21.4% | 20.8% |

**Read this carefully.** Of the 25.4% of live slots in |W| at T=365d:
- **88% have at least one creation event** (76% are pure create-only); the slot is in W
  primarily because it was *initialized* in window.
- **21% have at least one update**; the EIP-8188-relevant subset of W.
- **21% have at least one deletion**.

The update fraction of |W| **stays remarkably stable around 21–24%** across windows from
7d to 365d. So **|W ∩ updates| ≈ 21% × |W|** at every T of interest. At T=30d that's
21% × 2.93% = **0.7% of live state**; at T=365d, 21% × 25.43% = **5.3% of live state**.
These are the slots EIP-8188 would actually reprice.

The create-only fraction *grows* with T (60% → 76% over 1d → 365d) because the longer the
window, the more newly-created slots accumulate without subsequent activity. Slot creation
is a one-shot event by nature.

![Slot W partition](data/v2/q1_warmth_slot_W_typed.png)

### Decomposing W_mixed — what's in the "≥2 types" bucket?

W_mixed at T=365d is **6.74% of state** (~27% of |W|), so it earns its own decomposition.
Structurally, slots in W_mixed carry ≥2 of `{create, update, delete}`. Combining these
into the 6 possible patterns, sub-binning the multi-cycle-capable ones by create-count:

| combo | structural rule | count constraint |
|---|---|---|
| `C+U` | no delete | `n_C = 1` always (multiple creates need deletes between them) |
| `C+D (1-cycle)` | no update; `n_C = 1` | born once, died once — single ephemeral lifecycle |
| `C+D (multi-cycle)` | no update; `n_C ≥ 2` | repeated birth/death cycles, no modification |
| `U+D` | no create; `n_D = 1` always | k updates then one terminal delete (existed pre-window) |
| `C+U+D (1-cycle)` | all three; `n_C = 1` | full single lifecycle (born, modified, died) |
| `C+U+D (multi-cycle)` | all three; `n_C ≥ 2` | full lifecycle, repeated |

Composition of W_mixed at each T (% of W_mixed; rows sum to 100%):

| T (days) | C+U | C+D (1-cycle) | C+D (multi) | U+D | C+U+D (1-cycle) | C+U+D (multi) |
|---:|---:|---:|---:|---:|---:|---:|
| 1   | 20.45% | 52.98% | 8.04% | 5.71% | 7.18% | 5.64% |
| 7   | 20.64% | 55.74% | 8.10% | 3.47% | 6.16% | 5.88% |
| 14  | 21.50% | 54.85% | 8.30% | 3.13% | 6.20% | 6.03% |
| 30  | 24.59% | 51.02% | 9.41% | 2.29% | 5.98% | 6.71% |
| 60  | 27.26% | 49.35% | 9.86% | 1.55% | 5.72% | 6.26% |
| 90  | 32.11% | 45.26% | 9.08% | 1.36% | 5.87% | 6.31% |
| 180 | 33.02% | 44.37% | 8.56% | 1.49% | 5.90% | 6.66% |
| 365 | 35.62% | 42.42% | 7.84% | 1.35% | 6.36% | 6.42% |

Absolute share of live state (W_mixed sub-categories, % of state):

| T (days) | C+U | C+D (1-cycle) | C+D (multi) | U+D | C+U+D (1-cycle) | C+U+D (multi) |
|---:|---:|---:|---:|---:|---:|---:|
| 1   | 0.0032% | 0.0082% | 0.0013% | 0.0009% | 0.0011% | 0.0009% |
| 30  | 0.1357% | 0.2816% | 0.0520% | 0.0126% | 0.0330% | 0.0370% |
| 90  | 0.6078% | 0.8568% | 0.1719% | 0.0258% | 0.1112% | 0.1194% |
| 365 | 2.4016% | 2.8603% | 0.5287% | 0.0908% | 0.4285% | 0.4326% |

![W_mixed decomposition](data/v2/q1_warmth_slot_mixed_decomp.png)

Three things worth reading off:

1. **C+D (1-cycle) is the largest mixed category at almost every T** — 42–56%. These are
   slots **born and died in the same window with exactly one create and one delete**.
   Ephemeral state — probably temporary mapping entries, intermediate compute slots, or
   "pending" markers cleaned up after consumption. At T=365d this single category accounts
   for **2.86% of live state**, the largest piece of W_mixed.
2. **C+U grows steadily with T** (20% at T=1d → 35.6% at T=365d). These are slots created
   in window then modified at least once but not deleted — the "fresh hot storage"
   pattern. Their share rises with T because they accumulate updates over time without
   dying.
3. **U+D shrinks as T grows** (5.7% at T=1d → 1.4% at T=365d). At small T the
   "modified-then-died" pattern is more visible because long lifecycle histories haven't
   completed yet; at large T most pre-existing slots that die also get recreated somewhere
   along the way (moving them to C+U+D), and many that don't die stay in W_only_update.

**Multi-cycle is a stable minority.** C+D multi-cycle and C+U+D multi-cycle each sit around
6–10% of W_mixed at every T. Combined ~14%. So the "slot churns through multiple
birth-death cycles in window" pattern is real but small — most mixed slots have a single
lifecycle within window.

## 5. Read structure — slot R by returned value

Slot reads split by the returned `value`: `zero` (the slot was empty when read — an
"is this slot set?" probe) vs `nonzero` (a populated read returning real data). For R the
partition is trivially clean: an R-only slot has no writes in window, so its value is
stable, so all reads return the same value — `R_mixed = 0` everywhere.

### Slot R partitioned by returned value (% of live state)

R_mixed is omitted (always zero). Stacked total = R.

| T (days) | zero-only | nonzero-only | R |
|---:|---:|---:|---:|
| 1   |  0.045% | 0.022% |  0.07% |
| 7   |  0.279% | 0.077% |  0.36% |
| 14  |  0.513% | 0.131% |  0.64% |
| 30  |  1.037% | 0.213% |  1.25% |
| 60  |  2.046% | 0.293% |  2.34% |
| 90  |  2.916% | 0.364% |  3.28% |
| 180 |  5.373% | 0.535% |  5.91% |
| 365 |  9.838% | 0.754% | 10.61% |

### Slot R — share of |R| by returned value

| T (days) | zero | nonzero |
|---:|---:|---:|
| 1   | 67.8% | 32.2% |
| 7   | 78.4% | 21.6% |
| 30  | 83.0% | 17.0% |
| 90  | 88.9% | 11.1% |
| 365 | 92.9% |  7.3% |

**The zero-share grows monotonically with T**, reaching 93% at T=365d. So:

- **Most of slot R is empty-slot probes** — `SLOAD` returning 0 against slots that haven't
  been written in (or before) the window. Typical cases: a contract checking whether a
  mapping entry exists (`mapping[key]` returns 0 if unset), state-existence guards
  (`if (slot == 0) revert()`), default-value reads.
- **Only ~7% of R at T=365d is genuine populated state inspection** — `SLOAD` returning
  meaningful data from slots that hold real state but happen not to be modified in this
  window (oracle parameters, contract config, immutable-style storage variables filled at
  deploy then never changed).
- The nonzero-only share *shrinks* with T because the universe of "ever-set slots that
  weren't touched in 365 days" grows much slower than the universe of "slots probed with
  `SLOAD` and found empty" — empty-slot probes scale with calldata-driven access,
  populated reads scale with the small set of legitimately read-only state.

![Slot R partition](data/v2/q1_warmth_slot_R_typed.png)

## 6. Concentration — top-N share of accesses

For each `(access_set, T, object_type)`, the share of access events captured by the top-1%
and top-10% of objects (denominator: objects in the access set).

![Concentration top-1% — slots](data/v2/q3_concentration_top1_slot.png)
![Concentration top-10% — slots](data/v2/q3_concentration_top10_slot.png)
![Concentration top-1% — accounts](data/v2/q3_concentration_top1_account.png)
![Concentration top-10% — accounts](data/v2/q3_concentration_top10_account.png)

**Slots, top-1% share of accesses:**

| T (days) | W | R | R∪W |
|---:|---:|---:|---:|
| 1   | 48.0% | 65.7% | 56.4% |
| 30  | 62.3% | 83.8% | 72.3% |
| 90  | 66.1% | 87.1% | 76.1% |
| 365 | 68.2% | 87.7% | 77.9% |

**Accounts, top-1% share of accesses:**

| T (days) | W | R | R∪W |
|---:|---:|---:|---:|
| 1   | 40.8% | 78.9% | 57.5% |
| 30  | 60.5% | 96.0% | 77.7% |
| 90  | 64.0% | 98.0% | 82.0% |
| 365 | 68.4% | 97.7% | 86.6% |

Three readings:

1. **R is more concentrated than W at every T and every object type.** R-only sees ~88%
   of slot accesses on the top 1% at T=365d; for accounts the figure is ~98%. A handful of
   popular contracts absorb essentially all the read pressure on accounts.
2. **Concentration grows with T.** Wider windows pull in more tail keys that themselves get
   few accesses, so the head's relative weight rises monotonically. The effect is sharpest
   going from T=1d to T=30d (~14pp for slot R; ~17pp for account R); past T=30d the gain
   flattens.
3. **Accounts concentrate far more tightly than slots.** Account R-only at T=30d: top-1%
   captures 96% of accesses. Slot R-only at T=30d: 84%. Read traffic on accounts is
   dominated by an extraordinarily small set of popular contracts (likely DEX routers,
   multicall, weth9, common implementation contracts behind proxies); slot reads are spread
   more broadly because they're reads against many contracts' storage.

---

# Part II — Policy implications (EIP-8188 counterfactuals)

## 7. Warm-update coverage under EIP-8188

The set-membership views above can't directly answer **"of the update gas spent in window,
what fraction would be priced as Active under EIP-8188?"** — that question is per-event,
not per-key. A naive measurement checks each update against a *static* past-window warm
set, which double-counts the cold tier: a slot that gets its first write inside the window
and then a second update later is checked against the past-only set both times, so both
come out cold even though EIP-8188 would only price the first as Inactive. The per-event
measurement below promotes the slot intra-window.

### Definition

For each window `[anchor − T·7200, anchor]`, classify every update SSTORE event
(`from_value ≠ 0 ∧ to_value ≠ 0`) as **warm** or **cold**:

- **warm** — at least one `create` (`0 → nonzero`) or `update` event happened earlier on
  the same slot at an earlier event-order within the window.
- **cold** — the update IS the slot's first warming event in window (no preceding
  create-or-update for this slot in window). Deletions don't count as warming events.

Algorithm — one GROUP BY on `cityHash64(address, slot)`:

1. For each slot in window, count update events (`n_update`).
2. Among the slot's create-or-update events, take the one with the smallest
   `(block_number, transaction_index, internal_index)` order. If that earliest warming
   event is itself an update, the slot contributes one cold update; otherwise all of its
   updates are warm.

That is: `warm_per_slot = n_update − first_cu_is_update`. Sum across slots.

### Results

| T (days) | total updates | warm | cold | % warm |
|---:|---:|---:|---:|---:|
| 1   |     3,827,500 |     3,277,106 |     550,394 | **85.62%** |
| 7   |    28,158,628 |    25,937,983 |   2,220,645 | **92.11%** |
| 14  |    55,135,247 |    51,051,775 |   4,083,472 | **92.59%** |
| 30  |   125,684,703 |   117,983,724 |   7,700,979 | **93.87%** |
| 60  |   262,053,193 |   251,043,962 |  11,009,231 | **95.80%** |
| 90  |   402,058,353 |   387,899,906 |  14,158,447 | **96.48%** |
| 180 |   788,431,719 |   765,249,545 |  23,182,174 | **97.06%** |
| 365 | 1,424,321,470 | 1,389,975,882 |  34,345,588 | **97.59%** |

![Slot update coverage — warm vs cold](data/v2/slot_update_coverage.png)

A static past-window check yields only **84.8% at T=30d** — a ~9 pp underestimate against
the per-event **93.9%**. The gap is exactly the intra-window promotion: slots that get
their first write inside the window and then get hit again. Three readings:

1. **EIP-8188's Active tier covers update gas extremely well.** At T=30d, **94%** of update
   SSTOREs would keep the cheap Active price. At T=90d, 97%. The Inactive premium only
   affects ~3–6% of update events at policy-relevant T.
2. **The benefit saturates fast.** Going from T=1d → 30d buys +8 pp of coverage; T=30d →
   365d buys only another +3.7 pp. The marginal benefit of stretching T beyond ~30d is
   small for update gas.
3. **Cold updates correspond exactly to first-touch awakenings of cold state.** The raw
   count of cold updates at T=365d is **34.3M slots over a year** — the long tail of state
   reactivated after a year of dormancy. (Per-slot, not per-event; a slot has at most one
   cold-update event in window, the first warming.)

## 8. Read-side period-bump: first-operation classification

The §3 view treats W and R as set-membership; §7 treats updates as events. A third
question is about the **first operation per object** in window:

> Under a hypothetical extension of EIP-8188 where the first read of an inactive object
> also bumps its period — making reads write-like for users — which objects pay that cost?

The bad-UX set is **objects whose first in-window event is a nonzero read**:

- For a slot, "nonzero read" means SLOAD returning a populated value (zero reads don't bump
  anything because the slot has no period at value=0).
- For an account, "nonzero read" means `balance_reads` returning balance > 0 or
  `nonce_reads` returning nonce > 0 (empty accounts don't have a period either).

A write or a zero read as the first event has no policy cost: writes already bump the
period under base EIP-8188, and zero reads target objects that don't exist yet.

### Method

Per object, pick the event with the smallest `(block_number, transaction_index)` in window
and classify it. `internal_index` is a per-table index (per `_diffs` or per-`_reads`), not
a cross-table opcode order, so we can't use it to break ties between a read and a write in
the same transaction — we adopt a deterministic convention: at the same `(block, tx_idx)`,
**writes > nonzero reads > zero reads > appearance reads**. This under-counts the policy-bad
set (a read that actually preceded a write in the same tx gets classified as
"first = write"), but it's the safer error to make.

The account query needs no JOIN against `canonical_execution_transaction`:
`canonical_execution_contracts` is dropped (every contract creation also emits a
`nonce_diff` on the same address at the same real tx_index, so the account is already
captured as a write with correct ordering), and `address_appearances` (which lacks a
`transaction_index`) is given an end-of-block sort position — the block number dominates
`event_order`, so cross-block ordering is exact and the appearance correctly loses every
same-block tie.

### Slots — first-operation classification

For each T, the share of slots in R∪W by what their first event is:

| T (days) | first = write | first = zero read | first = nonzero read |
|---:|---:|---:|---:|
| 1   | 58.26% | 27.97% | **13.77%** |
| 7   | 62.74% | 28.97% |  8.29% |
| 14  | 67.56% | 25.66% |  6.78% |
| 30  | 68.54% | 25.91% |  5.56% |
| 60  | 69.72% | 26.33% |  3.94% |
| 90  | 69.42% | 27.02% |  3.56% |
| 180 | 71.05% | 26.22% |  2.73% |
| 365 | 69.31% | 28.42% |  2.27% |

![Slot first-op classification](data/v2/slot_first_op.png)

**Reading.** At T=30d, **5.56% of slots in R∪W** (≈ 3.6M slots) would be hit by the
hypothetical read-side period bump — their first in-window event is an SLOAD returning a
populated value. This falls monotonically to **2.27% at T=365d** because as T grows, the
chance that an object has *some* write earlier in the same window grows too.

The 26–28% **zero-read** band is large but policy-irrelevant under this framing — those are
first-time SLOADs on slots that have no period to bump. They're just structural "slot
didn't exist" probes (§5).

### Accounts — first-operation classification

For each T, the share of accounts in R∪W by what their first event is:

| T (days) | first = write | first = nonzero read | first = zero read |
|---:|---:|---:|---:|
| 1   | 83.84% | **15.75%** |  0.41% |
| 7   | 87.36% | 11.98% |  0.67% |
| 14  | 86.79% | 12.51% |  0.71% |
| 30  | 88.96% | 10.36% |  0.68% |
| 60  | 90.78% |  8.48% |  0.75% |
| 90  | 92.24% |  7.07% |  0.69% |
| 180 | 84.85% | 10.65% |  4.51% |
| 365 | 81.05% |  8.43% | 10.52% |

(`first = appearance read` is identically 0 — appearance reads always lose the tie-break to
balance/nonce reads when present at the same `(block, tx_idx)`, because the same tx that
emits an appearance also emits balance and nonce reads on the same account.)

![Account first-op classification](data/v2/account_first_op.png)

**Reading.** The policy-bad set is even more pronounced for accounts at small T:
**15.75% of warm accounts at T=1d** have a nonzero balance/nonce read as their first event.
It bottoms out around 7% at T=90d, then ticks back up — at T=180d/365d the zero-read band
swells (4.5% / 10.5%) as the universe of long-dormant accounts probed-while-empty grows.
Most of the nonzero-read-first accounts are "view-call targets" — popular contracts called
read-only via balance/nonce checks before a tx decides whether to interact.

### R-only accounts — empty vs non-empty

Within the R set (accounts in `_reads` with NO write in window), balance and nonce are
stable through window (no writes ⇒ no value changes). So we can classify each R-only
account once:

- **empty**: `max(balance) = 0 AND max(nonce) = 0` from `balance_reads` and `nonce_reads`
  in window.
- **non-empty**: `max(balance) > 0 OR max(nonce) > 0`.
- **unknown**: no `balance_reads` or `nonce_reads` in window — only observed via
  `address_appearances`. No value-level data to classify.

| T (days) | R-only total | empty | non-empty | unknown |
|---:|---:|---:|---:|---:|
| 1   |    135,922 | 1.79% | **98.21%** | 0 |
| 7   |    586,826 | 3.71% | **96.29%** | 0 |
| 14  |  1,065,388 | 3.66% | 96.34% | 0 |
| 30  |  1,752,187 | 4.24% | 95.76% | 0 |
| 60  |  2,619,858 | 5.68% | 94.32% | 0 |
| 90  |  3,327,941 | 5.98% | 94.02% | 0 |
| 180 |  8,061,680 | 5.71% | 94.29% | 10 |
| 365 | 17,025,144 | 7.34% | **92.66%** | 11 |

**Almost every R-only account is non-empty** — 93–98% across all windows, drifting only
slightly toward empty as T grows (7.3% empty at T=365d). So under read-side EIP-8188,
virtually all R-only account reads would bump a real period, converting reads into writes
from the user's perspective. The empty-account "free pass" is structurally tiny. `unknown`
is negligible (≤11 accounts at any T) — almost every R-only account that appears via
`address_appearances` also has at least one balance or nonce read in window.

This is the policy-relevant complement to the first-op analysis: even for accounts that
aren't first-event reads, the *pure* R-only slice (where any read at all bumps a period) is
dominated by non-empty objects.

> Caveat: at every T, the **confirmed**-empty count (both balance and nonce read, both 0)
> is ~0 — the "empty" bucket is entirely accounts whose balance was read as 0 with the
> nonce never read. These are overwhelmingly `call_to` / `tx_to` recipients: a CALL or
> transfer reads the target's balance but never its nonce (only the sender's nonce is
> consulted, and that's an increment = write). So "empty" reads as "zero-value call target,
> existence unconfirmed" — and the bias is conservative for the policy conclusion (any
> dormant nonce>0 EOA misclassified here would be non-empty, pushing the policy-bad share
> *up*).

## 9. What this opens up

The clearest threads worth pulling next:

- **W ⊂ R operationally, even though sets are disjoint by construction.** Almost every W
  object also appears in `_reads`. The structural reason (SLOAD-then-SSTORE codegen,
  tx_from nonce reads) is interesting on its own. A `_reads` signal is informative for
  near-term tiering decisions — it captures both views and writes.
- **R-only is the new lens.** The asymmetric slice — read but not written — is where the
  most distinctive structure lives: 30% extra warm-set mass over writes alone, and very
  heavy concentration (~88% top-1% for slots, ~98% for accounts). Worth a dedicated
  drill-down: which contracts dominate R-only? What's the contract-class breakdown of the
  R-only head?
- **Historical sweep.** Snapshot is one anchor (24,870,000, late-Apr 2026). Sweeping weekly
  over the post-Merge range would tell us whether the R/W ratio is stable, whether R's
  share grew after Dencun (blob / calldata changes), and whether the R-only-account
  concentration spike is recent.
- **Per-tx all-hot fraction.** A natural follow-on: what share of transactions touch *only*
  warm state (under chosen `T`)? Per-tx framing gives a user-impact answer rather than a
  population-level one.

---

## Appendix A — SQL queries

All queries run per `(T, object_type)` over the trailing block range
`bn_lo = anchor − T·7200`, `bn_hi = anchor` (post-Merge cadence 7,200 blocks/day; anchor
`24,870,000`; `T ∈ {1, 7, 14, 30, 60, 90, 180, 365}`). Full builders live in
`state_access/queries_v2.py`.

### Slot histogram (warmth, write/read structure, concentration)

Storage-slot key is `(contract_address, slot)`. Writes come from `storage_diffs`; reads
from `storage_reads`. The inner UNION ALL tags each row with `is_w` / `is_r`; the outer
GROUP BY on `cityHash64(address, slot)` sums them per key; the final GROUP BY collapses to
`(slice, n_w, n_r, n_keys)`. The slice partition (`w_only` / `r_only` / `rw`) is re-mapped
in Python to the W / R sets: W = `w_only ∪ rw`, R = `r_only`, R∪W = all three.

```sql
WITH per_key AS (
    SELECT
        h,
        sum(is_w) AS n_w,
        sum(is_r) AS n_r
    FROM (
        SELECT cityHash64(address, slot) AS h, 1 AS is_w, 0 AS is_r
        FROM canonical_execution_storage_diffs
        WHERE meta_network_name = 'mainnet'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(contract_address, slot) AS h, 0 AS is_w, 1 AS is_r
        FROM canonical_execution_storage_reads
        WHERE meta_network_name = 'mainnet'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
    )
    GROUP BY h
)
SELECT
    multiIf(n_w > 0 AND n_r > 0, 'rw',
            n_w > 0,             'w_only',
                                 'r_only') AS slice,
    n_w,
    n_r,
    count() AS n_keys
FROM per_key
GROUP BY slice, n_w, n_r
ORDER BY slice, n_w, n_r
```

### Slot typed histogram (§4 / §5)

Same per-key GROUP BY shape, but with five typed counters in place of two. Each write event
splits into `create` / `update` / `delete` by `(from_value, to_value)` and each read into
`zero` / `nonzero` by the returned `value`. Output: one row per distinct
`(n_w_create, n_w_update, n_w_delete, n_r_zero, n_r_nonzero)` count-tuple, with `n_keys`.

```sql
WITH per_key AS (
    SELECT
        h,
        sum(is_w_create)  AS n_w_create,
        sum(is_w_update)  AS n_w_update,
        sum(is_w_delete)  AS n_w_delete,
        sum(is_r_zero)    AS n_r_zero,
        sum(is_r_nonzero) AS n_r_nonzero
    FROM (
        SELECT
            cityHash64(address, slot) AS h,
            toUInt8(from_value =  '0x000…0' AND to_value != '0x000…0') AS is_w_create,
            toUInt8(from_value != '0x000…0' AND to_value != '0x000…0') AS is_w_update,
            toUInt8(from_value != '0x000…0' AND to_value =  '0x000…0') AS is_w_delete,
            toUInt8(0) AS is_r_zero,
            toUInt8(0) AS is_r_nonzero
        FROM canonical_execution_storage_diffs
        WHERE meta_network_name = 'mainnet'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT
            cityHash64(contract_address, slot) AS h,
            toUInt8(0) AS is_w_create,
            toUInt8(0) AS is_w_update,
            toUInt8(0) AS is_w_delete,
            toUInt8(value =  '0x000…0') AS is_r_zero,
            toUInt8(value != '0x000…0') AS is_r_nonzero
        FROM canonical_execution_storage_reads
        WHERE meta_network_name = 'mainnet'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
    )
    GROUP BY h
)
SELECT
    n_w_create, n_w_update, n_w_delete, n_r_zero, n_r_nonzero,
    count() AS n_keys
FROM per_key
GROUP BY n_w_create, n_w_update, n_w_delete, n_r_zero, n_r_nonzero
ORDER BY n_w_create, n_w_update, n_w_delete, n_r_zero, n_r_nonzero
```

The W_mixed decomposition (§4) and R returned-value split (§5) are derived in Python from
this histogram — no extra query.

### Account histogram (warmth, concentration)

Account key is `cityHash64(address)`. Writes come from `balance_diffs`, `nonce_diffs`, and
`contracts` (`contract_address` is the newly-materialized account). Reads come from
`balance_reads`, `nonce_reads`, and `address_appearances` filtered to non-ERC*
relationships. Same outer aggregation as the slot query.

```sql
WITH per_key AS (
    SELECT
        h,
        sum(is_w) AS n_w,
        sum(is_r) AS n_r
    FROM (
        SELECT cityHash64(address) AS h, 1 AS is_w, 0 AS is_r
        FROM canonical_execution_balance_diffs
        WHERE meta_network_name = 'mainnet'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address) AS h, 1 AS is_w, 0 AS is_r
        FROM canonical_execution_nonce_diffs
        WHERE meta_network_name = 'mainnet'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(contract_address) AS h, 1 AS is_w, 0 AS is_r
        FROM canonical_execution_contracts
        WHERE meta_network_name = 'mainnet'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address) AS h, 0 AS is_w, 1 AS is_r
        FROM canonical_execution_balance_reads
        WHERE meta_network_name = 'mainnet'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address) AS h, 0 AS is_w, 1 AS is_r
        FROM canonical_execution_nonce_reads
        WHERE meta_network_name = 'mainnet'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
        UNION ALL
        SELECT cityHash64(address) AS h, 0 AS is_w, 1 AS is_r
        FROM canonical_execution_address_appearances
        WHERE meta_network_name = 'mainnet'
          AND block_number BETWEEN {bn_lo} AND {bn_hi}
          AND relationship IN ('call_from', 'call_to', 'tx_from', 'tx_to',
                               'miner_fee', 'factory', 'create',
                               'suicide_refund', 'suicide')
    )
    GROUP BY h
)
SELECT
    multiIf(n_w > 0 AND n_r > 0, 'rw',
            n_w > 0,             'w_only',
                                 'r_only') AS slice,
    n_w,
    n_r,
    count() AS n_keys
FROM per_key
GROUP BY slice, n_w, n_r
ORDER BY slice, n_w, n_r
```

### Live-state totals (denominators)

```sql
SELECT block_number, accounts, storages
FROM execution_state_size
WHERE meta_network_name = 'mainnet' AND block_number <= 24870000
ORDER BY block_number DESC
LIMIT 1
```

At the anchor: 1,552,604,459 slots, 379,632,901 accounts, 1,932,237,360 combined
(ethpandaops profile — the local cluster's snapshot of this table is empty).

### Warm-update coverage (§7)

For each T, classify every update SSTORE event in `[anchor − T·7200, anchor]` by whether
the same slot had any earlier create-or-update event in the same window. One GROUP BY on
`cityHash64(address, slot)`; no JOIN, no window function.

```sql
WITH slot_events AS (
    SELECT
        cityHash64(address, slot) AS h,
        toUInt64(block_number) * 1000000000
            + toUInt64(transaction_index) * 100000
            + toUInt64(internal_index) AS event_order,
        (from_value != '0x000…0' AND to_value != '0x000…0') AS is_update,
        (from_value =  '0x000…0' AND to_value != '0x000…0') AS is_create
    FROM canonical_execution_storage_diffs
    WHERE meta_network_name = 'mainnet'
      AND block_number BETWEEN {bn_lo} AND {bn_hi}
),
per_slot AS (
    SELECT
        h,
        countIf(is_update) AS n_update,
        -- argMinIf returns is_update of the row with the smallest event_order
        -- among rows where (is_create OR is_update). Deletion rows are filtered out.
        argMinIf(is_update, event_order, is_create OR is_update) AS first_cu_is_update
    FROM slot_events
    GROUP BY h
    HAVING n_update > 0
)
SELECT
    sum(n_update) AS total_updates,
    sum(n_update - toUInt64(first_cu_is_update)) AS warm_updates,
    sum(toUInt64(first_cu_is_update)) AS cold_updates,
    round(100.0 * warm_updates / total_updates, 4) AS pct_warm
FROM per_slot
```

`event_order` packs `(block_number, transaction_index, internal_index)` into a single
monotone UInt64 (block numbers fit easily — `25M × 1e9 ≈ 2.5e16`, well below UInt64 max).

### First-operation classification (§8)

For each object in R∪W, find its earliest event in window by `(block_number,
transaction_index)` and classify. Slots and accounts both use the UNION-ALL-then-`argMin`
shape, no JOIN: `contracts` is dropped (redundant with `nonce_diffs`) and
`address_appearances` gets an end-of-block sort position (it always loses same-block ties,
matching its lowest priority).

```sql
-- Slots — slot_first_op
WITH slot_events AS (
    SELECT
        cityHash64(address, slot) AS h,
        toUInt64(block_number) * 1000000 + toUInt64(transaction_index) * 10 + 0 AS event_order,
        toUInt8(0) AS is_read,
        toUInt8(0) AS read_is_nonzero
    FROM canonical_execution_storage_diffs
    WHERE meta_network_name='mainnet' AND block_number BETWEEN {bn_lo} AND {bn_hi}
    UNION ALL
    SELECT
        cityHash64(contract_address, slot) AS h,
        toUInt64(block_number) * 1000000 + toUInt64(transaction_index) * 10 + 1 AS event_order,
        toUInt8(1) AS is_read,
        toUInt8(value != '0x000…0') AS read_is_nonzero
    FROM canonical_execution_storage_reads
    WHERE meta_network_name='mainnet' AND block_number BETWEEN {bn_lo} AND {bn_hi}
),
per_slot AS (
    SELECT
        h,
        argMin(is_read,         event_order) AS first_is_read,
        argMin(read_is_nonzero, event_order) AS first_read_is_nonzero
    FROM slot_events
    GROUP BY h
)
SELECT
    count() AS total_slots,
    sum(toUInt8(first_is_read = 0)) AS first_is_write,
    sum(toUInt8(first_is_read = 1 AND first_read_is_nonzero = 0)) AS first_is_zero_read,
    sum(toUInt8(first_is_read = 1 AND first_read_is_nonzero = 1)) AS first_is_nonzero_read
FROM per_slot;

-- Accounts — account_first_op (priority packing into event_order so the tie-break is
-- write > nonzero_read > zero_read > appearance_read at the same (block, tx_idx))
WITH all_events AS (
    SELECT cityHash64(address) AS h,
        toUInt64(block_number) * 10000000 + toUInt64(transaction_index) * 10 + 0 AS event_order,
        'write' AS op
    FROM canonical_execution_balance_diffs
    WHERE meta_network_name='mainnet' AND block_number BETWEEN {bn_lo} AND {bn_hi}
    UNION ALL  -- nonce_diffs (write, +0)
    UNION ALL  -- balance_reads: +1 if balance!=0 else +2, op nonzero_read/zero_read
    UNION ALL  -- nonce_reads:   +1 if nonce!=0   else +2, op nonzero_read/zero_read
    UNION ALL  -- address_appearances: event_order = block*10000000 + 9999999 (end-of-block),
               --   op appearance_read; no tx_index needed, always loses same-block ties
)
SELECT count() AS total_accounts,
       sum(toUInt8(op='write'))           AS first_is_write,
       sum(toUInt8(op='nonzero_read'))    AS first_is_nonzero_read,
       sum(toUInt8(op='zero_read'))       AS first_is_zero_read,
       sum(toUInt8(op='appearance_read')) AS first_is_appearance_read
FROM (SELECT h, argMin(op, event_order) AS op FROM all_events GROUP BY h);
```

Full SQL in `state_access/queries_v2.py` (`slot_first_op`, `account_first_op`).

### R-only empty/non-empty split (§8)

Single GROUP BY, no JOINs. Each source row is tagged with `UInt8` flags; we aggregate
`max(balance != 0)` rather than `max(balance)` so the per-group state is a byte, not a
`UInt256`. This is what lets it run at T=365d (a triple-CTE + double-LEFT-JOIN form stalls
the cluster).

```sql
WITH per_acct AS (
    SELECT
        h,
        max(is_write)     AS any_write,
        max(bal_nonzero)  AS bal_nonzero,
        max(non_nonzero)  AS non_nonzero,
        max(has_bal_read) AS has_bal_read,
        max(has_non_read) AS has_non_read
    FROM (
        -- writes (is_write=1): balance_diffs, nonce_diffs, contracts(contract_address)
        SELECT cityHash64(address) AS h, toUInt8(1) AS is_write,
               toUInt8(0) AS bal_nonzero, toUInt8(0) AS non_nonzero,
               toUInt8(0) AS has_bal_read, toUInt8(0) AS has_non_read
        FROM canonical_execution_balance_diffs WHERE in_window
        UNION ALL ... -- nonce_diffs, contracts: same write pattern
        -- value reads: balance_reads sets bal_nonzero/has_bal_read, nonce_reads similarly
        UNION ALL
        SELECT cityHash64(address), toUInt8(0),
               toUInt8(balance != 0), toUInt8(0), toUInt8(1), toUInt8(0)
        FROM canonical_execution_balance_reads WHERE in_window
        UNION ALL ... -- nonce_reads: toUInt8(nonce != 0) into non_nonzero, has_non_read=1
        -- value-less reads: address_appearances (all flags 0 except it's a read presence)
        UNION ALL
        SELECT cityHash64(address), toUInt8(0),
               toUInt8(0), toUInt8(0), toUInt8(0), toUInt8(0)
        FROM canonical_execution_address_appearances
        WHERE in_window AND relationship IN (...)
    )
    GROUP BY h
)
SELECT
    countIf(any_write = 0) AS total_r,
    countIf(any_write = 0 AND bal_nonzero = 0 AND non_nonzero = 0
            AND (has_bal_read = 1 OR has_non_read = 1)) AS empty_accounts,
    countIf(any_write = 0 AND (bal_nonzero = 1 OR non_nonzero = 1)) AS nonempty_accounts,
    countIf(any_write = 0 AND has_bal_read = 0 AND has_non_read = 0) AS unknown_accounts
FROM per_acct;
```

`contracts` is kept in the write set here (unlike `account_first_op`): ~1% of
contract-creation accounts lack a `nonce_diff`/`balance_diff` in window, and dropping
contracts would wrongly admit those written accounts into R.

## Appendix B — outputs

```
state_access/data/v2/
  slot_histogram.parquet         # raw (slice, n_w, n_r, n_keys) per T (input)
  account_histogram.parquet
  slot_typed_histogram.parquet   # typed slot histogram for §4 / §5
  slot_update_coverage.parquet   # per-T warm/cold update split for §7
  slot_first_op.parquet          # §8 first-op classification (slots)
  account_first_op.parquet       # §8 first-op classification (accounts)
  account_r_empty_split.parquet  # §8 empty/non-empty R-only accounts
  q1_warmth_{slot,account,combined}.parquet         # set sizes + pct of live state
  q1_warmth_slot_typed.parquet                       # typed slot W/R breakdown
  q1_warmth_slot_mixed_decomp.parquet                # W_mixed sub-categories
  q3_concentration_{slot,account}.parquet
  q1_warmth_{slot,account,combined}.png
  q1_warmth_slot_{W,R}_typed.png                     # typed slot stacked areas
  q1_warmth_slot_mixed_decomp.png                    # W_mixed 6-way decomposition
  slot_update_coverage.png                           # §7 warm/cold update line chart
  slot_first_op.png                                  # §8 slot first-op stacked bar
  account_first_op.png                               # §8 account first-op stacked bar
  account_r_empty_split.png                          # §8 R-only empty vs non-empty
  q3_concentration_{top1,top10}_{slot,account}.png
```

Reproduce: `uv run python -m state_access.collect_v2 && uv run python -m state_access.analysis_v2`.
`collect_v2` is resumable per `(T, object_type)` cell; delete a histogram parquet to force a
re-pull. Verification checks (additivity `|R∪W|=|W|+|R|`, partition sum, monotonicity) live
in `analysis_v2.run_one`.
